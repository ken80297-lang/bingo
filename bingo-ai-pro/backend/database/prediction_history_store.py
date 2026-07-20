from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"
_INITIALIZED = False
_LEARNED_ISSUES_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0}
LEARNED_ISSUES_TTL_SECONDS = 30
_PREDICTION_STATS_CACHE: dict[str, Any] = {"payload": {}, "expires_at": {}}
PREDICTION_STATS_TTL_SECONDS = 60
MIN_PRODUCTION_ISSUE_LENGTH = 6
PRODUCTION_PREDICTION_QUERY_NAME = "production_latest_prediction_v2"
_NON_PRODUCTION_TEXT_MARKERS = ("preview", "simulation", "test", "fixture", "synthetic")

LIFECYCLE_COLUMNS = {
    "prediction_status": ("text default 'waiting_draw'", "text default 'waiting_draw'"),
    "verified_issue": ("text", "text"),
    "verified_at": ("timestamptz", "text"),
    "matched_numbers": ("jsonb", "text"),
    "missed_numbers": ("jsonb", "text"),
    "prediction_count": ("integer default 0", "integer default 0"),
    "hit_rate": ("double precision default 0", "real default 0"),
    "super_number_hit": ("boolean default false", "integer default 0"),
    "verification_version": ("text", "text"),
    "learning_used": ("boolean default false", "integer default 0"),
    "model_score": ("double precision default 0", "real default 0"),
}
ALLOWED_PREDICTION_STATUSES = {"pending", "waiting_draw", "verified", "expired", "failed"}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _invalidate_prediction_stats_cache() -> None:
    _PREDICTION_STATS_CACHE["payload"] = {}
    _PREDICTION_STATS_CACHE["expires_at"] = {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value

def _prediction_status(value: Any, has_winning_numbers: bool = False) -> str:
    if has_winning_numbers:
        return "verified"
    text = str(value or "waiting_draw")
    return text if text in ALLOWED_PREDICTION_STATUSES else "waiting_draw"


def _normalize_numbers(values: Any, limit: int | None = None) -> list[int]:
    numbers: list[int] = []
    if isinstance(values, str):
        parsed = _json_loads(values)
        values = parsed if isinstance(parsed, list) else [values]
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    numbers.sort()
    return numbers[:limit] if limit else numbers


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


def _valid_production_issue(value: Any) -> str | None:
    issue = _valid_issue(value)
    if not issue or len(issue) < MIN_PRODUCTION_ISSUE_LENGTH:
        return None
    return issue


def is_production_prediction(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    based_on = _valid_production_issue(record.get("issue") or record.get("based_on_issue"))
    target = _valid_production_issue(record.get("prediction_issue") or record.get("target_issue"))
    if not based_on or not target:
        return False
    try:
        if int(target) != int(based_on) + 1:
            return False
    except Exception:
        return False
    recommended = _normalize_numbers(record.get("recommend_numbers", []))
    if not recommended:
        return False
    marker_text = " ".join(
        str(record.get(key) or "")
        for key in ("strategy", "source", "trigger", "model_version")
    ).lower()
    return not any(marker in marker_text for marker in _NON_PRODUCTION_TEXT_MARKERS)


def _validate_prediction_item(item: dict) -> tuple[bool, str | None]:
    based_on = _valid_issue(item.get("issue"))
    target = _valid_issue(item.get("prediction_issue"))
    if not based_on:
        return False, "based_on_missing"
    if not target:
        return False, "target_unconfirmed"
    try:
        if int(target) != int(based_on) + 1:
            return False, "target_unconfirmed"
    except Exception:
        return False, "target_unconfirmed"
    recommended = _normalize_numbers(item.get("recommend_numbers", []))
    if not recommended:
        return False, "insufficient_draw_data"
    return True, None


def _record_prediction_event(
    *,
    item: dict,
    event_type: str,
    prediction_created: bool,
    prediction_skipped: bool,
    skip_reason: str | None,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: float | None = None,
) -> None:
    try:
        from services.operations_center import record_operation_event

        based_on = item.get("issue")
        target = item.get("prediction_issue")
        payload = {
            "event_type": event_type,
            "based_on_issue": based_on,
            "proposed_target_issue": target,
            "confirmed_target_issue": target if not skip_reason else None,
            "prediction_created": prediction_created,
            "prediction_skipped": prediction_skipped,
            "skip_reason": skip_reason,
            "model_version": item.get("model_version") or item.get("strategy"),
            "recommended_count": len(_normalize_numbers(item.get("recommend_numbers", []))),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
        }
        record_operation_event(
            component="prediction",
            event_type=event_type,
            status="warning" if prediction_skipped else "ok",
            issue=str(based_on) if based_on else None,
            message=_json_dumps(payload),
            duration_ms=duration_ms,
            error_type=skip_reason if prediction_skipped else None,
        )
    except Exception:
        logger.exception("prediction lifecycle event recording failed")


def _record_prediction_write_rejected(item: dict, reason: str) -> None:
    try:
        from services.operations_center import record_operation_event

        payload = {
            "event_type": "prediction_write_rejected",
            "based_on_issue": item.get("issue"),
            "target_issue": item.get("prediction_issue"),
            "source": item.get("source"),
            "trigger": item.get("trigger"),
            "status": "skipped",
            "reason": reason,
            "recommended_count": len(_normalize_numbers(item.get("recommend_numbers", []))),
        }
        record_operation_event(
            component="prediction",
            event_type="prediction_write_rejected",
            status="warning",
            issue=str(item.get("issue") or "") or None,
            message=_json_dumps(payload),
            error_type=reason,
        )
    except Exception:
        logger.exception("prediction write rejection event failed")


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_prediction_history_tables() -> dict:
    global _INITIALIZED
    results = {"cloud": "unknown", "sqlite": "unknown"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists prediction_history (
                            id bigserial primary key,
                            issue text,
                            prediction_issue text,
                            predict_time timestamptz,
                            strategy text,
                            confidence double precision,
                            recommend_numbers jsonb,
                            super_number integer,
                            three_star jsonb,
                            four_star jsonb,
                            twins jsonb,
                            consecutive jsonb,
                            patch_numbers jsonb,
                            tails jsonb,
                            big_small text,
                            odd_even text,
                            reasons jsonb,
                            winning_numbers jsonb,
                            hit_count integer default 0,
                            super_hit boolean default false,
                            three_star_hit boolean default false,
                            four_star_hit boolean default false,
                            accuracy double precision default 0,
                            model_scores jsonb,
                            winning_model text,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now(),
                            unique(prediction_issue, strategy)
                        )
                        """,
                        prepare=False,
                    )
                    cur.execute("alter table prediction_history add column if not exists model_scores jsonb", prepare=False)
                    cur.execute("alter table prediction_history add column if not exists winning_model text", prepare=False)
                    for column, (cloud_type, _) in LIFECYCLE_COLUMNS.items():
                        cur.execute(
                            f"alter table prediction_history add column if not exists {column} {cloud_type}",
                            prepare=False,
                        )
                    for index_sql in (
                        "create index if not exists idx_prediction_history_created_at on prediction_history (created_at)",
                        "create index if not exists idx_prediction_history_updated_at on prediction_history (updated_at)",
                        "create index if not exists idx_prediction_history_status on prediction_history (prediction_status)",
                        "create index if not exists idx_prediction_history_issue on prediction_history (prediction_issue)",
                        "create unique index if not exists idx_prediction_history_unique_target on prediction_history (prediction_issue) where prediction_issue is not null",
                    ):
                        cur.execute(index_sql, prepare=False)
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            logger.exception("failed to initialize cloud prediction_history table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists prediction_history (
                    id integer primary key autoincrement,
                    issue text,
                    prediction_issue text,
                    predict_time text,
                    strategy text,
                    confidence real,
                    recommend_numbers text,
                    super_number integer,
                    three_star text,
                    four_star text,
                    twins text,
                    consecutive text,
                    patch_numbers text,
                    tails text,
                    big_small text,
                    odd_even text,
                    reasons text,
                    winning_numbers text,
                    hit_count integer default 0,
                    super_hit integer default 0,
                    three_star_hit integer default 0,
                    four_star_hit integer default 0,
                    accuracy real default 0,
                    model_scores text,
                    winning_model text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp,
                    unique(prediction_issue, strategy)
                )
                """
            )
            existing = {row[1] for row in conn.execute("pragma table_info(prediction_history)").fetchall()}
            if "model_scores" not in existing:
                conn.execute("alter table prediction_history add column model_scores text")
            if "winning_model" not in existing:
                conn.execute("alter table prediction_history add column winning_model text")
            for column, (_, sqlite_type) in LIFECYCLE_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"alter table prediction_history add column {column} {sqlite_type}")
            for index_sql in (
                "create index if not exists idx_prediction_history_created_at on prediction_history (created_at)",
                "create index if not exists idx_prediction_history_updated_at on prediction_history (updated_at)",
                "create index if not exists idx_prediction_history_status on prediction_history (prediction_status)",
                "create index if not exists idx_prediction_history_issue on prediction_history (prediction_issue)",
                "create unique index if not exists idx_prediction_history_unique_target on prediction_history (prediction_issue) where prediction_issue is not null",
            ):
                conn.execute(index_sql)
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite prediction_history table")

    _INITIALIZED = True
    return results


def _ensure_initialized() -> None:
    global _INITIALIZED
    if not _INITIALIZED:
        # Table creation/migration belongs to FastAPI startup. Running DDL from
        # read APIs can block player dashboard requests when Supabase is busy.
        _INITIALIZED = True


def _prediction_params(item: dict) -> tuple:
    recommended = _normalize_numbers(item.get("recommend_numbers", []))
    return (
        item.get("issue"),
        item.get("prediction_issue"),
        item.get("predict_time") or _now(),
        item.get("strategy"),
        item.get("confidence"),
        _json_dumps(recommended),
        item.get("super_number"),
        _json_dumps(_normalize_numbers(item.get("three_star", []))),
        _json_dumps(_normalize_numbers(item.get("four_star", []))),
        _json_dumps(item.get("twins", [])),
        _json_dumps(item.get("consecutive", [])),
        _json_dumps(_normalize_numbers(item.get("patch_numbers", []))),
        _json_dumps(item.get("tails", [])),
        item.get("big_small"),
        item.get("odd_even"),
        _json_dumps(item.get("reasons", [])),
        _json_dumps(item.get("model_scores", {})),
        item.get("winning_model"),
        item.get("prediction_status") or "waiting_draw",
        len(recommended or []),
        bool(item.get("learning_used", False)),
    )


def save_prediction_history(item: dict, *, caller_context: str | None = None) -> dict:
    _ensure_initialized()
    if caller_context != "prediction_service":
        _record_prediction_write_rejected(item, "unauthorized_writer")
        return {
            "status": "rejected",
            "message": "prediction history writes must go through PredictionService",
            "skip_reason": "unauthorized_writer",
        }
    is_valid, skip_reason = _validate_prediction_item(item)
    if not is_valid:
        _record_prediction_event(
            item=item,
            event_type="prediction_skipped",
            prediction_created=False,
            prediction_skipped=True,
            skip_reason=skip_reason,
        )
        return {
            "status": "skipped",
            "message": "prediction target is not confirmed",
            "skip_reason": skip_reason,
        }
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into prediction_history
                        (
                            issue, prediction_issue, predict_time, strategy, confidence,
                            recommend_numbers, super_number, three_star, four_star, twins,
                            consecutive, patch_numbers, tails, big_small, odd_even, reasons,
                            model_scores, winning_model, prediction_status, prediction_count,
                            learning_used, updated_at
                        )
                        values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
                                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                                %s::jsonb, %s, %s, %s, %s, now())
                        on conflict (prediction_issue, strategy) do update set
                            issue = excluded.issue,
                            predict_time = excluded.predict_time,
                            confidence = excluded.confidence,
                            recommend_numbers = excluded.recommend_numbers,
                            super_number = excluded.super_number,
                            three_star = excluded.three_star,
                            four_star = excluded.four_star,
                            twins = excluded.twins,
                            consecutive = excluded.consecutive,
                            patch_numbers = excluded.patch_numbers,
                            tails = excluded.tails,
                            big_small = excluded.big_small,
                            odd_even = excluded.odd_even,
                            reasons = excluded.reasons,
                            model_scores = excluded.model_scores,
                            winning_model = excluded.winning_model,
                            prediction_status = case
                                when prediction_history.prediction_status in ('verified', 'failed')
                                then prediction_history.prediction_status
                                else excluded.prediction_status
                            end,
                            prediction_count = excluded.prediction_count,
                            updated_at = now()
                        returning id
                        """,
                        _prediction_params(item),
                        prepare=False,
                    )
                    row_id = int(cur.fetchone()[0])
                conn.commit()
            _invalidate_prediction_stats_cache()
            _record_prediction_event(
                item=item,
                event_type="prediction_created",
                prediction_created=True,
                prediction_skipped=False,
                skip_reason=None,
            )
            return {"status": "ok", "storage": "cloud", "id": row_id}
        except Exception as exc:
            logger.exception("cloud prediction_history save failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into prediction_history
                (
                    issue, prediction_issue, predict_time, strategy, confidence,
                    recommend_numbers, super_number, three_star, four_star, twins,
                    consecutive, patch_numbers, tails, big_small, odd_even, reasons,
                    model_scores, winning_model, prediction_status, prediction_count,
                    learning_used, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(prediction_issue, strategy) do update set
                    issue = excluded.issue,
                    predict_time = excluded.predict_time,
                    confidence = excluded.confidence,
                    recommend_numbers = excluded.recommend_numbers,
                    super_number = excluded.super_number,
                    three_star = excluded.three_star,
                    four_star = excluded.four_star,
                    twins = excluded.twins,
                    consecutive = excluded.consecutive,
                    patch_numbers = excluded.patch_numbers,
                    tails = excluded.tails,
                    big_small = excluded.big_small,
                    odd_even = excluded.odd_even,
                    reasons = excluded.reasons,
                    model_scores = excluded.model_scores,
                    winning_model = excluded.winning_model,
                    prediction_status = case
                        when prediction_history.prediction_status in ('verified', 'failed')
                        then prediction_history.prediction_status
                        else excluded.prediction_status
                    end,
                    prediction_count = excluded.prediction_count,
                    updated_at = excluded.updated_at
                """,
                (*_prediction_params(item), _now()),
            )
            row_id = int(cursor.lastrowid or 0)
        _invalidate_prediction_stats_cache()
        _record_prediction_event(
            item=item,
            event_type="prediction_created",
            prediction_created=True,
            prediction_skipped=False,
            skip_reason=None,
        )
        return {"status": "ok", "storage": "sqlite", "id": row_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite prediction_history save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params, prepare=False)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    if _cloud_enabled():
        try:
            return _query_cloud(sql, params)
        except Exception:
            logger.exception("cloud prediction_history query failed")
    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite prediction_history query failed")
        return []


def _row_to_prediction(row: Any) -> dict:
    recommend_numbers = _normalize_numbers(_json_loads(row[6]) or [])
    winning_numbers = _normalize_numbers(_json_loads(row[17]) or [])
    matched_numbers = _normalize_numbers(_json_loads(row[30]) if len(row) > 30 else [])
    missed_numbers = _normalize_numbers(_json_loads(row[31]) if len(row) > 31 else [])
    if winning_numbers and not matched_numbers:
        winning_set = set(_as_int_list(winning_numbers))
        matched_numbers = [number for number in _as_int_list(recommend_numbers) if number in winning_set]
    if winning_numbers and not missed_numbers:
        winning_set = set(_as_int_list(winning_numbers))
        missed_numbers = [number for number in _as_int_list(recommend_numbers) if number not in winning_set]
    raw_status = row[27] if len(row) > 27 and row[27] else None
    effective_status = _prediction_status(raw_status, bool(winning_numbers))
    return {
        "id": row[0],
        "issue": row[1],
        "prediction_issue": row[2],
        "predict_time": str(row[3]) if row[3] is not None else None,
        "strategy": row[4],
        "confidence": row[5],
        "recommend_numbers": recommend_numbers,
        "super_number": row[7],
        "three_star": _normalize_numbers(_json_loads(row[8]) or []),
        "four_star": _normalize_numbers(_json_loads(row[9]) or []),
        "twins": _json_loads(row[10]) or [],
        "consecutive": _json_loads(row[11]) or [],
        "patch_numbers": _normalize_numbers(_json_loads(row[12]) or []),
        "tails": _json_loads(row[13]) or [],
        "big_small": row[14],
        "odd_even": row[15],
        "reasons": _json_loads(row[16]) or [],
        "winning_numbers": winning_numbers,
        "hit_count": row[18] or 0,
        "super_hit": bool(row[19]),
        "three_star_hit": bool(row[20]),
        "four_star_hit": bool(row[21]),
        "accuracy": row[22] or 0,
        "created_at": str(row[23]) if row[23] is not None else None,
        "updated_at": str(row[24]) if row[24] is not None else None,
        "model_scores": _json_loads(row[25]) if len(row) > 25 else {},
        "winning_model": row[26] if len(row) > 26 else None,
        "prediction_status": effective_status,
        "verified_issue": row[28] if len(row) > 28 else None,
        "verified_at": str(row[29]) if len(row) > 29 and row[29] is not None else None,
        "matched_numbers": matched_numbers or [],
        "missed_numbers": missed_numbers or [],
        "prediction_count": row[32] if len(row) > 32 and row[32] is not None else len(recommend_numbers or []),
        "hit_rate": row[33] if len(row) > 33 and row[33] is not None else (row[22] or 0),
        "super_number_hit": bool(row[34]) if len(row) > 34 else bool(row[19]),
        "verification_version": row[35] if len(row) > 35 else None,
        "learning_used": bool(row[36]) if len(row) > 36 else False,
        "model_score": row[37] if len(row) > 37 else None,
    }


def _prediction_event_metadata(record: dict) -> dict:
    based_on = str(record.get("issue") or "")
    target = str(record.get("prediction_issue") or "")
    if not based_on and not target:
        return {}
    rows = _query_with_fallback(
        """
        select message
        from operation_events
        where event_type = 'prediction_created'
          and (
            issue = %s
            or message like %s
          )
        order by created_at desc, id desc
        limit 1
        """,
        (based_on, f"%{target}%"),
        sqlite_sql="""
        select message
        from operation_events
        where event_type = 'prediction_created'
          and (
            issue = ?
            or message like ?
          )
        order by created_at desc, id desc
        limit 1
        """,
    )
    if not rows:
        return {}
    payload = _json_loads(rows[0][0])
    if not isinstance(payload, dict):
        return {}
    return {
        "source": payload.get("source"),
        "trigger": payload.get("trigger"),
        "operation_event": {
            "event_type": payload.get("event_type") or "prediction_created",
            "based_on_issue": payload.get("based_on_issue"),
            "target_issue": payload.get("target_issue"),
            "recommended_count": payload.get("recommended_count"),
        },
    }


def _enrich_prediction_metadata(record: dict) -> dict:
    metadata = _prediction_event_metadata(record)
    record["source"] = metadata.get("source") or "production_history"
    record["trigger"] = metadata.get("trigger") or "production_read_layer"
    if metadata.get("operation_event"):
        record["operation_event"] = metadata.get("operation_event")
    return record


PREDICTION_SELECT_COLUMNS = """
        id, issue, prediction_issue, predict_time, strategy, confidence,
        recommend_numbers, super_number, three_star, four_star, twins,
        consecutive, patch_numbers, tails, big_small, odd_even, reasons,
        winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
        accuracy, created_at, updated_at, model_scores, winning_model,
        prediction_status, verified_issue, verified_at, matched_numbers,
        missed_numbers, prediction_count, hit_rate, super_number_hit,
        verification_version, learning_used, model_score
"""

PREDICTION_SELECT_COLUMNS_P = """
        p.id, p.issue, p.prediction_issue, p.predict_time, p.strategy, p.confidence,
        p.recommend_numbers, p.super_number, p.three_star, p.four_star, p.twins,
        p.consecutive, p.patch_numbers, p.tails, p.big_small, p.odd_even, p.reasons,
        p.winning_numbers, p.hit_count, p.super_hit, p.three_star_hit, p.four_star_hit,
        p.accuracy, p.created_at, p.updated_at, p.model_scores, p.winning_model,
        p.prediction_status, p.verified_issue, p.verified_at, p.matched_numbers,
        p.missed_numbers, p.prediction_count, p.hit_rate, p.super_number_hit,
        p.verification_version, p.learning_used, p.model_score
"""


def get_latest_prediction_history() -> dict | None:
    _ensure_initialized()
    rows = _query_with_fallback(
        """
        select {columns}
        from prediction_history p
        left join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue ~ '^[0-9]+$'
          and p.prediction_issue ~ '^[0-9]+$'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%%'
          and p.prediction_issue not like '99%%'
          and upper(p.issue) not like 'TEST%%'
          and upper(p.prediction_issue) not like 'TEST%%'
          and p.prediction_issue::bigint = p.issue::bigint + 1
          and jsonb_typeof(p.recommend_numbers) = 'array'
          and jsonb_array_length(p.recommend_numbers) > 0
          and coalesce(lower(p.strategy), '') not like '%%preview%%'
          and coalesce(lower(p.strategy), '') not like '%%simulation%%'
          and coalesce(lower(p.strategy), '') not like '%%test%%'
          and coalesce(lower(p.strategy), '') not like '%%fixture%%'
          and coalesce(lower(p.strategy), '') not like '%%synthetic%%'
        order by p.prediction_issue::bigint desc, p.created_at desc, p.id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
        sqlite_sql="""
        select {columns}
        from prediction_history p
        left join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue not glob '*[^0-9]*'
          and p.prediction_issue not glob '*[^0-9]*'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%'
          and p.prediction_issue not like '99%'
          and upper(p.issue) not like 'TEST%'
          and upper(p.prediction_issue) not like 'TEST%'
          and cast(p.prediction_issue as integer) = cast(p.issue as integer) + 1
          and p.recommend_numbers is not null
          and p.recommend_numbers not in ('', '[]')
          and coalesce(lower(p.strategy), '') not like '%preview%'
          and coalesce(lower(p.strategy), '') not like '%simulation%'
          and coalesce(lower(p.strategy), '') not like '%test%'
          and coalesce(lower(p.strategy), '') not like '%fixture%'
          and coalesce(lower(p.strategy), '') not like '%synthetic%'
        order by cast(p.prediction_issue as integer) desc, p.created_at desc, p.id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
    )
    if not rows:
        return None
    record = _row_to_prediction(rows[0])
    record["read_layer"] = {
        "data_source": "database",
        "table_name": "prediction_history",
        "query_name": PRODUCTION_PREDICTION_QUERY_NAME,
        "production_filtered": True,
    }
    return _enrich_prediction_metadata(record)


def get_prediction_history_records(limit: int = 100) -> list[dict]:
    _ensure_initialized()
    limit = max(1, min(int(limit or 100), 500))
    rows = _query_with_fallback(
        """
        select {columns}
        from prediction_history p
        left join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue ~ '^[0-9]+$'
          and p.prediction_issue ~ '^[0-9]+$'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%%'
          and p.prediction_issue not like '99%%'
          and upper(p.issue) not like 'TEST%%'
          and upper(p.prediction_issue) not like 'TEST%%'
          and p.prediction_issue::bigint = p.issue::bigint + 1
          and jsonb_typeof(p.recommend_numbers) = 'array'
          and jsonb_array_length(p.recommend_numbers) > 0
          and coalesce(lower(p.strategy), '') not like '%%preview%%'
          and coalesce(lower(p.strategy), '') not like '%%simulation%%'
          and coalesce(lower(p.strategy), '') not like '%%test%%'
          and coalesce(lower(p.strategy), '') not like '%%fixture%%'
          and coalesce(lower(p.strategy), '') not like '%%synthetic%%'
        order by p.prediction_issue::bigint desc, p.created_at desc, p.id desc
        limit %s
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
        (limit,),
        sqlite_sql="""
        select {columns}
        from prediction_history p
        left join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue not glob '*[^0-9]*'
          and p.prediction_issue not glob '*[^0-9]*'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%'
          and p.prediction_issue not like '99%'
          and upper(p.issue) not like 'TEST%'
          and upper(p.prediction_issue) not like 'TEST%'
          and cast(p.prediction_issue as integer) = cast(p.issue as integer) + 1
          and p.recommend_numbers is not null
          and p.recommend_numbers not in ('', '[]')
          and coalesce(lower(p.strategy), '') not like '%preview%'
          and coalesce(lower(p.strategy), '') not like '%simulation%'
          and coalesce(lower(p.strategy), '') not like '%test%'
          and coalesce(lower(p.strategy), '') not like '%fixture%'
          and coalesce(lower(p.strategy), '') not like '%synthetic%'
        order by cast(p.prediction_issue as integer) desc, p.created_at desc, p.id desc
        limit ?
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
    )
    records = []
    for row in rows:
        record = _row_to_prediction(row)
        record["read_layer"] = {
            "data_source": "database",
            "table_name": "prediction_history",
            "query_name": "production_prediction_history_v2",
            "production_filtered": True,
        }
        records.append(_enrich_prediction_metadata(record))
    return records


def get_prediction_history_count() -> int:
    _ensure_initialized()
    rows = _query_with_fallback("select count(*) from prediction_history")
    if not rows:
        return 0
    try:
        return int(rows[0][0] or 0)
    except Exception:
        return 0


def get_prediction_lifecycle_aggregates() -> dict:
    learned_count = 0
    try:
        from database.learning_store import get_learned_live_target_count

        learned_count = get_learned_live_target_count()
    except Exception:
        logger.exception("learned live target count failed")

    rows = _query_with_fallback(
        """
        select
            count(*) as total_prediction_count,
            sum(case when prediction_issue is not null then 1 else 0 end) as valid_target_count,
            sum(case when prediction_issue is null then 1 else 0 end) as null_target_count,
            sum(case when prediction_issue is not null
                      and jsonb_typeof(recommend_numbers) = 'array'
                      and jsonb_array_length(recommend_numbers) > 0
                     then 1 else 0 end) as valid_prediction_count,
            sum(case when prediction_status = 'verified'
                      and verified_at is not null
                      and jsonb_typeof(winning_numbers) = 'array'
                      and jsonb_array_length(winning_numbers) = 20
                      and jsonb_typeof(matched_numbers) = 'array'
                      and jsonb_typeof(missed_numbers) = 'array'
                     then 1 else 0 end) as completed_verified_count,
            sum(case when jsonb_typeof(winning_numbers) = 'array'
                      and jsonb_array_length(winning_numbers) = 20
                     then 1 else 0 end) as stored_official_result_count,
            sum(case when prediction_issue is not null
                      and prediction_status = 'verified'
                      and verified_at is not null
                      and jsonb_typeof(winning_numbers) = 'array'
                      and jsonb_array_length(winning_numbers) = 20
                      and jsonb_typeof(recommend_numbers) = 'array'
                      and jsonb_array_length(recommend_numbers) > 0
                     then 1 else 0 end) as valid_sample_count
        from prediction_history
        """,
        sqlite_sql="""
        select
            count(*) as total_prediction_count,
            sum(case when prediction_issue is not null then 1 else 0 end) as valid_target_count,
            sum(case when prediction_issue is null then 1 else 0 end) as null_target_count,
            sum(case when prediction_issue is not null
                      and recommend_numbers is not null
                      and recommend_numbers not in ('', '[]')
                     then 1 else 0 end) as valid_prediction_count,
            sum(case when prediction_status = 'verified'
                      and verified_at is not null
                      and winning_numbers is not null
                      and winning_numbers not in ('', '[]')
                      and matched_numbers is not null
                      and missed_numbers is not null
                     then 1 else 0 end) as completed_verified_count,
            sum(case when winning_numbers is not null
                      and winning_numbers not in ('', '[]')
                     then 1 else 0 end) as stored_official_result_count,
            sum(case when prediction_issue is not null
                      and prediction_status = 'verified'
                      and verified_at is not null
                      and winning_numbers is not null
                      and winning_numbers not in ('', '[]')
                      and recommend_numbers is not null
                      and recommend_numbers not in ('', '[]')
                     then 1 else 0 end) as valid_sample_count
        from prediction_history
        """,
    )
    row = rows[0] if rows else [0] * 7
    official_rows = _query_with_fallback(
        """
        select count(distinct p.prediction_issue)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and jsonb_typeof(o.numbers) = 'array'
          and jsonb_array_length(o.numbers) = 20
        """,
        sqlite_sql="""
        select count(distinct p.prediction_issue)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and o.numbers is not null
          and o.numbers not in ('', '[]')
        """,
    )
    return {
        "total_prediction_count": int(row[0] or 0),
        "valid_target_count": int(row[1] or 0),
        "null_target_count": int(row[2] or 0),
        "valid_prediction_count": int(row[3] or 0),
        "completed_verified_count": int(row[4] or 0),
        "stored_official_result_count": int(row[5] or 0),
        "has_official_result_count": int(official_rows[0][0] or 0) if official_rows else 0,
        "valid_sample_count": int(row[6] or 0),
        "learned_distinct_target_count": learned_count,
    }


def get_prediction_daily_aggregation() -> list[dict]:
    rows = _query_with_fallback(
        """
        select date(created_at), count(*)
        from prediction_history
        group by date(created_at)
        order by date(created_at)
        """,
    )
    return [{"date": str(row[0]), "prediction_count": int(row[1] or 0)} for row in rows]


def get_prediction_hourly_aggregation() -> list[dict]:
    rows = _query_with_fallback(
        """
        select extract(hour from created_at)::int as hour, count(*)
        from prediction_history
        where created_at is not null
        group by hour
        order by hour
        """,
        sqlite_sql="""
        select cast(strftime('%H', created_at) as integer) as hour, count(*)
        from prediction_history
        where created_at is not null
        group by hour
        order by hour
        """,
    )
    values = {int(row[0]): int(row[1] or 0) for row in rows if row[0] is not None}
    return [{"hour": hour, "prediction_count": values.get(hour, 0)} for hour in range(24)]

def mark_prediction_learning_used(issue: str, used: bool = True) -> dict:
    _ensure_initialized()
    issue = str(issue or "")
    if not issue:
        return {"status": "error", "updated": 0, "error": "missing issue"}
    updated = 0
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        update prediction_history
                        set learning_used = %s,
                            updated_at = now()
                        where prediction_issue = %s
                          and prediction_status = 'verified'
                        """,
                        (used, issue),
                        prepare=False,
                    )
                    updated = cur.rowcount or 0
                conn.commit()
            _invalidate_prediction_stats_cache()
            return {"status": "ok", "storage": "cloud", "updated": updated}
        except Exception:
            logger.exception("cloud prediction_history learning_used update failed")

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                update prediction_history
                set learning_used = ?,
                    updated_at = ?
                where prediction_issue = ?
                  and prediction_status = 'verified'
                """,
                (1 if used else 0, _now(), issue),
            )
            updated = cursor.rowcount or 0
        _invalidate_prediction_stats_cache()
        return {"status": "ok", "storage": "sqlite", "updated": updated}
    except Exception as exc:
        logger.exception("sqlite prediction_history learning_used update failed")
        return {"status": "error", "updated": 0, "error": str(exc)}
    try:
        return int(rows[0][0] or 0)
    except Exception:
        return 0


def _prediction_records_for_target_issue(issue: str) -> list[dict]:
    rows = _query_with_fallback(
        """
        select {columns}
        from prediction_history
        where prediction_issue = %s
          and issue is not null
          and prediction_issue is not null
        order by created_at desc, id desc
        """.format(columns=PREDICTION_SELECT_COLUMNS),
        (str(issue),),
        sqlite_sql="""
        select {columns}
        from prediction_history
        where prediction_issue = ?
          and issue is not null
          and prediction_issue is not null
        order by created_at desc, id desc
        """.format(columns=PREDICTION_SELECT_COLUMNS),
    )
    return [_row_to_prediction(row) for row in rows]


def get_prediction_for_source_target(source_issue: str, target_issue: str) -> dict | None:
    source = _valid_issue(source_issue)
    target = _valid_issue(target_issue)
    if not source or not target:
        return None
    rows = _query_with_fallback(
        """
        select {columns}
        from prediction_history
        where issue = %s
          and prediction_issue = %s
          and issue is not null
          and prediction_issue is not null
          and recommend_numbers is not null
          and jsonb_typeof(recommend_numbers) = 'array'
          and jsonb_array_length(recommend_numbers) > 0
        order by created_at desc, id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS),
        (source, target),
        sqlite_sql="""
        select {columns}
        from prediction_history
        where issue = ?
          and prediction_issue = ?
          and issue is not null
          and prediction_issue is not null
          and recommend_numbers is not null
          and recommend_numbers not in ('', '[]')
        order by created_at desc, id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS),
    )
    if not rows:
        return None
    record = _row_to_prediction(rows[0])
    record["read_layer"] = {
        "data_source": "database",
        "table_name": "prediction_history",
        "query_name": "prediction_for_source_target",
        "production_filtered": True,
    }
    return record


def get_latest_prediction_context() -> dict | None:
    rows = _query_with_fallback(
        """
        with latest as (
            select id, issue, draw_date, draw_time, numbers, open_order_numbers,
                   super_number, win_no_only, source, verification_status, fetched_at,
                   verified, raw_json, created_at, updated_at
            from official_draw_history
            where issue ~ '^[0-9]+$'
              and length(issue) >= 6
              and issue not like '99%%'
              and upper(issue) not like 'TEST%%'
            order by issue::bigint desc
            limit 1
        ),
        prediction as (
            select {columns}
            from prediction_history
            where issue = (select issue from latest)
              and prediction_issue = ((select issue from latest)::bigint + 1)::text
              and issue is not null
              and prediction_issue is not null
              and recommend_numbers is not null
              and jsonb_typeof(recommend_numbers) = 'array'
              and jsonb_array_length(recommend_numbers) > 0
            order by created_at desc, id desc
            limit 1
        )
        select latest.*, prediction.*
        from latest
        left join prediction on true
        """.format(columns=PREDICTION_SELECT_COLUMNS),
        sqlite_sql="""
        with latest as (
            select id, issue, draw_date, draw_time, numbers, open_order_numbers,
                   super_number, win_no_only, source, verification_status, fetched_at,
                   verified, raw_json, created_at, updated_at
            from official_draw_history
            where issue glob '[0-9]*'
              and length(issue) >= 6
              and issue not like '99%'
              and upper(issue) not like 'TEST%'
            order by cast(issue as integer) desc
            limit 1
        ),
        prediction as (
            select {columns}
            from prediction_history
            where issue = (select issue from latest)
              and prediction_issue = cast(cast((select issue from latest) as integer) + 1 as text)
              and issue is not null
              and prediction_issue is not null
              and recommend_numbers is not null
              and recommend_numbers not in ('', '[]')
            order by created_at desc, id desc
            limit 1
        )
        select latest.*, prediction.*
        from latest
        left join prediction on 1 = 1
        """.format(columns=PREDICTION_SELECT_COLUMNS),
    )
    if not rows:
        return None
    from database.official_draw_store import _row_to_official

    row = rows[0]
    draw = _row_to_official(row[:15])
    prediction = _row_to_prediction(row[15:]) if row[15] is not None else None
    source_issue = _valid_issue(draw.get("issue"))
    return {
        "draw": draw,
        "prediction": prediction,
        "target_issue": str(int(source_issue) + 1) if source_issue else None,
    }


def get_latest_verified_prediction_at_or_before(issue: str) -> dict | None:
    target = _valid_issue(issue)
    if not target:
        return None
    rows = _query_with_fallback(
        """
        select {columns}
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue ~ '^[0-9]+$'
          and p.prediction_issue ~ '^[0-9]+$'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%%'
          and p.prediction_issue not like '99%%'
          and upper(p.issue) not like 'TEST%%'
          and upper(p.prediction_issue) not like 'TEST%%'
          and p.prediction_issue::bigint = p.issue::bigint + 1
          and p.prediction_issue::bigint <= %s::bigint
          and jsonb_typeof(p.recommend_numbers) = 'array'
          and jsonb_array_length(p.recommend_numbers) = 20
          and jsonb_typeof(coalesce(p.winning_numbers, o.numbers)) = 'array'
          and jsonb_array_length(coalesce(p.winning_numbers, o.numbers)) = 20
          and coalesce(lower(p.strategy), '') not like '%%preview%%'
          and coalesce(lower(p.strategy), '') not like '%%simulation%%'
          and coalesce(lower(p.strategy), '') not like '%%test%%'
          and coalesce(lower(p.strategy), '') not like '%%fixture%%'
          and coalesce(lower(p.strategy), '') not like '%%synthetic%%'
        order by p.prediction_issue::bigint desc, p.created_at desc, p.id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
        (target,),
        sqlite_sql="""
        select {columns}
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.issue is not null
          and p.prediction_issue is not null
          and p.issue not glob '*[^0-9]*'
          and p.prediction_issue not glob '*[^0-9]*'
          and length(p.issue) >= {min_issue_length}
          and length(p.prediction_issue) >= {min_issue_length}
          and p.issue not like '99%'
          and p.prediction_issue not like '99%'
          and upper(p.issue) not like 'TEST%'
          and upper(p.prediction_issue) not like 'TEST%'
          and cast(p.prediction_issue as integer) = cast(p.issue as integer) + 1
          and cast(p.prediction_issue as integer) <= cast(? as integer)
          and p.recommend_numbers is not null
          and p.recommend_numbers not in ('', '[]')
          and coalesce(p.winning_numbers, o.numbers) is not null
          and coalesce(p.winning_numbers, o.numbers) not in ('', '[]')
          and coalesce(lower(p.strategy), '') not like '%preview%'
          and coalesce(lower(p.strategy), '') not like '%simulation%'
          and coalesce(lower(p.strategy), '') not like '%test%'
          and coalesce(lower(p.strategy), '') not like '%fixture%'
          and coalesce(lower(p.strategy), '') not like '%synthetic%'
        order by cast(p.prediction_issue as integer) desc, p.created_at desc, p.id desc
        limit 1
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
    )
    if not rows:
        return None
    record = _row_to_prediction(rows[0])
    record["read_layer"] = {
        "data_source": "database",
        "table_name": "prediction_history",
        "query_name": "latest_verified_prediction_at_or_before",
        "production_filtered": True,
    }
    return _enrich_prediction_metadata(record)


def update_prediction_history_result(actual: dict) -> dict:
    _ensure_initialized()
    issue = str(actual.get("issue"))
    winning_numbers = [int(n) for n in actual.get("numbers") or []]
    actual_super = actual.get("super_number")
    if len(winning_numbers) != 20:
        return {"status": "waiting_draw", "updated": 0, "issue": issue}
    updated = 0
    verified_items = []
    learned_issues = _learned_prediction_issues()
    for item in _prediction_records_for_target_issue(issue):
        recommended = [int(n) for n in item.get("recommend_numbers") or []]
        matched_numbers = sorted(set(recommended) & set(winning_numbers))
        missed_numbers = [number for number in recommended if number not in set(winning_numbers)]
        hit_count = len(matched_numbers)
        prediction_count = len(recommended)
        super_hit = bool(actual_super is not None and item.get("super_number") == actual_super)
        three_star_hit = len(set(item.get("three_star") or []) & set(winning_numbers)) >= 3
        four_star_hit = len(set(item.get("four_star") or []) & set(winning_numbers)) >= 4
        accuracy = round(hit_count / max(1, prediction_count), 4)
        winning_model = _winning_model(item.get("model_scores") or {}, winning_numbers)
        model_score = _model_score(item.get("model_scores") or {}, winning_model)
        verified_at = _now()
        learning_used = bool(item.get("learning_used") or str(item.get("prediction_issue") or "") in learned_issues)
        params = (
            _json_dumps(winning_numbers),
            hit_count,
            super_hit,
            three_star_hit,
            four_star_hit,
            accuracy,
            winning_model,
            "verified",
            issue,
            verified_at,
            _json_dumps(matched_numbers),
            _json_dumps(missed_numbers),
            prediction_count,
            accuracy,
            super_hit,
            "prediction_lifecycle_v1",
            learning_used,
            model_score,
            verified_at,
            item.get("id"),
        )
        if _cloud_enabled():
            try:
                with _cloud_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            update prediction_history
                            set winning_numbers = %s::jsonb,
                                hit_count = %s,
                                super_hit = %s,
                                three_star_hit = %s,
                                four_star_hit = %s,
                                accuracy = %s,
                                winning_model = %s,
                                prediction_status = %s,
                                verified_issue = %s,
                                verified_at = %s,
                                matched_numbers = %s::jsonb,
                                missed_numbers = %s::jsonb,
                                prediction_count = %s,
                                hit_rate = %s,
                                super_number_hit = %s,
                                verification_version = %s,
                                learning_used = %s,
                                model_score = %s,
                                updated_at = %s
                            where id = %s
                            """,
                            params,
                            prepare=False,
                        )
                    conn.commit()
                updated += 1
                verified_items.append(
                    _verification_summary(
                        item,
                        issue,
                        matched_numbers,
                        missed_numbers,
                        hit_count,
                        prediction_count,
                        accuracy,
                        super_hit,
                        verified_at,
                    )
                )
                continue
            except Exception:
                logger.exception("cloud prediction_history result update failed")
        try:
            with _sqlite_connection() as conn:
                conn.execute(
                    """
                    update prediction_history
                    set winning_numbers = ?,
                        hit_count = ?,
                        super_hit = ?,
                        three_star_hit = ?,
                        four_star_hit = ?,
                        accuracy = ?,
                        winning_model = ?,
                        prediction_status = ?,
                        verified_issue = ?,
                        verified_at = ?,
                        matched_numbers = ?,
                        missed_numbers = ?,
                        prediction_count = ?,
                        hit_rate = ?,
                        super_number_hit = ?,
                        verification_version = ?,
                        learning_used = ?,
                        model_score = ?,
                        updated_at = ?
                    where id = ?
                    """,
                    (
                        params[0],
                        hit_count,
                        1 if super_hit else 0,
                        1 if three_star_hit else 0,
                        1 if four_star_hit else 0,
                        accuracy,
                        winning_model,
                        params[7],
                        params[8],
                        params[9],
                        params[10],
                        params[11],
                        params[12],
                        params[13],
                        1 if super_hit else 0,
                        params[15],
                        1 if params[16] else 0,
                        params[17],
                        params[18],
                        item.get("id"),
                    ),
                )
            updated += 1
            verified_items.append(
                _verification_summary(
                    item,
                    issue,
                    matched_numbers,
                    missed_numbers,
                    hit_count,
                    prediction_count,
                    accuracy,
                    super_hit,
                    verified_at,
                )
            )
        except Exception:
            logger.exception("sqlite prediction_history result update failed")
    if updated:
        _invalidate_prediction_stats_cache()
        try:
            from services.operations_center import record_operation_event

            record_operation_event(
                component="prediction",
                event_type="prediction_verified",
                status="ok",
                issue=issue,
                message=_json_dumps(
                    {
                        "event_type": "prediction_verified",
                        "target_issue": issue,
                        "updated": updated,
                        "verified_count": len(verified_items),
                    }
                ),
            )
        except Exception:
            logger.exception("prediction verified event recording failed")
    return {
        "status": "ok",
        "updated": updated,
        "issue": issue,
        "prediction_status": "verified" if updated else "waiting_draw",
        "learning_used": False,
        "results": verified_items,
    }


def _winning_model(model_scores: dict, winning_numbers: list[int]) -> str | None:
    best_model = None
    best_hits = -1
    winning_set = set(winning_numbers)
    for model, payload in (model_scores or {}).items():
        candidates = payload.get("candidate_numbers") if isinstance(payload, dict) else []
        hits = len(set(_as_int_list(candidates)) & winning_set)
        if hits > best_hits:
            best_model = model
            best_hits = hits
    return best_model


def _model_score(model_scores: dict, model_name: str | None) -> float | None:
    if not model_name:
        return None
    payload = (model_scores or {}).get(model_name)
    if isinstance(payload, dict):
        try:
            return float(payload.get("confidence") or 0)
        except Exception:
            return None
    return None


def _learned_prediction_issues(limit: int = 1000) -> set[str]:
    cached = _LEARNED_ISSUES_CACHE.get("payload")
    expires_at = float(_LEARNED_ISSUES_CACHE.get("expires_at") or 0)
    if isinstance(cached, set) and time.monotonic() < expires_at:
        return set(cached)
    try:
        from database.learning_store import get_learned_live_target_issues

        learned = get_learned_live_target_issues(limit)
        _LEARNED_ISSUES_CACHE["payload"] = set(learned)
        _LEARNED_ISSUES_CACHE["expires_at"] = time.monotonic() + LEARNED_ISSUES_TTL_SECONDS
        return learned
    except Exception:
        logger.exception("failed to load learned prediction issues")
        return set()


def _verification_summary(
    item: dict,
    issue: str,
    matched_numbers: list[int],
    missed_numbers: list[int],
    hit_count: int,
    prediction_count: int,
    hit_rate: float,
    super_hit: bool,
    verified_at: str,
) -> dict:
    return {
        "id": item.get("id"),
        "issue": item.get("issue"),
        "prediction_issue": item.get("prediction_issue"),
        "target_issue": item.get("prediction_issue"),
        "prediction_status": "verified",
        "verified_issue": issue,
        "verified_at": verified_at,
        "matched_numbers": matched_numbers,
        "missed_numbers": missed_numbers,
        "hit_count": hit_count,
        "prediction_count": prediction_count,
        "hit_rate": hit_rate,
        "super_number_hit": super_hit,
        "learning_used": False,
    }


def _as_int_list(values: Any) -> list[int]:
    return _normalize_numbers(values)


def get_prediction_history_statistics(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    cache_key = str(limit)
    now = time.monotonic()
    cached_payload = (_PREDICTION_STATS_CACHE.get("payload") or {}).get(cache_key)
    expires_at = float((_PREDICTION_STATS_CACHE.get("expires_at") or {}).get(cache_key) or 0)
    if isinstance(cached_payload, dict) and expires_at > now:
        payload = deepcopy(cached_payload)
        payload["cache"] = {
            "status": "hit",
            "ttl_seconds": PREDICTION_STATS_TTL_SECONDS,
            "expires_in_seconds": round(expires_at - now, 3),
        }
        return payload
    start = time.perf_counter()
    all_records = get_prediction_history_records(limit)
    records = [item for item in all_records if item.get("winning_numbers")]
    total = len(records)
    learned_issues = _learned_prediction_issues()
    waiting_prediction_count = sum(
        1 for item in all_records if item.get("prediction_status") in ("pending", "waiting_draw")
    )
    verified_prediction_count = sum(1 for item in all_records if item.get("prediction_status") == "verified")
    verified_waiting_learning = sum(
        1
        for item in all_records
        if item.get("prediction_status") == "verified"
        and not item.get("learning_used")
        and str(item.get("prediction_issue") or "") not in learned_issues
    )
    learned_records = [
        item
        for item in all_records
        if item.get("learning_used") or str(item.get("prediction_issue") or "") in learned_issues
    ]
    last_learning_time = max((item.get("updated_at") for item in learned_records if item.get("updated_at")), default=None)
    if not total:
        payload = {
            "status": "empty",
            "message": "尚未累積 AI 預測紀錄，系統已開始保存後續推薦。",
            "sample_size": 0,
            "three_star_rate": 0,
            "four_star_rate": 0,
            "five_star_rate": 0,
            "super_hit_rate": 0,
            "average_hits": 0,
            "prediction_success_rate": 0,
            "success_threshold": 1,
            "success_definition": "hit_count >= 1 among verified predictions",
            "verified_rate": 0,
            "three_star_or_better_rate": 0,
            "average_hit": 0,
            "average_hit_last_30": 0,
            "average_hit_last_100": 0,
            "waiting_prediction_count": waiting_prediction_count,
            "verified_prediction_count": verified_prediction_count,
            "pending_learning": verified_waiting_learning,
            "verified_waiting_learning": verified_waiting_learning,
            "last_learning_time": last_learning_time,
        }
        payload["cache"] = {
            "status": "miss",
            "ttl_seconds": PREDICTION_STATS_TTL_SECONDS,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
        }
        (_PREDICTION_STATS_CACHE.setdefault("payload", {}))[cache_key] = deepcopy(payload)
        (_PREDICTION_STATS_CACHE.setdefault("expires_at", {}))[cache_key] = time.monotonic() + PREDICTION_STATS_TTL_SECONDS
        return payload
    last_30 = records[:30]
    last_100 = records[:100]
    payload = {
        "status": "ok",
        "sample_size": total,
        "three_star_rate": round(sum(1 for item in records if item.get("three_star_hit")) / total * 100, 2),
        "four_star_rate": round(sum(1 for item in records if item.get("four_star_hit")) / total * 100, 2),
        "five_star_rate": round(sum(1 for item in records if (item.get("hit_count") or 0) >= 5) / total * 100, 2),
        "super_hit_rate": round(sum(1 for item in records if item.get("super_hit")) / total * 100, 2),
        "average_hits": round(sum(item.get("hit_count") or 0 for item in records) / total, 2),
        "prediction_success_rate": round(sum(1 for item in records if (item.get("hit_count") or 0) > 0) / total * 100, 2),
        "success_threshold": 1,
        "success_definition": "hit_count >= 1 among verified predictions",
        "verified_rate": round(total / max(1, len(all_records)) * 100, 2),
        "three_star_or_better_rate": round(sum(1 for item in records if (item.get("hit_count") or 0) >= 3) / total * 100, 2),
        "average_hit": round(sum(item.get("hit_count") or 0 for item in records) / total, 2),
        "average_hit_last_30": round(sum(item.get("hit_count") or 0 for item in last_30) / max(1, len(last_30)), 2),
        "average_hit_last_100": round(sum(item.get("hit_count") or 0 for item in last_100) / max(1, len(last_100)), 2),
        "waiting_prediction_count": waiting_prediction_count,
        "verified_prediction_count": verified_prediction_count,
        "pending_learning": verified_waiting_learning,
        "verified_waiting_learning": verified_waiting_learning,
        "last_learning_time": last_learning_time,
    }
    payload["cache"] = {
        "status": "miss",
        "ttl_seconds": PREDICTION_STATS_TTL_SECONDS,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
    }
    (_PREDICTION_STATS_CACHE.setdefault("payload", {}))[cache_key] = deepcopy(payload)
    (_PREDICTION_STATS_CACHE.setdefault("expires_at", {}))[cache_key] = time.monotonic() + PREDICTION_STATS_TTL_SECONDS
    return payload
