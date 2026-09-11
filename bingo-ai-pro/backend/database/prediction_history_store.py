from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from config.production_scope import (
    get_production_generation,
    get_production_start_issue,
    is_issue_in_current_generation,
)
from config.release import FEATURE_VERSION, GIT_COMMIT_HASH, MODEL_VERSION, RELEASE_VERSION

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
_CARD_TWO_HISTORY_TIMING_LIMIT = 20
_CARD_TWO_HISTORY_TIMING_LOCK = threading.Lock()
_CARD_TWO_HISTORY_TIMINGS: list[dict[str, Any]] = []
_CARD_TWO_QUERY_TIMING: ContextVar[dict[str, Any] | None] = ContextVar("card_two_query_timing", default=None)
_CARD_TWO_DASHBOARD_CONNECTION: ContextVar[Any | None] = ContextVar("card_two_dashboard_connection", default=None)
_CARD_TWO_DASHBOARD_CONNECTION_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "card_two_dashboard_connection_state",
    default=None,
)
_CARD_TWO_DASHBOARD_EXECUTION_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "card_two_dashboard_execution_context",
    default=None,
)
_DIAGNOSTIC_QUERY_EVENTS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "diagnostic_query_events",
    default=None,
)
_DIAGNOSTIC_QUERY_LABEL: ContextVar[str | None] = ContextVar("diagnostic_query_label", default=None)
_DIAGNOSTIC_QUERY_STARTED_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "diagnostic_query_started_event",
    default=None,
)
_DIAGNOSTIC_CARD_TWO_TIMING_EVENTS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "diagnostic_card_two_timing_events",
    default=None,
)

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
TRACEABILITY_COLUMNS = {
    "production_generation": ("integer default 2", "integer default 2"),
    "production_valid": ("boolean default true", "integer default 1"),
    "release_version": ("text", "text"),
    "git_commit_hash": ("text", "text"),
    "model_version": ("text", "text"),
    "feature_version": ("text", "text"),
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
    if not is_issue_in_current_generation(issue):
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
    if not is_issue_in_current_generation(based_on) or not is_issue_in_current_generation(target):
        return False, "outside_current_generation"
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


def _dashboard_read_connection():
    from database.postgres import dashboard_read_connection

    return dashboard_read_connection()


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
                    for column, (cloud_type, _) in TRACEABILITY_COLUMNS.items():
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
            for column, (_, sqlite_type) in TRACEABILITY_COLUMNS.items():
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
        int(item.get("production_generation") or get_production_generation()),
        bool(item.get("production_valid", True)),
        item.get("release_version") or RELEASE_VERSION,
        item.get("git_commit_hash") or GIT_COMMIT_HASH,
        item.get("model_version") or MODEL_VERSION,
        item.get("feature_version") or FEATURE_VERSION,
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
                            learning_used, production_generation, production_valid,
                            release_version, git_commit_hash, model_version, feature_version,
                            updated_at
                        )
                        values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
                                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                                %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
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
                            production_generation = excluded.production_generation,
                            production_valid = excluded.production_valid,
                            release_version = excluded.release_version,
                            git_commit_hash = excluded.git_commit_hash,
                            model_version = excluded.model_version,
                            feature_version = excluded.feature_version,
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
                    learning_used, production_generation, production_valid,
                    release_version, git_commit_hash, model_version, feature_version,
                    updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    production_generation = excluded.production_generation,
                    production_valid = excluded.production_valid,
                    release_version = excluded.release_version,
                    git_commit_hash = excluded.git_commit_hash,
                    model_version = excluded.model_version,
                    feature_version = excluded.feature_version,
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


def _execute_cloud_query(conn, sql: str, params: tuple = (), timing: dict[str, Any] | None = None) -> list[Any]:
    with conn.cursor() as cur:
        if timing is not None:
            timing["transaction_status_before"] = _transaction_status(conn)
        context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
        if context is not None and timing and timing.get("query_tag") == "card_two_history.main_query":
            _execute_dashboard_context_probe(conn, "select1_a")
            context["active_components_before_card_two"] = _active_dashboard_component_names()
        execute_started = time.perf_counter()
        started_event = _DIAGNOSTIC_QUERY_STARTED_EVENT.get()
        if started_event is not None:
            started_event.set()
        try:
            cur.execute(sql, params, prepare=False)
        finally:
            execute_finished = time.perf_counter()
            _record_diagnostic_query_event(conn, timing, execute_started, execute_finished)
        if timing is not None:
            timing["execute_ms"] = round((execute_finished - execute_started) * 1000, 2)
            timing["transaction_status_after"] = _transaction_status(conn)
        fetch_started = time.perf_counter()
        rows = cur.fetchall()
        if timing is not None:
            timing["fetch_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
            timing["row_count"] = len(rows)
        if context is not None and timing and timing.get("query_tag") == "card_two_history.main_query":
            context["card_two_execute_ms"] = timing.get("execute_ms")
            context["fetch_ms"] = timing.get("fetch_ms")
            context["status_before_execute"] = timing.get("transaction_status_before")
            context["status_after_execute"] = timing.get("transaction_status_after")
            context["card_two_fetch_finished_at"] = time.perf_counter()
            context["card_two_execute_window_ms"] = round((execute_finished - execute_started) * 1000, 2)
            context["active_components_after_card_two"] = _active_dashboard_component_names()
            context["overlapping_components_card_two"] = _overlapping_dashboard_components(
                execute_started,
                execute_finished,
            )
            _execute_dashboard_context_probe(conn, "select1_b")
        return rows


def _record_diagnostic_query_event(
    conn: Any,
    timing: dict[str, Any] | None,
    started: float,
    finished: float,
) -> None:
    events = _DIAGNOSTIC_QUERY_EVENTS.get()
    if events is None:
        return
    label = _DIAGNOSTIC_QUERY_LABEL.get() or (timing or {}).get("query_tag") or "unknown"
    events.append(
        {
            "label": label,
            "query_tag": (timing or {}).get("query_tag"),
            "thread_id": threading.get_ident(),
            "connection_hash": _connection_hash(conn),
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "started_at": started,
            "finished_at": finished,
            "execute_ms": round((finished - started) * 1000, 2),
        }
    )


def _active_dashboard_component_names() -> list[str]:
    try:
        from services.player_dashboard import active_dashboard_components

        return sorted(
            {
                str(item.get("component"))
                for item in active_dashboard_components()
                if item.get("component") and item.get("component") != "card_two_history"
            }
        )
    except Exception:
        return []


def _overlapping_dashboard_components(started: float, finished: float) -> list[dict[str, Any]]:
    try:
        from services.player_dashboard import active_dashboard_components

        overlap_ms = round((finished - started) * 1000, 2)
        return [
            {
                "component": item.get("component"),
                "thread_id": item.get("thread_id"),
                "overlap_ms": overlap_ms,
            }
            for item in active_dashboard_components()
            if item.get("component") and item.get("component") != "card_two_history"
        ]
    except Exception:
        return []


def _execute_dashboard_context_probe(conn: Any, name: str) -> None:
    context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
    if context is None:
        return
    with conn.cursor() as cur:
        before = _transaction_status(conn)
        started = time.perf_counter()
        cur.execute("select 1", prepare=False)
        execute_ms = round((time.perf_counter() - started) * 1000, 2)
        after = _transaction_status(conn)
        cur.fetchall()
    context[f"{name}_execute_ms"] = execute_ms
    context[f"{name}_status_before"] = before
    context[f"{name}_status_after"] = after
    context[f"{name}_active_components"] = _active_dashboard_component_names()


def _record_shared_connection_acquire_timing(timing: dict[str, Any] | None, state: dict[str, Any] | None) -> None:
    if timing is None:
        return
    if not state:
        timing["connect_ms"] = 0.0
        timing["pool_acquire_ms"] = 0.0
        return
    timing["pool_acquire_ms"] = state.get("connect_ms", 0.0)
    if state.get("connect_reported"):
        timing["connect_ms"] = 0.0
        timing["connection_reused"] = True
        return
    timing["connect_ms"] = state.get("connect_ms", 0.0)
    state["connect_reported"] = True


def _connection_hash(conn: Any) -> str | None:
    try:
        raw = f"{type(conn).__name__}:{id(conn)}"
    except Exception:
        return None
    return sha256(raw.encode("utf-8")).hexdigest()[:12]


def _transaction_status(conn: Any) -> str | None:
    try:
        status = conn.info.transaction_status
    except Exception:
        return None
    mapping = {
        0: "IDLE",
        1: "ACTIVE",
        2: "INTRANS",
        3: "INERROR",
        4: "UNKNOWN",
    }
    value = getattr(status, "value", status)
    return mapping.get(value, str(status))


def _record_connection_metadata(timing: dict[str, Any] | None, conn: Any, state: dict[str, Any] | None) -> None:
    if timing is None:
        return
    timing["connection_hash"] = _connection_hash(conn)
    if state and state.get("opened_at") is not None:
        timing["connection_age_ms"] = round((time.perf_counter() - state["opened_at"]) * 1000, 2)
    try:
        timing["backend_pid"] = conn.info.backend_pid
    except Exception:
        timing["backend_pid"] = None


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    timing = _CARD_TWO_QUERY_TIMING.get()
    total_started = time.perf_counter() if timing is not None else None
    try:
        shared_state = _CARD_TWO_DASHBOARD_CONNECTION_STATE.get()
        shared_conn = _CARD_TWO_DASHBOARD_CONNECTION.get()
        if shared_state is not None:
            if not shared_state.get("cloud_available", True) or shared_conn is None:
                raise RuntimeError("dashboard read connection unavailable")
            _record_shared_connection_acquire_timing(timing, shared_state)
            _record_connection_metadata(timing, shared_conn, shared_state)
            return _execute_cloud_query(shared_conn, sql, params, timing)

        connect_started = time.perf_counter()
        conn_context = _cloud_connection()
        if timing is not None:
            timing["connect_ms"] = round((time.perf_counter() - connect_started) * 1000, 2)
            timing["pool_acquire_ms"] = timing["connect_ms"]
        with conn_context as conn:
            _record_connection_metadata(timing, conn, None)
            return _execute_cloud_query(conn, sql, params, timing)
    except Exception as exc:
        shared_state = _CARD_TWO_DASHBOARD_CONNECTION_STATE.get()
        if shared_state is not None:
            shared_state["cloud_available"] = False
        if timing is not None:
            timing["result"] = "failed"
            timing["error_type"] = type(exc).__name__
        raise
    finally:
        if timing is not None and total_started is not None:
            timing.setdefault("backend", "postgres")
            timing.setdefault("result", "success")
            timing["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _prediction_history_summary_cloud_sql() -> str:
    return """
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
            """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH)


def _diagnostic_card_two_signature(rows: list[Any]) -> dict[str, Any]:
    ids = [row[0] for row in rows]
    payload = _json_dumps([[str(value) for value in row] for row in rows])
    return {
        "row_ids": ids,
        "row_signature": sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


def _diagnostic_statement(conn: Any, name: str, sql: str, params: tuple = ()) -> dict[str, Any]:
    before = _transaction_status(conn)
    cursor_create_started = time.perf_counter()
    cursor_context = conn.cursor()
    cursor_create_finished = time.perf_counter()
    close_started = None
    close_finished = None
    with cursor_context as cur:
        execute_started = time.perf_counter()
        cur.execute(sql, params, prepare=False)
        execute_finished = time.perf_counter()
        after = _transaction_status(conn)
        fetch_started = time.perf_counter()
        rows = cur.fetchall()
        fetch_finished = time.perf_counter()
        close_started = time.perf_counter()
    close_finished = time.perf_counter()
    result = {
        "statement": name,
        "thread_id": threading.get_ident(),
        "connection_hash": _connection_hash(conn),
        "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
        "cursor_create_ms": round((cursor_create_finished - cursor_create_started) * 1000, 2),
        "execute_ms": round((execute_finished - execute_started) * 1000, 2),
        "fetch_ms": round((fetch_finished - fetch_started) * 1000, 2),
        "cursor_close_ms": round((close_finished - close_started) * 1000, 2) if close_started is not None else None,
        "row_count": len(rows),
        "transaction_status_before": before,
        "transaction_status_after": after,
        "timeline": {
            "cursor_create_start": cursor_create_started,
            "cursor_create_end": cursor_create_finished,
            "execute_call_start": execute_started,
            "execute_call_end": execute_finished,
            "fetch_start": fetch_started,
            "fetch_end": fetch_finished,
            "cursor_close_start": close_started,
            "cursor_close_end": close_finished,
        },
    }
    if name == "card_two":
        result.update(_diagnostic_card_two_signature(rows))
    return result


def _diagnostic_pool_statement_sequence(statements: list[tuple[str, str, tuple]]) -> dict[str, Any]:
    acquire_started = time.perf_counter()
    release_started = None
    release_finished = None
    with _dashboard_read_connection() as conn:
        acquire_finished = time.perf_counter()
        result = {
            "pool_acquire_start": acquire_started,
            "pool_acquire_end": acquire_finished,
            "pool_acquire_ms": round((acquire_finished - acquire_started) * 1000, 2),
            "connection_obtained": acquire_finished,
            "connection_hash": _connection_hash(conn),
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "checkout_status": _transaction_status(conn),
            "statements": [],
        }
        try:
            for name, sql, params in statements:
                result["statements"].append(_diagnostic_statement(conn, name, sql, params))
            result["final_status"] = _transaction_status(conn)
        finally:
            try:
                conn.rollback()
            except Exception:
                logger.warning("card two stepwise diagnostic rollback failed", exc_info=True)
            release_started = time.perf_counter()
    release_finished = time.perf_counter()
    result["connection_release_start"] = release_started
    result["connection_release_end"] = release_finished
    result["connection_release_ms"] = (
        round((release_finished - release_started) * 1000, 2)
        if release_started is not None
        else None
    )
    return result


def _diagnostic_main_query_sample(*, reset_before: bool = False, connection_factory=None) -> dict[str, Any]:
    connection_factory = connection_factory or _dashboard_read_connection
    acquire_started = time.perf_counter()
    release_started = None
    release_finished = None
    with connection_factory() as conn:
        acquire_finished = time.perf_counter()
        if reset_before:
            conn.rollback()
        result = {
            "pool_acquire_start": acquire_started,
            "pool_acquire_end": acquire_finished,
            "pool_acquire_ms": round((acquire_finished - acquire_started) * 1000, 2),
            "connection_obtained": acquire_finished,
            "connection_hash": _connection_hash(conn),
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "checkout_status": _transaction_status(conn),
            "reset_before": reset_before,
            "statement": _diagnostic_statement(
                conn,
                "card_two",
                _prediction_history_summary_cloud_sql(),
                (100,),
            ),
        }
        try:
            conn.rollback()
        except Exception:
            logger.warning("card two main query diagnostic rollback failed", exc_info=True)
        release_started = time.perf_counter()
    release_finished = time.perf_counter()
    result["connection_release_start"] = release_started
    result["connection_release_end"] = release_finished
    result["connection_release_ms"] = (
        round((release_finished - release_started) * 1000, 2)
        if release_started is not None
        else None
    )
    return result


def _diagnostic_explain_card_two_main_query() -> dict[str, Any]:
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _prediction_history_summary_cloud_sql()
    try:
        with _dashboard_read_connection() as conn:
            return _diagnostic_explain_card_two_main_query_on_conn(conn)
    except Exception as exc:
        return {
            "available": False,
            "error_type": type(exc).__name__,
        }


def _diagnostic_explain_card_two_main_query_on_conn(conn: Any) -> dict[str, Any]:
    explain_sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + _prediction_history_summary_cloud_sql()
    with conn.cursor() as cur:
        started = time.perf_counter()
        cur.execute(explain_sql, (100,), prepare=False)
        rows = cur.fetchall()
        wall_ms = round((time.perf_counter() - started) * 1000, 2)
    payload = rows[0][0] if rows else None
    root = payload[0] if isinstance(payload, list) and payload else {}
    plan = root.get("Plan", {}) if isinstance(root, dict) else {}
    return {
        "available": True,
        "wall_ms": wall_ms,
        "planning_ms": root.get("Planning Time") if isinstance(root, dict) else None,
        "execution_ms": root.get("Execution Time") if isinstance(root, dict) else None,
        "actual_rows": plan.get("Actual Rows"),
        "shared_hit_blocks": plan.get("Shared Hit Blocks"),
        "shared_read_blocks": plan.get("Shared Read Blocks"),
        "node_type": plan.get("Node Type"),
    }


def _diagnostic_wait_state(pid: int | None) -> dict[str, Any]:
    if not pid:
        return {"available": False, "reason": "missing backend pid"}
    try:
        with _dashboard_read_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select state, wait_event_type, wait_event,
                           xact_start is not null as has_xact_start,
                           query_start is not null as has_query_start
                    from pg_stat_activity
                    where pid = %s
                    """,
                    (pid,),
                    prepare=False,
                )
                rows = cur.fetchall()
        if not rows:
            return {"available": True, "rows": []}
        row = rows[0]
        return {
            "available": True,
            "state": row[0],
            "wait_event_type": row[1],
            "wait_event": row[2],
            "has_xact_start": row[3],
            "has_query_start": row[4],
        }
    except Exception as exc:
        return {
            "available": False,
            "error_type": type(exc).__name__,
        }


@contextmanager
def _diagnostic_autocommit_read_connection():
    from database import postgres
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=postgres.DATABASE_URL or "",
        kwargs={
            "connect_timeout": postgres._connect_timeout_seconds(),
            "autocommit": True,
        },
        min_size=0,
        max_size=1,
        open=False,
        timeout=postgres.DASHBOARD_READ_POOL_ACQUIRE_TIMEOUT_SECONDS,
        name="card-two-autocommit-diagnostic",
    )
    pool.open(wait=False)
    try:
        with pool.connection(timeout=postgres.DASHBOARD_READ_POOL_ACQUIRE_TIMEOUT_SECONDS) as conn:
            yield conn
    finally:
        pool.close(timeout=1.0)


def _connection_endpoint_status(conninfo: str | None) -> dict[str, Any]:
    if not conninfo:
        return {
            "configured": False,
            "hostname_classification": "unknown",
            "port": None,
            "sslmode": None,
            "definitely_transaction_pooler": False,
        }
    try:
        parsed = urlsplit(conninfo)
        hostname = parsed.hostname or ""
        port = parsed.port
        sslmode = (parse_qs(parsed.query).get("sslmode") or [None])[0]
    except Exception:
        return {
            "configured": True,
            "hostname_classification": "unknown",
            "port": None,
            "sslmode": None,
            "definitely_transaction_pooler": False,
        }
    classification = "unknown"
    if hostname.startswith("db.") and "supabase" in hostname:
        classification = "direct"
    elif "pooler.supabase" in hostname and port == 6543:
        classification = "transaction pooler"
    elif "pooler.supabase" in hostname and port == 5432:
        classification = "session pooler"
    return {
        "configured": True,
        "hostname_classification": classification,
        "port": port,
        "sslmode": sslmode,
        "definitely_transaction_pooler": classification == "transaction pooler",
    }


def _first_env_value(names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        value = os.getenv(name)
        if value:
            return name, value
    return None, None


@contextmanager
def _diagnostic_psycopg_connection(conninfo: str):
    import psycopg
    from database import postgres

    conn = psycopg.connect(conninfo, connect_timeout=postgres._connect_timeout_seconds())
    try:
        yield conn
    finally:
        conn.close()


def _run_card_two_roundtrip_sequence(statements: list[tuple[str, str, tuple]], connection_factory=None) -> dict[str, Any]:
    connection_factory = connection_factory or _dashboard_read_connection
    acquire_started = time.perf_counter()
    with connection_factory() as conn:
        pool_acquire_ms = round((time.perf_counter() - acquire_started) * 1000, 2)
        result = {
            "checkout_status": _transaction_status(conn),
            "pool_acquire_ms": pool_acquire_ms,
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "connection_hash": _connection_hash(conn),
            "statements": [],
        }
        try:
            for name, sql, params in statements:
                result["statements"].append(_diagnostic_statement(conn, name, sql, params))
            result["final_status"] = _transaction_status(conn)
            return result
        finally:
            try:
                conn.rollback()
            except Exception:
                logger.warning("card two roundtrip diagnostic rollback failed", exc_info=True)


def run_card_two_roundtrip_diagnostic(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    card_two = ("card_two", _prediction_history_summary_cloud_sql(), (100,))
    select1 = ("select1", "select 1", ())
    sequence_a = [select1, select1, card_two, select1]
    sequence_b = [card_two, select1, card_two, select1]
    return {
        "status": "ok",
        "sequence_a": [_run_card_two_roundtrip_sequence(sequence_a) for _ in range(repetitions)],
        "sequence_b": [_run_card_two_roundtrip_sequence(sequence_b) for _ in range(repetitions)],
    }


def run_card_two_autocommit_roundtrip_diagnostic(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    card_two = ("card_two", _prediction_history_summary_cloud_sql(), (100,))
    select1 = ("select1", "select 1", ())
    sequence = [select1, select1, card_two, select1]
    control = [_run_card_two_roundtrip_sequence(sequence) for _ in range(repetitions)]
    autocommit = [
        _run_card_two_roundtrip_sequence(sequence, _diagnostic_autocommit_read_connection)
        for _ in range(repetitions)
    ]
    control_card_two = [item["statements"][2] for item in control]
    autocommit_card_two = [item["statements"][2] for item in autocommit]
    semantic_equivalence = all(
        left.get("row_count") == right.get("row_count")
        and left.get("row_ids") == right.get("row_ids")
        and left.get("row_signature") == right.get("row_signature")
        for left, right in zip(control_card_two, autocommit_card_two)
    )
    return {
        "status": "ok",
        "control_autocommit": False,
        "autocommit_test_mode": True,
        "control": control,
        "autocommit": autocommit,
        "semantic_equivalence": semantic_equivalence,
    }


def run_card_two_stepwise_latency_benchmark(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    card_two = ("card_two", _prediction_history_summary_cloud_sql(), (100,))
    select1 = ("select1", "select 1", ())
    sequential_sequence = [select1, card_two, select1, card_two, select1, card_two, select1]

    reused_statements = [card_two for _ in range(5)]
    reused_connection = _diagnostic_pool_statement_sequence(reused_statements)
    first_pid = reused_connection.get("backend_pid")

    autocommit: dict[str, Any]
    try:
        autocommit = {
            "executed": True,
            "samples": [
                _diagnostic_main_query_sample(
                    reset_before=False,
                    connection_factory=_diagnostic_autocommit_read_connection,
                )
                for _ in range(repetitions)
            ],
        }
    except Exception as exc:
        autocommit = {
            "executed": False,
            "error_type": type(exc).__name__,
            "samples": [],
        }

    return {
        "status": "ok",
        "repetitions": repetitions,
        "timer_boundary": {
            "main_query_execute_ms": "Python wall-clock duration of cur.execute(sql, params, prepare=False)",
            "main_query_fetch_ms": "Python wall-clock duration of cur.fetchall() after execute returns",
            "pool_acquire_ms": "connection checkout/acquire measured before cursor execution",
            "cursor_create_ms": "Python wall-clock duration of conn.cursor() creation",
            "connection_release_ms": "Python wall-clock duration leaving the connection context",
        },
        "main_query_execute_calls": 1,
        "main_query_fetch_calls": 1,
        "sequential_probe": [
            _diagnostic_pool_statement_sequence(sequential_sequence)
            for _ in range(repetitions)
        ],
        "fresh_connection": [
            _diagnostic_main_query_sample(reset_before=False)
            for _ in range(repetitions)
        ],
        "reused_connection": reused_connection,
        "transaction_current": [
            _diagnostic_main_query_sample(reset_before=False)
            for _ in range(repetitions)
        ],
        "transaction_reset": [
            _diagnostic_main_query_sample(reset_before=True)
            for _ in range(repetitions)
        ],
        "autocommit": autocommit,
        "postgres_server_execution": [
            _diagnostic_explain_card_two_main_query()
            for _ in range(repetitions)
        ],
        "wait_state": _diagnostic_wait_state(first_pid),
    }


def _run_connection_path_sequences(connection_factory, repetitions: int) -> list[dict[str, Any]]:
    card_two = ("card_two", _prediction_history_summary_cloud_sql(), (100,))
    select1 = ("select1", "select 1", ())
    sequence = [select1, select1, card_two, select1]
    return [_run_card_two_roundtrip_sequence(sequence, connection_factory) for _ in range(repetitions)]


def _safe_connection_path_sequences(connection_factory, repetitions: int) -> dict[str, Any]:
    try:
        return {
            "error_type": None,
            "sequences": _run_connection_path_sequences(connection_factory, repetitions),
        }
    except Exception as exc:
        return {
            "error_type": type(exc).__name__,
            "sequences": [],
        }


def run_card_two_connection_path_benchmark(repetitions: int = 7) -> dict[str, Any]:
    from database import postgres

    repetitions = max(1, min(int(repetitions or 7), 7))
    direct_name, direct_url = _first_env_value(
        (
            "DIRECT_DATABASE_URL",
            "DATABASE_DIRECT_URL",
            "SUPABASE_DIRECT_DATABASE_URL",
        )
    )
    session_name, session_url = _first_env_value(
        (
            "SESSION_POOLER_DATABASE_URL",
            "DATABASE_SESSION_POOLER_URL",
            "SUPABASE_SESSION_POOLER_DATABASE_URL",
        )
    )
    result: dict[str, Any] = {
        "status": "ok",
        "current_connection_path": _connection_endpoint_status(postgres.DATABASE_URL),
        "direct_connection": {
            "available": bool(direct_url),
            "env_var": direct_name,
            "endpoint": _connection_endpoint_status(direct_url),
            "sequences": [],
            "error_type": None,
        },
        "session_pooler": {
            "available": bool(session_url),
            "env_var": session_name,
            "endpoint": _connection_endpoint_status(session_url),
            "sequences": [],
            "error_type": None,
        },
        "current_pooler": {
            **_safe_connection_path_sequences(_dashboard_read_connection, repetitions),
        },
    }
    if direct_url:
        result["direct_connection"].update(_safe_connection_path_sequences(
            lambda: _diagnostic_psycopg_connection(direct_url),
            repetitions,
        ))
    if session_url:
        result["session_pooler"].update(_safe_connection_path_sequences(
            lambda: _diagnostic_psycopg_connection(session_url),
            repetitions,
        ))
    return result


_CONNECTION_PATH_BENCHMARK_BUCKETS = (
    ("lt_75_ms", None, 75),
    ("75_225_ms", 75, 225),
    ("225_375_ms", 225, 375),
    ("375_525_ms", 375, 525),
    ("525_675_ms", 525, 675),
    ("gt_675_ms", 675, None),
)


def _median(values: list[Any]) -> float | None:
    samples = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not samples:
        return None
    midpoint = len(samples) // 2
    if len(samples) % 2:
        return round(samples[midpoint], 2)
    return round((samples[midpoint - 1] + samples[midpoint]) / 2, 2)


def _diagnostic_connect_path(conninfo: str) -> tuple[Any, float]:
    import psycopg
    from database import postgres

    started = time.perf_counter()
    conn = psycopg.connect(conninfo, connect_timeout=postgres._connect_timeout_seconds())
    return conn, round((time.perf_counter() - started) * 1000, 2)


def _connection_path_dsn_candidates() -> dict[str, dict[str, Any]]:
    from database import postgres

    direct_name, direct_url = _first_env_value(
        (
            "DIRECT_DATABASE_URL",
            "DATABASE_DIRECT_URL",
            "SUPABASE_DIRECT_DATABASE_URL",
        )
    )
    session_name, session_url = _first_env_value(
        (
            "SESSION_POOLER_DATABASE_URL",
            "DATABASE_SESSION_POOLER_URL",
            "SUPABASE_SESSION_POOLER_DATABASE_URL",
        )
    )
    return {
        "CURRENT_TRANSACTION_POOLER": {
            "available": bool(postgres.DATABASE_URL),
            "env_var": "DATABASE_URL" if postgres.DATABASE_URL else None,
            "conninfo": postgres.DATABASE_URL,
            "endpoint": _connection_endpoint_status(postgres.DATABASE_URL),
            "sequence_uses_dashboard_pool": True,
        },
        "SESSION_POOLER": {
            "available": bool(session_url),
            "env_var": session_name,
            "conninfo": session_url,
            "endpoint": _connection_endpoint_status(session_url),
            "sequence_uses_dashboard_pool": False,
        },
        "DIRECT_DATABASE": {
            "available": bool(direct_url),
            "env_var": direct_name,
            "conninfo": direct_url,
            "endpoint": _connection_endpoint_status(direct_url),
            "sequence_uses_dashboard_pool": False,
        },
    }


def _bucket_statement_latencies(values: list[float]) -> dict[str, int]:
    buckets = {name: 0 for name, _low, _high in _CONNECTION_PATH_BENCHMARK_BUCKETS}
    for value in values:
        for name, low, high in _CONNECTION_PATH_BENCHMARK_BUCKETS:
            if (low is None or value >= low) and (high is None or value < high):
                buckets[name] += 1
                break
    return buckets


def _quantization_visible(values: list[float]) -> bool:
    bucketed = _bucket_statement_latencies(values)
    quantized_samples = sum(
        bucketed[name]
        for name in ("75_225_ms", "225_375_ms", "375_525_ms", "525_675_ms")
    )
    return quantized_samples >= max(2, len(values) // 2) if values else False


def _diagnostic_path_sequence_sample(path: dict[str, Any]) -> dict[str, Any]:
    sequence = [
        ("select1_first", "select 1", ()),
        ("select1_second", "select 1", ()),
        ("card_two_1", _prediction_history_summary_cloud_sql(), (100,)),
        ("select1_third", "select 1", ()),
        ("card_two_2", _prediction_history_summary_cloud_sql(), (100,)),
        ("select1_fourth", "select 1", ()),
    ]
    connect_started = time.perf_counter()
    conn = None
    close_conn = None
    close_started = None
    try:
        if path.get("sequence_uses_dashboard_pool"):
            connection_context = _dashboard_read_connection()
            conn = connection_context.__enter__()
            connect_ms = round((time.perf_counter() - connect_started) * 1000, 2)
            close_conn = lambda: connection_context.__exit__(None, None, None)
        else:
            conn, connect_ms = _diagnostic_connect_path(str(path["conninfo"]))
            close_conn = conn.close
        result = {
            "connection_path": path["name"],
            "connect_ms": connect_ms,
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "connection_hash": _connection_hash(conn),
            "checkout_status": _transaction_status(conn),
            "autocommit": getattr(conn, "autocommit", None),
            "statements": [],
        }
        for name, sql, params in sequence:
            result["statements"].append(_diagnostic_statement(conn, name, sql, params))
        result["final_status"] = _transaction_status(conn)
        try:
            result["server_execution"] = _diagnostic_explain_card_two_main_query_on_conn(conn)
        except Exception as exc:
            result["server_execution"] = {
                "available": False,
                "error_type": type(exc).__name__,
            }
        try:
            conn.rollback()
        except Exception:
            logger.warning("connection path benchmark rollback failed", exc_info=True)
        close_started = time.perf_counter()
        close_conn()
        result["close_ms"] = round((time.perf_counter() - close_started) * 1000, 2)
        return result
    except Exception as exc:
        if conn is not None and close_started is None:
            try:
                if close_conn is not None:
                    close_conn()
                else:
                    conn.close()
            except Exception:
                pass
        return {
            "connection_path": path["name"],
            "error_type": type(exc).__name__,
            "connect_ms": round((time.perf_counter() - connect_started) * 1000, 2),
            "statements": [],
        }


def _diagnostic_path_fresh_sample(path: dict[str, Any]) -> dict[str, Any]:
    conn = None
    try:
        conn, connect_ms = _diagnostic_connect_path(str(path["conninfo"]))
        select1 = _diagnostic_statement(conn, "select1", "select 1", ())
        card_two = _diagnostic_statement(conn, "card_two", _prediction_history_summary_cloud_sql(), (100,))
        try:
            conn.rollback()
        except Exception:
            logger.warning("fresh connection benchmark rollback failed", exc_info=True)
        return {
            "connection_path": path["name"],
            "connect_ms": connect_ms,
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "select1": select1,
            "card_two": card_two,
        }
    except Exception as exc:
        return {
            "connection_path": path["name"],
            "error_type": type(exc).__name__,
            "connect_ms": None,
        }
    finally:
        if conn is not None:
            conn.close()


def _diagnostic_path_reused_sample(path: dict[str, Any]) -> dict[str, Any]:
    conn = None
    statements = [
        ("select1_1", "select 1", ()),
        ("card_two_1", _prediction_history_summary_cloud_sql(), (100,)),
        ("select1_2", "select 1", ()),
        ("card_two_2", _prediction_history_summary_cloud_sql(), (100,)),
        ("select1_3", "select 1", ()),
        ("card_two_3", _prediction_history_summary_cloud_sql(), (100,)),
        ("select1_4", "select 1", ()),
    ]
    try:
        conn, connect_ms = _diagnostic_connect_path(str(path["conninfo"]))
        result = {
            "connection_path": path["name"],
            "connect_ms": connect_ms,
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None),
            "connection_hash": _connection_hash(conn),
            "statements": [
                _diagnostic_statement(conn, name, sql, params)
                for name, sql, params in statements
            ],
            "final_status": _transaction_status(conn),
        }
        try:
            conn.rollback()
        except Exception:
            logger.warning("reused connection benchmark rollback failed", exc_info=True)
        return result
    except Exception as exc:
        return {
            "connection_path": path["name"],
            "error_type": type(exc).__name__,
            "statements": [],
        }
    finally:
        if conn is not None:
            conn.close()


def _statement_execute_ms(samples: list[dict[str, Any]], names: set[str]) -> list[float]:
    values: list[float] = []
    for sample in samples:
        for statement in sample.get("statements", []):
            if statement.get("statement") in names and isinstance(statement.get("execute_ms"), (int, float)):
                values.append(float(statement["execute_ms"]))
    return values


def _connection_path_summary(
    sequences: list[dict[str, Any]],
    fresh_connections: list[dict[str, Any]],
    reused_connections: list[dict[str, Any]],
) -> dict[str, Any]:
    first_select_values = _statement_execute_ms(sequences, {"select1_first"})
    warm_select_values = _statement_execute_ms(
        sequences,
        {"select1_second", "select1_third", "select1_fourth"},
    ) + _statement_execute_ms(
        reused_connections,
        {"select1_2", "select1_3", "select1_4"},
    )
    card_two_values = _statement_execute_ms(
        sequences,
        {"card_two_1", "card_two_2"},
    ) + _statement_execute_ms(
        reused_connections,
        {"card_two_1", "card_two_2", "card_two_3"},
    )
    all_execute_values = first_select_values + warm_select_values + card_two_values
    server_execution_values = [
        sample.get("server_execution", {}).get("execution_ms")
        for sample in sequences
        if isinstance(sample.get("server_execution", {}).get("execution_ms"), (int, float))
    ]
    connect_values = [
        sample.get("connect_ms")
        for sample in sequences + fresh_connections + reused_connections
        if isinstance(sample.get("connect_ms"), (int, float))
    ]
    errors = [
        sample.get("error_type")
        for sample in sequences + fresh_connections + reused_connections
        if sample.get("error_type")
    ]
    card_two_median = _median(card_two_values)
    server_median = _median(server_execution_values)
    return {
        "connect_median_ms": _median(connect_values),
        "first_select1_median_ms": _median(first_select_values),
        "warm_select1_median_ms": _median(warm_select_values),
        "card_two_median_ms": card_two_median,
        "server_execution_median_ms": server_median,
        "client_server_gap_ms": (
            round(card_two_median - server_median, 2)
            if card_two_median is not None and server_median is not None
            else None
        ),
        "latency_buckets": _bucket_statement_latencies(all_execute_values),
        "quantization_visible": _quantization_visible(all_execute_values),
        "stable": not errors and bool(sequences),
        "errors": errors,
    }


def _connection_path_rankings(paths: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    def ranked(metric: str) -> list[str]:
        available = [
            (name, payload["summary"].get(metric))
            for name, payload in paths.items()
            if payload.get("available")
            and payload.get("summary")
            and isinstance(payload["summary"].get(metric), (int, float))
        ]
        return [name for name, _value in sorted(available, key=lambda item: item[1])]

    return {
        "lowest_warm_statement_latency": ranked("warm_select1_median_ms"),
        "lowest_connection_latency": ranked("connect_median_ms"),
        "lowest_card_two_latency": ranked("card_two_median_ms"),
        "operational_safety": [
            name
            for name in ("CURRENT_TRANSACTION_POOLER", "SESSION_POOLER", "DIRECT_DATABASE")
            if paths.get(name, {}).get("available")
        ],
    }


def _safe_username_format(username: str | None) -> str | None:
    if not username:
        return None
    if username == "postgres":
        return "postgres"
    if username.startswith("postgres."):
        return "postgres.<project-ref>"
    return "<non-postgres-format>"


def _parse_diagnostic_conninfo(conninfo: str | None) -> dict[str, Any]:
    if not conninfo:
        return {
            "scheme": None,
            "host": None,
            "port": None,
            "database": None,
            "username": None,
            "username_format": None,
        }
    parse_error = None
    try:
        parsed = urlsplit(conninfo)
        scheme = parsed.scheme or None
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError as exc:
            parse_error = type(exc).__name__
            port = None
        database = parsed.path.lstrip("/") or None
        username = parsed.username
    except Exception as exc:
        parse_error = type(exc).__name__
        scheme = None
        host = None
        port = None
        database = None
        username = None
    if not host:
        try:
            from psycopg.conninfo import conninfo_to_dict

            values = conninfo_to_dict(conninfo)
            scheme = "conninfo"
            host = values.get("host")
            raw_port = values.get("port")
            port = int(raw_port) if raw_port else None
            database = values.get("dbname")
            username = values.get("user")
        except Exception:
            pass
    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "username_format": _safe_username_format(username),
        "parse_error": parse_error,
    }


def _resolved_address_family(addrinfo: list[Any]) -> str:
    families = {item[0] for item in addrinfo}
    has_ipv4 = socket.AF_INET in families
    has_ipv6 = socket.AF_INET6 in families
    if has_ipv4 and has_ipv6:
        return "BOTH"
    if has_ipv4:
        return "IPv4"
    if has_ipv6:
        return "IPv6"
    return "NONE"


def _sanitize_connection_error(message: str, conninfo: str | None, parsed: dict[str, Any]) -> str:
    sanitized = str(message or "")
    if conninfo:
        sanitized = sanitized.replace(conninfo, "<redacted-dsn>")
    for key in ("username",):
        value = parsed.get(key)
        if value:
            sanitized = sanitized.replace(str(value), "<redacted-username>")
    try:
        url = urlsplit(conninfo or "")
        if url.password:
            sanitized = sanitized.replace(url.password, "<redacted-password>")
        if url.netloc:
            sanitized = sanitized.replace(url.netloc, "<redacted-netloc>")
    except Exception:
        pass
    return sanitized


def _classify_connection_error(error_type: str | None, message: str, tcp_result: str) -> str:
    text = str(message or "").lower()
    if "could not translate host name" in text or "name or service not known" in text:
        return "DNS"
    if "timeout" in text or "timed out" in text:
        return "TCP_TIMEOUT"
    if "connection refused" in text:
        return "CONNECTION_REFUSED"
    if "password authentication failed" in text or "authentication failed" in text or "28p01" in text:
        return "AUTHENTICATION"
    if "ssl" in text or "tls" in text or error_type == "SSLError":
        return "SSL"
    if "too many clients" in text or "remaining connection slots" in text or "pool" in text and "full" in text:
        return "POOLER_CAPACITY"
    if "database" in text and ("does not exist" in text or "unknown" in text):
        return "DATABASE"
    if tcp_result == "FAIL" and ("network is unreachable" in text or "no route to host" in text):
        return "NETWORK_RESTRICTION"
    return "UNKNOWN"


def _session_pooler_shape(parsed: dict[str, Any]) -> dict[str, bool]:
    host = str(parsed.get("host") or "")
    return {
        "host_matches_supabase_session_pooler": (
            host.startswith("aws-")
            and host.endswith(".pooler.supabase.com")
            and "-ap-northeast-1." in host
        ),
        "port_is_5432": parsed.get("port") == 5432,
        "username_starts_with_postgres_dot": str(parsed.get("username") or "").startswith("postgres."),
        "database_is_postgres": parsed.get("database") == "postgres",
    }


def classify_session_pooler_connection_failure() -> dict[str, Any]:
    session_name, session_url = _first_env_value(
        (
            "DATABASE_SESSION_POOLER_URL",
            "SESSION_POOLER_DATABASE_URL",
            "SUPABASE_SESSION_POOLER_DATABASE_URL",
        )
    )
    parsed = _parse_diagnostic_conninfo(session_url)
    host = parsed.get("host")
    port = parsed.get("port")
    diagnostic_port = 5432
    result: dict[str, Any] = {
        "session_pooler_env_present": bool(session_url),
        "session_pooler_env_var": session_name,
        "parsed_scheme": parsed.get("scheme"),
        "parsed_host": host,
        "parsed_port": port,
        "parsed_database": parsed.get("database"),
        "parsed_username_format": parsed.get("username_format"),
        "parse_error": parsed.get("parse_error"),
        "supabase_session_pooler_shape": _session_pooler_shape(parsed),
        "dns_resolution": "FAIL",
        "resolved_address_family": "NONE",
        "tcp_connect_to_host_5432": "FAIL",
        "tcp_connect_ms": None,
        "psycopg_connect": "FAIL",
        "psycopg_connect_ms": None,
        "operational_error_class": None,
        "sanitized_error_message": None,
        "error_category": "UNKNOWN",
        "select_one_result": "NOT RUN",
    }
    if not session_url or not host:
        result["sanitized_error_message"] = "DATABASE_SESSION_POOLER_URL is not configured or host could not be parsed."
        result["error_category"] = "UNKNOWN"
        return result

    try:
        addrinfo = socket.getaddrinfo(str(host), diagnostic_port, type=socket.SOCK_STREAM)
        result["dns_resolution"] = "PASS"
        result["resolved_address_family"] = _resolved_address_family(addrinfo)
    except Exception as exc:
        result["operational_error_class"] = type(exc).__name__
        result["sanitized_error_message"] = _sanitize_connection_error(str(exc), session_url, parsed)
        result["error_category"] = "DNS"
        return result

    tcp_started = time.perf_counter()
    try:
        with socket.create_connection((str(host), diagnostic_port), timeout=5):
            pass
        result["tcp_connect_to_host_5432"] = "PASS"
    except Exception as exc:
        result["operational_error_class"] = type(exc).__name__
        result["sanitized_error_message"] = _sanitize_connection_error(str(exc), session_url, parsed)
        result["error_category"] = _classify_connection_error(type(exc).__name__, str(exc), "FAIL")
    finally:
        result["tcp_connect_ms"] = round((time.perf_counter() - tcp_started) * 1000, 2)

    conn = None
    psycopg_started = time.perf_counter()
    try:
        import psycopg

        conn = psycopg.connect(session_url, connect_timeout=5)
        result["psycopg_connect"] = "PASS"
        result["operational_error_class"] = None
        result["sanitized_error_message"] = None
        with conn.cursor() as cur:
            cur.execute("select 1", prepare=False)
            row = cur.fetchone()
        result["select_one_result"] = "PASS" if row and row[0] == 1 else "FAIL"
        result["error_category"] = "UNKNOWN" if result["select_one_result"] == "PASS" else "DATABASE"
    except Exception as exc:
        result["operational_error_class"] = type(exc).__name__
        result["sanitized_error_message"] = _sanitize_connection_error(str(exc), session_url, parsed)
        result["error_category"] = _classify_connection_error(
            type(exc).__name__,
            str(exc),
            result["tcp_connect_to_host_5432"],
        )
        result["select_one_result"] = "FAIL"
    finally:
        result["psycopg_connect_ms"] = round((time.perf_counter() - psycopg_started) * 1000, 2)
        if conn is not None:
            conn.close()
    return result


def _stats(values: list[float]) -> dict[str, Any]:
    return {
        "samples": values,
        "median": _median(values),
        "min": round(min(values), 2) if values else None,
        "max": round(max(values), 2) if values else None,
    }


def _dns_latency_samples(host: str, count: int = 10) -> dict[str, Any]:
    samples: list[float] = []
    errors: list[str] = []
    for _ in range(max(1, min(count, 10))):
        started = time.perf_counter()
        try:
            socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            samples.append(round((time.perf_counter() - started) * 1000, 2))
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {**_stats(samples), "errors": errors}


def _tcp_latency_samples(host: str, port: int, count: int = 10) -> dict[str, Any]:
    samples: list[float] = []
    errors: list[str] = []
    for _ in range(max(1, min(count, 10))):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
            samples.append(round((time.perf_counter() - started) * 1000, 2))
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {**_stats(samples), "errors": errors}


def _psycopg_connect_latency_samples(conninfo: str | None, count: int = 5) -> dict[str, Any]:
    samples: list[float] = []
    errors: list[str] = []
    if not conninfo:
        return {**_stats(samples), "errors": ["missing conninfo"]}
    import psycopg

    for _ in range(max(1, min(count, 5))):
        conn = None
        started = time.perf_counter()
        try:
            conn = psycopg.connect(conninfo, connect_timeout=5)
            samples.append(round((time.perf_counter() - started) * 1000, 2))
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            if conn is not None:
                conn.close()
    return {**_stats(samples), "errors": errors}


def _same_connection_select1_sequence(conninfo: str | None) -> dict[str, Any]:
    if not conninfo:
        return {
            "samples": [],
            "median": None,
            "backend_pid": None,
            "same_backend_pid": False,
            "transaction_status_sequence": [],
            "errors": ["missing conninfo"],
        }
    import psycopg

    conn = None
    samples: list[float] = []
    statuses: list[dict[str, Any]] = []
    backend_pids: list[Any] = []
    errors: list[str] = []
    try:
        conn = psycopg.connect(conninfo, connect_timeout=5)
        backend_pid = getattr(getattr(conn, "info", None), "backend_pid", None)
        for index in range(5):
            before = _transaction_status(conn)
            with conn.cursor() as cur:
                started = time.perf_counter()
                cur.execute("select 1", prepare=False)
                samples.append(round((time.perf_counter() - started) * 1000, 2))
                row = cur.fetchone()
            after = _transaction_status(conn)
            backend_pids.append(getattr(getattr(conn, "info", None), "backend_pid", None))
            statuses.append(
                {
                    "statement": index + 1,
                    "before": before,
                    "after": after,
                    "result": "PASS" if row and row[0] == 1 else "FAIL",
                }
            )
        try:
            conn.rollback()
        except Exception:
            logger.warning("same connection select1 diagnostic rollback failed", exc_info=True)
        return {
            "samples": samples,
            "median": _median(samples),
            "backend_pid": backend_pid,
            "same_backend_pid": len(set(backend_pids)) == 1 if backend_pids else False,
            "transaction_status_sequence": statuses,
            "errors": errors,
        }
    except Exception as exc:
        errors.append(type(exc).__name__)
        return {
            "samples": samples,
            "median": _median(samples),
            "backend_pid": getattr(getattr(conn, "info", None), "backend_pid", None) if conn is not None else None,
            "same_backend_pid": False,
            "transaction_status_sequence": statuses,
            "errors": errors,
        }
    finally:
        if conn is not None:
            conn.close()


def _latency_layer(
    dns: dict[str, Any],
    tcp_6543: dict[str, Any],
    transaction_connect: dict[str, Any],
    same_connection_select1: dict[str, Any],
) -> str:
    dns_median = dns.get("median")
    tcp_median = tcp_6543.get("median")
    connect_median = transaction_connect.get("median")
    select_median = same_connection_select1.get("median")
    if isinstance(dns_median, (int, float)) and dns_median >= 75:
        return "DNS"
    if isinstance(tcp_median, (int, float)) and tcp_median >= 75:
        return "TCP"
    if isinstance(connect_median, (int, float)) and connect_median >= 300:
        return "TLS_POSTGRES_STARTUP"
    if isinstance(select_median, (int, float)) and select_median >= 75:
        return "SQL_ROUNDTRIP"
    return "UNKNOWN"


def run_network_roundtrip_decomposition() -> dict[str, Any]:
    from database import postgres

    session_name, session_url = _first_env_value(
        (
            "DATABASE_SESSION_POOLER_URL",
            "SESSION_POOLER_DATABASE_URL",
            "SUPABASE_SESSION_POOLER_DATABASE_URL",
        )
    )
    current_parsed = _parse_diagnostic_conninfo(postgres.DATABASE_URL)
    host = current_parsed.get("host")
    result: dict[str, Any] = {
        "render_region": os.getenv("RENDER_REGION") or os.getenv("RENDER_SERVICE_REGION"),
        "current_pooler_host": host,
        "session_pooler_env_present": bool(session_url),
        "session_pooler_env_var": session_name,
    }
    if not host:
        result.update(
            {
                "dns": {**_stats([]), "errors": ["missing current pooler host"]},
                "tcp_6543": {**_stats([]), "errors": ["missing current pooler host"]},
                "tcp_5432": {**_stats([]), "errors": ["missing current pooler host"]},
                "psycopg_transaction_connect": _psycopg_connect_latency_samples(postgres.DATABASE_URL),
                "psycopg_session_connect": _psycopg_connect_latency_samples(session_url),
                "same_connection_select1": _same_connection_select1_sequence(postgres.DATABASE_URL),
                "latency_layer": "UNKNOWN",
            }
        )
        return result

    dns = _dns_latency_samples(str(host), 10)
    tcp_6543 = _tcp_latency_samples(str(host), 6543, 10)
    tcp_5432 = _tcp_latency_samples(str(host), 5432, 10)
    transaction_connect = _psycopg_connect_latency_samples(postgres.DATABASE_URL, 5)
    session_connect = _psycopg_connect_latency_samples(session_url, 5)
    same_connection_select1 = _same_connection_select1_sequence(postgres.DATABASE_URL)
    result.update(
        {
            "dns": dns,
            "tcp_6543": tcp_6543,
            "tcp_5432": tcp_5432,
            "psycopg_transaction_connect": transaction_connect,
            "psycopg_session_connect": session_connect,
            "same_connection_select1": same_connection_select1,
            "latency_layer": _latency_layer(
                dns,
                tcp_6543,
                transaction_connect,
                same_connection_select1,
            ),
        }
    )
    return result


def _http_json(url: str, timeout: float = 4.0) -> dict[str, Any]:
    try:
        request = Request(url, headers={"User-Agent": "bingo-ai-pro-runtime-diagnostics"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"error_type": type(exc).__name__}


def _public_outbound_ip() -> dict[str, Any]:
    payload = _http_json("https://api.ipify.org?format=json", timeout=4.0)
    ip = payload.get("ip")
    if ip:
        return {"ip": ip, "source": "api.ipify.org", "error_type": None}
    return {"ip": None, "source": "api.ipify.org", "error_type": payload.get("error_type")}


def _geo_lookup(ip: str | None) -> dict[str, Any]:
    if not ip:
        return {"available": False, "error_type": "missing_ip"}
    payload = _http_json(f"https://ipwho.is/{ip}", timeout=4.0)
    if payload.get("success") is False:
        return {
            "available": False,
            "ip": ip,
            "error_type": payload.get("message") or "geo_lookup_failed",
        }
    return {
        "available": not bool(payload.get("error_type")),
        "ip": ip,
        "continent": payload.get("continent"),
        "country": payload.get("country"),
        "region": payload.get("region"),
        "city": payload.get("city"),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": (payload.get("timezone") or {}).get("id") if isinstance(payload.get("timezone"), dict) else None,
        "connection": {
            "asn": (payload.get("connection") or {}).get("asn") if isinstance(payload.get("connection"), dict) else None,
            "org": (payload.get("connection") or {}).get("org") if isinstance(payload.get("connection"), dict) else None,
            "isp": (payload.get("connection") or {}).get("isp") if isinstance(payload.get("connection"), dict) else None,
        },
        "error_type": payload.get("error_type"),
    }


def _dns_addresses(host: str | None) -> dict[str, Any]:
    if not host:
        return {"addresses": [], "errors": ["missing host"]}
    try:
        addrinfo = socket.getaddrinfo(str(host), None, type=socket.SOCK_STREAM)
    except Exception as exc:
        return {"addresses": [], "errors": [type(exc).__name__]}
    addresses: list[str] = []
    for item in addrinfo:
        address = item[4][0]
        if address not in addresses:
            addresses.append(address)
    return {"addresses": addresses, "errors": []}


def _aws_region_hint_from_host(host: str | None) -> str | None:
    text = str(host or "")
    for part in text.split("."):
        if part.startswith("ap-") or part.startswith("us-") or part.startswith("eu-"):
            return part
    return None


def _route_probe(host: str | None) -> dict[str, Any]:
    if not host:
        return {"available": False, "tool": None, "output": None, "error": "missing host"}
    commands = []
    tracepath = shutil.which("tracepath")
    if tracepath:
        commands.append([tracepath, "-n", str(host)])
    traceroute = shutil.which("traceroute")
    if traceroute:
        commands.append([traceroute, "-n", "-w", "1", "-q", "1", "-m", "8", str(host)])
    ping = shutil.which("ping")
    if ping:
        commands.append([ping, "-c", "4", "-W", "1", str(host)])
    if not commands:
        return {"available": False, "tool": None, "output": None, "error": "no route tool available"}
    command = commands[0]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        output = "\n".join((completed.stdout or "", completed.stderr or "")).strip()
        return {
            "available": True,
            "tool": Path(command[0]).name,
            "returncode": completed.returncode,
            "output": output[:2000],
            "error": None,
        }
    except Exception as exc:
        return {
            "available": False,
            "tool": Path(command[0]).name,
            "output": None,
            "error": type(exc).__name__,
        }


def _region_mismatch_evidence(render_geo: dict[str, Any], supabase_geo: dict[str, Any], supabase_region_hint: str | None) -> dict[str, Any]:
    render_country = render_geo.get("country")
    render_region = render_geo.get("region")
    supabase_country = supabase_geo.get("country")
    supabase_region = supabase_geo.get("region")
    mismatch = None
    if render_country and supabase_country:
        mismatch = render_country != supabase_country or (
            bool(render_region and supabase_region) and render_region != supabase_region
        )
    return {
        "render_country": render_country,
        "render_region": render_region,
        "supabase_country": supabase_country,
        "supabase_region": supabase_region,
        "supabase_aws_region_hint": supabase_region_hint,
        "region_mismatch_supported": mismatch,
    }


def identify_render_supabase_route() -> dict[str, Any]:
    from database import postgres

    current_parsed = _parse_diagnostic_conninfo(postgres.DATABASE_URL)
    host = current_parsed.get("host")
    render_env_candidates = {
        name: os.getenv(name)
        for name in (
            "RENDER_REGION",
            "RENDER_SERVICE_REGION",
            "RENDER_SERVICE_NAME",
            "RENDER_SERVICE_ID",
            "RENDER_INSTANCE_ID",
        )
        if os.getenv(name)
    }
    outbound = _public_outbound_ip()
    render_geo = _geo_lookup(outbound.get("ip"))
    supabase_dns = _dns_addresses(str(host) if host else None)
    supabase_ip = (supabase_dns.get("addresses") or [None])[0]
    supabase_geo = _geo_lookup(supabase_ip)
    supabase_region_hint = _aws_region_hint_from_host(str(host) if host else None)
    tcp_6543 = _tcp_latency_samples(str(host), 6543, 10) if host else {**_stats([]), "errors": ["missing host"]}
    tcp_5432 = _tcp_latency_samples(str(host), 5432, 10) if host else {**_stats([]), "errors": ["missing host"]}
    dns = _dns_latency_samples(str(host), 10) if host else {**_stats([]), "errors": ["missing host"]}
    mismatch = _region_mismatch_evidence(render_geo, supabase_geo, supabase_region_hint)
    return {
        "render_env_candidates": render_env_candidates,
        "render_outbound_ip": outbound,
        "render_outbound_ip_geo": render_geo,
        "supabase_host": host,
        "supabase_dns": supabase_dns,
        "supabase_host_geo": supabase_geo,
        "supabase_region_hint": supabase_region_hint,
        "network_route": _route_probe(str(host) if host else None),
        "dns": dns,
        "tcp_6543": tcp_6543,
        "tcp_5432": tcp_5432,
        "region_mismatch_evidence": mismatch,
    }


def run_card_two_connection_path_ab_benchmark(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    paths = _connection_path_dsn_candidates()
    result_paths: dict[str, dict[str, Any]] = {}
    for name, path in paths.items():
        path["name"] = name
        sanitized = {
            "available": path["available"],
            "env_var": path["env_var"],
            "endpoint": path["endpoint"],
            "sequence_uses_dashboard_pool": path["sequence_uses_dashboard_pool"],
            "sequences": [],
            "fresh_connections": [],
            "reused_connections": [],
            "summary": None,
        }
        if not path["available"]:
            sanitized["not_available_reason"] = "configured safe DSN not available"
            result_paths[name] = sanitized
            continue
        sanitized["sequences"] = [
            _diagnostic_path_sequence_sample(path)
            for _ in range(repetitions)
        ]
        sanitized["fresh_connections"] = [
            _diagnostic_path_fresh_sample(path)
            for _ in range(repetitions)
        ]
        sanitized["reused_connections"] = [
            _diagnostic_path_reused_sample(path)
            for _ in range(repetitions)
        ]
        sanitized["summary"] = _connection_path_summary(
            sanitized["sequences"],
            sanitized["fresh_connections"],
            sanitized["reused_connections"],
        )
        result_paths[name] = sanitized

    return {
        "status": "ok",
        "sample_count": repetitions,
        "timer_boundary": {
            "connect_ms": "pool checkout for current production sequence; fresh psycopg connect for fresh/reused and non-current paths",
            "execute_ms": "Python wall-clock duration of cur.execute(sql, params, prepare=False)",
            "server_execution_ms": "Postgres EXPLAIN ANALYZE Execution Time for Card Two main query",
        },
        "paths": result_paths,
        "rankings": _connection_path_rankings(result_paths),
        "decision_rules": {
            "connection_path_primary_bottleneck": "true if session/direct warm SELECT 1 and Card Two medians fall substantially below 100 ms while current remains quantized",
            "network_or_driver_next": "true if all available paths remain near the same ~150 ms latency bands",
            "production_switch": "diagnostic output only; no automatic DATABASE_URL change",
        },
    }


@contextmanager
def _diagnostic_query_capture(
    events: list[dict[str, Any]],
    label: str,
    started_event: threading.Event | None = None,
):
    events_token = _DIAGNOSTIC_QUERY_EVENTS.set(events)
    label_token = _DIAGNOSTIC_QUERY_LABEL.set(label)
    started_token = _DIAGNOSTIC_QUERY_STARTED_EVENT.set(started_event)
    try:
        yield
    finally:
        _DIAGNOSTIC_QUERY_STARTED_EVENT.reset(started_token)
        _DIAGNOSTIC_QUERY_LABEL.reset(label_token)
        _DIAGNOSTIC_QUERY_EVENTS.reset(events_token)


@contextmanager
def _diagnostic_shared_dashboard_connection(conn: Any):
    state = {
        "cloud_available": True,
        "connect_ms": 0.0,
        "connect_reported": False,
        "opened_at": time.perf_counter(),
    }
    conn_token = _CARD_TWO_DASHBOARD_CONNECTION.set(conn)
    state_token = _CARD_TWO_DASHBOARD_CONNECTION_STATE.set(state)
    try:
        yield
    finally:
        _CARD_TWO_DASHBOARD_CONNECTION.reset(conn_token)
        _CARD_TWO_DASHBOARD_CONNECTION_STATE.reset(state_token)


def _diagnostic_recent_card_two_events(start_index: int) -> list[dict[str, Any]]:
    with _CARD_TWO_HISTORY_TIMING_LOCK:
        return deepcopy(_CARD_TWO_HISTORY_TIMINGS[start_index:])


def _diagnostic_stage(events: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "stage" and event.get("stage") == stage:
            return event
    return None


def _diagnostic_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "summary":
            return event
    return None


def _diagnostic_context(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "dashboard_context":
            return event
    return None


def _diagnostic_card_two_sample(events: list[dict[str, Any]], query_events: list[dict[str, Any]]) -> dict[str, Any]:
    main = _diagnostic_stage(events, "main_query") or {}
    metadata = _diagnostic_stage(events, "metadata_bulk") or {}
    summary = _diagnostic_summary(events) or {}
    context = _diagnostic_context(events) or {}
    return {
        "total_ms": summary.get("total_ms"),
        "rows": summary.get("rows"),
        "metadata_queries": summary.get("metadata_queries"),
        "component_total_execution_ms": context.get("component_total_execution_ms"),
        "main_query": {
            "stage_ms": main.get("duration_ms"),
            **(main.get("db_timing") or {}),
        },
        "metadata_bulk": {
            "stage_ms": metadata.get("duration_ms"),
            **(metadata.get("db_timing") or {}),
        },
        "dashboard_context": context,
        "query_events": query_events,
    }


def _query_events_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return min(left.get("finished_at", 0), right.get("finished_at", 0)) > max(
        left.get("started_at", 0),
        right.get("started_at", 0),
    )


def _diagnostic_sql_overlap(query_events: list[dict[str, Any]]) -> dict[str, Any]:
    card_events = [
        event for event in query_events
        if str(event.get("label") or "").startswith("card_two")
    ]
    overlap_events = [
        event for event in query_events
        if str(event.get("label") or "").startswith("overlap")
    ]
    overlaps = [
        {
            "card_label": card.get("label"),
            "overlap_label": overlap.get("label"),
            "same_python_connection": card.get("connection_hash") == overlap.get("connection_hash"),
            "same_backend_pid": card.get("backend_pid") == overlap.get("backend_pid"),
            "overlap_ms": round(
                (
                    min(card.get("finished_at", 0), overlap.get("finished_at", 0))
                    - max(card.get("started_at", 0), overlap.get("started_at", 0))
                )
                * 1000,
                2,
            ),
        }
        for card in card_events
        for overlap in overlap_events
        if _query_events_overlap(card, overlap)
    ]
    return {
        "sql_overlap": bool(overlaps),
        "overlaps": overlaps,
        "card_two_connection_hashes": sorted({str(event.get("connection_hash")) for event in card_events}),
        "overlap_connection_hashes": sorted({str(event.get("connection_hash")) for event in overlap_events}),
        "card_two_backend_pids": sorted({str(event.get("backend_pid")) for event in card_events}),
        "overlap_backend_pids": sorted({str(event.get("backend_pid")) for event in overlap_events}),
    }


def _run_diagnostic_card_two(events: list[dict[str, Any]]) -> list[dict]:
    with _diagnostic_query_capture(events, "card_two"):
        with card_two_dashboard_execution_context(0.0):
            return get_prediction_history_summary_records(100, diagnostic_component="card_two_history")


def _run_diagnostic_overlap_loader(events: list[dict[str, Any]], component: str) -> Any:
    with _diagnostic_query_capture(events, f"overlap.{component}"):
        if component == "previous_verification":
            latest = get_prediction_history_summary_records(1, diagnostic_component=None)
            target_issue = (latest[0] if latest else {}).get("prediction_issue")
            if target_issue:
                return get_latest_verified_prediction_summary_at_or_before(target_issue)
            return None
        return get_prediction_lifecycle_aggregates(diagnostic_component=component)


def _run_card_two_contention_sample(
    *,
    mode: str,
    overlap_component: str,
    same_connection: bool,
    overlap: bool,
    run_overlap_loader: bool = True,
) -> dict[str, Any]:
    query_events: list[dict[str, Any]] = []
    timing_events: list[dict[str, Any]] = []
    started = time.perf_counter()
    errors: dict[str, str] = {}
    card_result_count = None
    overlap_result_type = None

    shared_conn: Any | None = None

    @contextmanager
    def maybe_shared_connection():
        if same_connection and shared_conn is not None:
            with _diagnostic_shared_dashboard_connection(shared_conn):
                yield
            return
        yield

    def card_task() -> None:
        nonlocal card_result_count
        timing_token = _DIAGNOSTIC_CARD_TWO_TIMING_EVENTS.set(timing_events)
        try:
            with maybe_shared_connection():
                records = _run_diagnostic_card_two(query_events)
            card_result_count = len(records or [])
        except Exception as exc:
            errors["card_two"] = type(exc).__name__
        finally:
            _DIAGNOSTIC_CARD_TWO_TIMING_EVENTS.reset(timing_token)

    def overlap_task() -> None:
        nonlocal overlap_result_type
        try:
            with maybe_shared_connection():
                result = _run_diagnostic_overlap_loader(query_events, overlap_component)
            overlap_result_type = type(result).__name__
        except Exception as exc:
            errors["overlap"] = type(exc).__name__

    def run_pair() -> None:
        if not run_overlap_loader:
            card_task()
            return
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-two-contention") as executor:
            if overlap:
                overlap_future = executor.submit(overlap_task)
                time.sleep(0.005)
                card_future = executor.submit(card_task)
                card_future.result(timeout=30)
                overlap_future.result(timeout=30)
                return
            overlap_future = executor.submit(overlap_task)
            overlap_future.result(timeout=30)
            card_future = executor.submit(card_task)
            card_future.result(timeout=30)

    if same_connection:
        with _dashboard_read_connection() as conn:
            shared_conn = conn
            run_pair()
    else:
        run_pair()

    sample = _diagnostic_card_two_sample(timing_events, query_events)
    sample.update(
        {
            "mode": mode,
            "overlap_component": overlap_component,
            "same_connection_requested": same_connection,
            "overlap_requested": overlap,
            "overlap_loader_requested": run_overlap_loader,
            "result_count": card_result_count,
            "overlap_result_type": overlap_result_type,
            "errors": errors,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )
    sample.update(_diagnostic_sql_overlap(query_events))
    sample["same_python_connection"] = (
        bool(sample.get("card_two_connection_hashes"))
        and sample.get("card_two_connection_hashes") == sample.get("overlap_connection_hashes")
    )
    sample["same_backend_pid"] = (
        bool(sample.get("card_two_backend_pids"))
        and sample.get("card_two_backend_pids") == sample.get("overlap_backend_pids")
    )
    return sample


def run_card_two_contention_isolation_benchmark(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    modes = [
        {
            "name": "mode_a_alone_shared_connection",
            "overlap_component": "prediction_aggregates",
            "same_connection": False,
            "overlap": False,
            "alone": True,
        },
        {
            "name": "mode_b_same_connection_overlap",
            "overlap_component": "prediction_aggregates",
            "same_connection": True,
            "overlap": True,
            "alone": False,
        },
        {
            "name": "mode_c_separate_connections_concurrent",
            "overlap_component": "prediction_aggregates",
            "same_connection": False,
            "overlap": True,
            "alone": False,
        },
        {
            "name": "mode_d_same_connection_staggered",
            "overlap_component": "prediction_aggregates",
            "same_connection": True,
            "overlap": False,
            "alone": False,
        },
        {
            "name": "mode_e_separate_connections_overlapping",
            "overlap_component": "prediction_aggregates",
            "same_connection": False,
            "overlap": True,
            "alone": False,
        },
        {
            "name": "mode_b_previous_verification_same_connection_overlap",
            "overlap_component": "previous_verification",
            "same_connection": True,
            "overlap": True,
            "alone": False,
        },
        {
            "name": "mode_c_previous_verification_separate_connections_overlap",
            "overlap_component": "previous_verification",
            "same_connection": False,
            "overlap": True,
            "alone": False,
        },
    ]
    results = []
    for mode in modes:
        samples = []
        for _ in range(repetitions):
            if mode.get("alone"):
                samples.append(
                    _run_card_two_contention_sample(
                        mode=mode["name"],
                        overlap_component=mode["overlap_component"],
                        same_connection=False,
                        overlap=False,
                        run_overlap_loader=False,
                    )
                )
            else:
                samples.append(
                    _run_card_two_contention_sample(
                        mode=mode["name"],
                        overlap_component=mode["overlap_component"],
                        same_connection=bool(mode["same_connection"]),
                        overlap=bool(mode["overlap"]),
                    )
                )
        results.append({"mode": mode["name"], "config": mode, "samples": samples})
    return {"status": "ok", "repetitions": repetitions, "modes": results}


def _diagnostic_ordering_relationship(query_events: list[dict[str, Any]]) -> dict[str, Any]:
    card_main = [
        event for event in query_events
        if event.get("label") == "card_two.main_query"
    ]
    card_metadata = [
        event for event in query_events
        if event.get("label") == "card_two.metadata_bulk"
    ]
    overlap_events = [
        event for event in query_events
        if str(event.get("label") or "").startswith("overlap.")
    ]
    first_main = min(card_main, key=lambda event: event.get("started_at", 0), default=None)
    first_overlap = min(overlap_events, key=lambda event: event.get("started_at", 0), default=None)
    return {
        "card_main_started_at": first_main.get("started_at") if first_main else None,
        "card_main_finished_at": first_main.get("finished_at") if first_main else None,
        "first_overlap_started_at": first_overlap.get("started_at") if first_overlap else None,
        "first_overlap_finished_at": first_overlap.get("finished_at") if first_overlap else None,
        "card_main_before_overlap": (
            bool(first_main and first_overlap)
            and first_main.get("finished_at", 0) <= first_overlap.get("started_at", 0)
        ),
        "overlap_before_card_main": (
            bool(first_main and first_overlap)
            and first_overlap.get("started_at", 0) <= first_main.get("started_at", 0)
        ),
        "card_main_overlap_sql": any(
            _query_events_overlap(card, overlap)
            for card in card_main
            for overlap in overlap_events
        ),
        "metadata_overlap_sql": any(
            _query_events_overlap(metadata, overlap)
            for metadata in card_metadata
            for overlap in overlap_events
        ),
    }


def _diagnostic_ordered_card_two(
    *,
    query_events: list[dict[str, Any]],
    after_main,
) -> dict[str, Any]:
    total_started = time.perf_counter()
    with _card_two_dashboard_connection_scope(True):
        main_timing: dict[str, Any] = {"query_tag": "card_two_history.main_query"}
        main_started = time.perf_counter()
        with _diagnostic_query_capture(query_events, "card_two.main_query"):
            rows = _with_card_two_query_timing(
                main_timing,
                lambda: _query_cloud(_prediction_history_summary_cloud_sql(), (100,)),
            )
        main_stage_ms = round((time.perf_counter() - main_started) * 1000, 2)

        records = []
        for row in rows:
            record = _row_to_prediction_summary(row)
            if not is_production_prediction(record):
                continue
            record["read_layer"] = {
                "data_source": "database",
                "table_name": "prediction_history",
                "query_name": "production_prediction_history_summary_v1",
                "production_filtered": True,
            }
            records.append(record)

        if after_main is not None:
            after_main()

        metadata_timing: dict[str, Any] = {"query_tag": "card_two_history.metadata_bulk"}
        metadata_started = time.perf_counter()
        with _diagnostic_query_capture(query_events, "card_two.metadata_bulk"):
            metadata_by_record, metadata_queries = _with_card_two_query_timing(
                metadata_timing,
                lambda: _prediction_event_metadata_bulk(records),
            )
        metadata_stage_ms = round((time.perf_counter() - metadata_started) * 1000, 2)
        enriched = [
            _enrich_prediction_metadata_from_map(record, metadata_by_record.get(id(record)))
            for record in records
        ]
    return {
        "total_ms": round((time.perf_counter() - total_started) * 1000, 2),
        "rows": len(enriched),
        "metadata_queries": metadata_queries,
        "main_query": {
            "stage_ms": main_stage_ms,
            **main_timing,
        },
        "metadata_bulk": {
            "stage_ms": metadata_stage_ms,
            **metadata_timing,
        },
    }


def _run_card_two_ordering_sample(mode: str) -> dict[str, Any]:
    query_events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    overlap_result_type = None
    overlap_started_event = threading.Event()
    overlap_future = None
    start_barrier = threading.Event()
    started = time.perf_counter()

    def run_overlap(wait_for_barrier: bool = False) -> None:
        nonlocal overlap_result_type
        try:
            if wait_for_barrier:
                start_barrier.wait(timeout=5)
            with _diagnostic_query_capture(
                query_events,
                "overlap.prediction_aggregates",
                overlap_started_event,
            ):
                result = get_prediction_lifecycle_aggregates(diagnostic_component="prediction_aggregates")
            overlap_result_type = type(result).__name__
        except Exception as exc:
            errors["overlap"] = type(exc).__name__

    def run_card(after_main=None, wait_for_barrier: bool = False) -> dict[str, Any] | None:
        try:
            if wait_for_barrier:
                start_barrier.wait(timeout=5)
            return _diagnostic_ordered_card_two(
                query_events=query_events,
                after_main=after_main,
            )
        except Exception as exc:
            errors["card_two"] = type(exc).__name__
            return None

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="card-two-ordering") as executor:
        if mode == "mode_a_card_two_alone":
            card_two = run_card()
        elif mode == "mode_b_card_two_main_first":
            def after_main():
                nonlocal overlap_future
                overlap_future = executor.submit(run_overlap)

            card_two = run_card(after_main=after_main)
            if overlap_future is not None:
                overlap_future.result(timeout=30)
        elif mode == "mode_c_db_heavy_first":
            overlap_future = executor.submit(run_overlap)
            ordering_verified = overlap_started_event.wait(timeout=10)
            card_two = run_card()
            overlap_future.result(timeout=30)
            query_events.append(
                {
                    "label": "diagnostic.ordering_marker",
                    "ordering": "db_heavy_first",
                    "verified": ordering_verified,
                }
            )
        elif mode == "mode_d_concurrent_start":
            overlap_future = executor.submit(run_overlap, True)
            card_future = executor.submit(run_card, None, True)
            start_barrier.set()
            card_two = card_future.result(timeout=30)
            overlap_future.result(timeout=30)
        else:
            raise ValueError(f"unknown ordering benchmark mode: {mode}")

    sample = dict(card_two or {})
    sample.update(
        {
            "mode": mode,
            "errors": errors,
            "overlap_result_type": overlap_result_type,
            "query_events": query_events,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )
    sample.update(_diagnostic_sql_overlap(query_events))
    sample.update(_diagnostic_ordering_relationship(query_events))
    if mode == "mode_a_card_two_alone":
        sample["ordering_verified"] = True
    elif mode == "mode_b_card_two_main_first":
        sample["ordering_verified"] = bool(sample.get("card_main_before_overlap"))
    elif mode == "mode_c_db_heavy_first":
        sample["ordering_verified"] = bool(sample.get("overlap_before_card_main"))
    elif mode == "mode_d_concurrent_start":
        sample["ordering_verified"] = True
    return sample


def run_card_two_ordering_benchmark(repetitions: int = 5) -> dict[str, Any]:
    repetitions = max(1, min(int(repetitions or 5), 5))
    modes = [
        "mode_a_card_two_alone",
        "mode_b_card_two_main_first",
        "mode_c_db_heavy_first",
        "mode_d_concurrent_start",
    ]
    return {
        "status": "ok",
        "repetitions": repetitions,
        "modes": [
            {
                "mode": mode,
                "samples": [_run_card_two_ordering_sample(mode) for _ in range(repetitions)],
            }
            for mode in modes
        ],
    }


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    if _cloud_enabled():
        try:
            rows = _query_cloud(sql, params)
            if rows:
                return rows
        except Exception:
            logger.exception("cloud prediction_history query failed")
    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite prediction_history query failed")
        return []


def _log_dashboard_stage(component: str, stage: str, started: float, result: str = "success", **extra: Any) -> None:
    fields = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
    suffix = f" {fields}" if fields else ""
    logger.warning(
        "component_stage_latency component=%s stage=%s duration_ms=%s result=%s%s",
        component,
        stage,
        round((time.perf_counter() - started) * 1000, 2),
        result,
        suffix,
    )


def _timed_dashboard_stage(component: str, stage: str, fn):
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        _log_dashboard_stage(component, stage, started, "failed", error_type=type(exc).__name__)
        raise
    _log_dashboard_stage(component, stage, started, "success")
    return result


def _maybe_timed_dashboard_stage(component: str | None, stage: str, fn):
    if component:
        return _timed_dashboard_stage(component, stage, fn)
    return fn()


def _with_card_two_query_timing(timing: dict[str, Any] | None, fn):
    if timing is None:
        return fn()
    token = _CARD_TWO_QUERY_TIMING.set(timing)
    try:
        return fn()
    finally:
        _CARD_TWO_QUERY_TIMING.reset(token)


@contextmanager
def _card_two_dashboard_connection_scope(enabled: bool):
    if not enabled or not _cloud_enabled():
        yield
        return

    existing_conn = _CARD_TWO_DASHBOARD_CONNECTION.get()
    existing_state = _CARD_TWO_DASHBOARD_CONNECTION_STATE.get()
    if existing_conn is not None and existing_state is not None:
        context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
        if context is not None:
            context["component_pre_db_ms"] = round((time.perf_counter() - context["worker_started_at"]) * 1000, 2)
            context["connection_checkout_ms"] = existing_state.get("connect_ms", 0.0)
            context["connection_hash"] = _connection_hash(existing_conn)
            context["checkout_status"] = _transaction_status(existing_conn)
            context["backend_pid"] = getattr(getattr(existing_conn, "info", None), "backend_pid", None)
            context["autocommit"] = getattr(existing_conn, "autocommit", None)
        try:
            yield
        finally:
            context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
            if context is not None:
                context["db_scope_exited_at"] = time.perf_counter()
                if context.get("card_two_fetch_finished_at") is not None:
                    context["db_post_fetch_ms"] = round(
                        (context["db_scope_exited_at"] - context["card_two_fetch_finished_at"]) * 1000,
                        2,
                    )
        return

    state: dict[str, Any] = {
        "cloud_available": True,
        "connect_ms": 0.0,
        "connect_reported": False,
        "opened_at": None,
    }
    acquired = False
    try:
        acquire_started = time.perf_counter()
        context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
        if context is not None:
            context["component_pre_db_ms"] = round((acquire_started - context["worker_started_at"]) * 1000, 2)
        with _dashboard_read_connection() as conn:
            acquired = True
            state["connect_ms"] = round((time.perf_counter() - acquire_started) * 1000, 2)
            state["opened_at"] = time.perf_counter()
            context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
            if context is not None:
                context["connection_checkout_ms"] = state["connect_ms"]
                context["connection_hash"] = _connection_hash(conn)
                context["checkout_status"] = _transaction_status(conn)
                context["backend_pid"] = getattr(getattr(conn, "info", None), "backend_pid", None)
                context["autocommit"] = getattr(conn, "autocommit", None)
            conn_token = _CARD_TWO_DASHBOARD_CONNECTION.set(conn)
            state_token = _CARD_TWO_DASHBOARD_CONNECTION_STATE.set(state)
            try:
                yield
            finally:
                context = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.get()
                if context is not None:
                    context["db_scope_exited_at"] = time.perf_counter()
                    if context.get("card_two_fetch_finished_at") is not None:
                        context["db_post_fetch_ms"] = round(
                            (context["db_scope_exited_at"] - context["card_two_fetch_finished_at"]) * 1000,
                            2,
                        )
                _CARD_TWO_DASHBOARD_CONNECTION.reset(conn_token)
                _CARD_TWO_DASHBOARD_CONNECTION_STATE.reset(state_token)
    except Exception as exc:
        if acquired:
            raise
        logger.warning(
            "cloud prediction_history dashboard read connection failed error_type=%s",
            type(exc).__name__,
        )
        state["cloud_available"] = False
        state["error_type"] = type(exc).__name__
        conn_token = _CARD_TWO_DASHBOARD_CONNECTION.set(None)
        state_token = _CARD_TWO_DASHBOARD_CONNECTION_STATE.set(state)
        try:
            yield
        finally:
            _CARD_TWO_DASHBOARD_CONNECTION.reset(conn_token)
            _CARD_TWO_DASHBOARD_CONNECTION_STATE.reset(state_token)


def _record_card_two_history_timing(payload: dict[str, Any]) -> None:
    event = dict(payload)
    event.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    diagnostic_events = _DIAGNOSTIC_CARD_TWO_TIMING_EVENTS.get()
    if diagnostic_events is not None:
        diagnostic_events.append(deepcopy(event))
    with _CARD_TWO_HISTORY_TIMING_LOCK:
        _CARD_TWO_HISTORY_TIMINGS.append(event)
        del _CARD_TWO_HISTORY_TIMINGS[:-_CARD_TWO_HISTORY_TIMING_LIMIT]


@contextmanager
def card_two_dashboard_execution_context(queue_ms: float | None = None):
    context = {
        "type": "dashboard_context",
        "component": "card_two_history",
        "executor_queue_ms": round(queue_ms, 2) if queue_ms is not None else None,
        "process_id": os.getpid(),
        "thread_id": threading.get_ident(),
        "worker_started_at": time.perf_counter(),
    }
    token = _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.set(context)
    try:
        yield
        context["result"] = "success"
    except Exception as exc:
        context["result"] = "failed"
        context["error_type"] = type(exc).__name__
        raise
    finally:
        now = time.perf_counter()
        context["component_total_execution_ms"] = round((now - context["worker_started_at"]) * 1000, 2)
        if context.get("db_scope_exited_at") is not None:
            context["component_post_db_ms"] = round((now - context["db_scope_exited_at"]) * 1000, 2)
        for key in (
            "worker_started_at",
            "card_two_fetch_finished_at",
            "db_scope_exited_at",
        ):
            context.pop(key, None)
        _record_card_two_history_timing(context)
        _CARD_TWO_DASHBOARD_EXECUTION_CONTEXT.reset(token)


def get_card_two_history_timing_status() -> dict[str, Any]:
    with _CARD_TWO_HISTORY_TIMING_LOCK:
        recent = deepcopy(_CARD_TWO_HISTORY_TIMINGS)
    return {
        "latest": recent[-1] if recent else None,
        "recent": recent,
        "limit": _CARD_TWO_HISTORY_TIMING_LIMIT,
    }


def _log_card_two_history_stage(stage: str, started: float, result: str = "success", **extra: Any) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    _record_card_two_history_timing(
        {
            "type": "stage",
            "stage": stage,
            "duration_ms": duration_ms,
            "result": result,
            **{key: value for key, value in extra.items() if value is not None},
        }
    )
    fields = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
    suffix = f" {fields}" if fields else ""
    logger.warning(
        "card_two_history_stage_latency stage=%s duration_ms=%s result=%s%s",
        stage,
        duration_ms,
        result,
        suffix,
    )


def _maybe_timed_card_two_history_stage(enabled: bool, stage: str, fn):
    if not enabled:
        return fn()
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        _log_card_two_history_stage(stage, started, "failed", error_type=type(exc).__name__)
        raise
    _log_card_two_history_stage(stage, started, "success")
    return result


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
        "production_generation": row[38] if len(row) > 38 and row[38] is not None else get_production_generation(),
        "production_valid": bool(row[39]) if len(row) > 39 else True,
        "release_version": row[40] if len(row) > 40 else RELEASE_VERSION,
        "git_commit_hash": row[41] if len(row) > 41 else GIT_COMMIT_HASH,
        "model_version": row[42] if len(row) > 42 else MODEL_VERSION,
        "feature_version": row[43] if len(row) > 43 else FEATURE_VERSION,
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
    return _prediction_event_metadata_from_message(rows[0][0])


def _prediction_event_metadata_from_message(message: Any) -> dict:
    payload = _json_loads(message)
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


def _prediction_event_metadata_bulk(records: list[dict]) -> tuple[dict[int, dict], int]:
    lookup_records = []
    for index, record in enumerate(records):
        based_on = str(record.get("issue") or "")
        target = str(record.get("prediction_issue") or "")
        if based_on or target:
            lookup_records.append((index, record, based_on, target))
    if not lookup_records:
        return {}, 0

    queries = 0
    rows: list[Any] = []
    cloud_params: list[Any] = []
    cloud_values = []
    for index, _, based_on, target in lookup_records:
        cloud_values.append("(%s, %s, %s)")
        cloud_params.extend((index, based_on, target))
    cloud_sql = f"""
        with lookup(idx, based_on, target) as (
            values {', '.join(cloud_values)}
        ),
        candidates as (
            select lookup.idx, event.message, event.created_at, event.id
            from lookup
            join operation_events event
              on lookup.based_on <> ''
             and event.issue = lookup.based_on
            where event.event_type = 'prediction_created'
            union all
            select lookup.idx, event.message, event.created_at, event.id
            from lookup
            join operation_events event
              on lookup.target <> ''
             and event.message like ('%%' || lookup.target || '%%')
            where event.event_type = 'prediction_created'
        ),
        ranked as (
            select idx, message,
                   row_number() over (
                       partition by idx
                       order by created_at desc, id desc
                   ) as rn
            from candidates
        )
        select lookup.idx, ranked.message
        from lookup
        left join ranked on ranked.idx = lookup.idx and ranked.rn = 1
        where ranked.message is not null
        order by lookup.idx
        """

    sqlite_params: list[Any] = []
    sqlite_values = []
    for index, _, based_on, target in lookup_records:
        sqlite_values.append("(?, ?, ?)")
        sqlite_params.extend((index, based_on, target))
    sqlite_sql = f"""
        with lookup(idx, based_on, target) as (
            values {', '.join(sqlite_values)}
        ),
        candidates as (
            select lookup.idx, event.message, event.created_at, event.id
            from lookup
            join operation_events event
              on lookup.based_on <> ''
             and event.issue = lookup.based_on
            where event.event_type = 'prediction_created'
            union all
            select lookup.idx, event.message, event.created_at, event.id
            from lookup
            join operation_events event
              on lookup.target <> ''
             and event.message like '%' || lookup.target || '%'
            where event.event_type = 'prediction_created'
        ),
        ranked as (
            select idx, message,
                   row_number() over (
                       partition by idx
                       order by created_at desc, id desc
                   ) as rn
            from candidates
        )
        select lookup.idx, ranked.message
        from lookup
        left join ranked on ranked.idx = lookup.idx and ranked.rn = 1
        where ranked.message is not null
        order by lookup.idx
        """

    if _cloud_enabled():
        queries += 1
        try:
            rows = _query_cloud(cloud_sql, tuple(cloud_params))
        except Exception:
            logger.exception("cloud prediction_history metadata query failed")
        if rows:
            return _metadata_map_from_indexed_rows(records, rows), queries

    queries += 1
    try:
        rows = _query_sqlite(sqlite_sql, tuple(sqlite_params))
    except Exception:
        logger.exception("sqlite prediction_history metadata query failed")
        return {}, queries
    return _metadata_map_from_indexed_rows(records, rows), queries


def _metadata_map_from_indexed_rows(records: list[dict], rows: list[Any]) -> dict[int, dict]:
    metadata_by_record: dict[int, dict] = {}
    for row in rows:
        try:
            record = records[int(row[0])]
        except Exception:
            continue
        metadata = _prediction_event_metadata_from_message(row[1] if len(row) > 1 else None)
        if metadata:
            metadata_by_record[id(record)] = metadata
    return metadata_by_record


def _enrich_prediction_metadata(record: dict) -> dict:
    metadata = _prediction_event_metadata(record)
    return _enrich_prediction_metadata_from_map(record, metadata)


def _enrich_prediction_metadata_from_map(record: dict, metadata: dict | None) -> dict:
    metadata = metadata or {}
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
        verification_version, learning_used, model_score,
        production_generation, production_valid, release_version,
        git_commit_hash, model_version, feature_version
"""

PREDICTION_SELECT_COLUMNS_P = """
        p.id, p.issue, p.prediction_issue, p.predict_time, p.strategy, p.confidence,
        p.recommend_numbers, p.super_number, p.three_star, p.four_star, p.twins,
        p.consecutive, p.patch_numbers, p.tails, p.big_small, p.odd_even, p.reasons,
        p.winning_numbers, p.hit_count, p.super_hit, p.three_star_hit, p.four_star_hit,
        p.accuracy, p.created_at, p.updated_at, p.model_scores, p.winning_model,
        p.prediction_status, p.verified_issue, p.verified_at, p.matched_numbers,
        p.missed_numbers, p.prediction_count, p.hit_rate, p.super_number_hit,
        p.verification_version, p.learning_used, p.model_score,
        p.production_generation, p.production_valid, p.release_version,
        p.git_commit_hash, p.model_version, p.feature_version
"""

PREDICTION_SUMMARY_COLUMNS = (
    "id",
    "issue",
    "prediction_issue",
    "predict_time",
    "strategy",
    "confidence",
    "recommend_numbers",
    "super_number",
    "three_star",
    "four_star",
    "twins",
    "consecutive",
    "patch_numbers",
    "tails",
    "big_small",
    "odd_even",
    "winning_numbers",
    "hit_count",
    "super_hit",
    "three_star_hit",
    "four_star_hit",
    "accuracy",
    "created_at",
    "updated_at",
    "winning_model",
    "prediction_status",
    "verified_issue",
    "verified_at",
    "matched_numbers",
    "missed_numbers",
    "prediction_count",
    "hit_rate",
    "super_number_hit",
    "learning_used",
    "model_score",
    "production_generation",
    "production_valid",
    "release_version",
)
PREDICTION_SUMMARY_SELECT_COLUMNS = ",\n        ".join(PREDICTION_SUMMARY_COLUMNS)
PREDICTION_SUMMARY_SELECT_COLUMNS_P = ",\n        ".join(f"p.{column}" for column in PREDICTION_SUMMARY_COLUMNS)


def _row_to_prediction_summary(row: Any) -> dict:
    data = dict(zip(PREDICTION_SUMMARY_COLUMNS, row))
    recommend_numbers = _normalize_numbers(_json_loads(data.get("recommend_numbers")) or [])
    winning_numbers = _normalize_numbers(_json_loads(data.get("winning_numbers")) or [])
    matched_numbers = _normalize_numbers(_json_loads(data.get("matched_numbers")) or [])
    missed_numbers = _normalize_numbers(_json_loads(data.get("missed_numbers")) or [])
    if winning_numbers and not matched_numbers:
        winning_set = set(_as_int_list(winning_numbers))
        matched_numbers = [number for number in _as_int_list(recommend_numbers) if number in winning_set]
    if winning_numbers and not missed_numbers:
        winning_set = set(_as_int_list(winning_numbers))
        missed_numbers = [number for number in _as_int_list(recommend_numbers) if number not in winning_set]
    raw_status = data.get("prediction_status")
    effective_status = _prediction_status(raw_status, bool(winning_numbers))
    return {
        "id": data.get("id"),
        "issue": data.get("issue"),
        "prediction_issue": data.get("prediction_issue"),
        "predict_time": str(data["predict_time"]) if data.get("predict_time") is not None else None,
        "strategy": data.get("strategy"),
        "confidence": data.get("confidence"),
        "recommend_numbers": recommend_numbers,
        "super_number": data.get("super_number"),
        "three_star": _normalize_numbers(_json_loads(data.get("three_star")) or []),
        "four_star": _normalize_numbers(_json_loads(data.get("four_star")) or []),
        "twins": _json_loads(data.get("twins")) or [],
        "consecutive": _json_loads(data.get("consecutive")) or [],
        "patch_numbers": _normalize_numbers(_json_loads(data.get("patch_numbers")) or []),
        "tails": _json_loads(data.get("tails")) or [],
        "big_small": data.get("big_small"),
        "odd_even": data.get("odd_even"),
        "reasons": [],
        "winning_numbers": winning_numbers,
        "hit_count": data.get("hit_count") or 0,
        "super_hit": bool(data.get("super_hit")),
        "three_star_hit": bool(data.get("three_star_hit")),
        "four_star_hit": bool(data.get("four_star_hit")),
        "accuracy": data.get("accuracy") or 0,
        "created_at": str(data["created_at"]) if data.get("created_at") is not None else None,
        "updated_at": str(data["updated_at"]) if data.get("updated_at") is not None else None,
        "model_scores": {},
        "winning_model": data.get("winning_model"),
        "prediction_status": effective_status,
        "verified_issue": data.get("verified_issue"),
        "verified_at": str(data["verified_at"]) if data.get("verified_at") is not None else None,
        "matched_numbers": matched_numbers or [],
        "missed_numbers": missed_numbers or [],
        "prediction_count": data.get("prediction_count") if data.get("prediction_count") is not None else len(recommend_numbers or []),
        "hit_rate": data.get("hit_rate") if data.get("hit_rate") is not None else (data.get("accuracy") or 0),
        "super_number_hit": bool(data.get("super_number_hit")) if data.get("super_number_hit") is not None else bool(data.get("super_hit")),
        "verification_version": None,
        "learning_used": bool(data.get("learning_used")),
        "model_score": data.get("model_score"),
        "production_generation": data.get("production_generation") if data.get("production_generation") is not None else get_production_generation(),
        "production_valid": bool(data.get("production_valid")) if data.get("production_valid") is not None else True,
        "release_version": data.get("release_version") or RELEASE_VERSION,
        "git_commit_hash": None,
        "model_version": None,
        "feature_version": None,
    }


def get_latest_prediction_history() -> dict | None:
    _ensure_initialized()
    cloud_sql = """
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
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH)
    sqlite_sql = """
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
        """.format(columns=PREDICTION_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH)
    rows = _query_with_fallback(cloud_sql, sqlite_sql=sqlite_sql)
    if _cloud_enabled():
        try:
            rows = list(rows or []) + _query_sqlite(sqlite_sql)
        except Exception:
            logger.exception("sqlite prediction_history latest sidecar query failed")
    if not rows:
        return None
    records = [_row_to_prediction(row) for row in rows]
    records = [record for record in records if is_production_prediction(record)]
    if not records:
        return None
    record = max(
        records,
        key=lambda item: (
            int(item.get("prediction_issue") or 0),
            str(item.get("created_at") or ""),
            int(item.get("id") or 0),
        ),
    )
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
        if not is_production_prediction(record):
            continue
        record["read_layer"] = {
            "data_source": "database",
            "table_name": "prediction_history",
            "query_name": "production_prediction_history_v2",
            "production_filtered": True,
        }
        records.append(_enrich_prediction_metadata(record))
    return records


def get_prediction_history_summary_records(limit: int = 100, *, diagnostic_component: str | None = None) -> list[dict]:
    total_started = time.perf_counter()
    diagnostics_enabled = diagnostic_component == "card_two_history"
    _ensure_initialized()
    limit = max(1, min(int(limit or 100), 500))
    with _card_two_dashboard_connection_scope(diagnostics_enabled):
        return _get_prediction_history_summary_records_loaded(
            limit,
            diagnostic_component=diagnostic_component,
            diagnostics_enabled=diagnostics_enabled,
            total_started=total_started,
        )


def _get_prediction_history_summary_records_loaded(
    limit: int,
    *,
    diagnostic_component: str | None,
    diagnostics_enabled: bool,
    total_started: float,
) -> list[dict]:
    main_query_started = time.perf_counter()
    main_query_timing: dict[str, Any] = {"query_tag": "card_two_history.main_query"}
    rows = _maybe_timed_dashboard_stage(
        diagnostic_component,
        "prediction_history_summary_query",
        lambda: _with_card_two_query_timing(
            main_query_timing if diagnostics_enabled else None,
            lambda: _query_with_fallback(
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
            """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
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
            """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
            ),
        ),
    )
    if diagnostics_enabled:
        _log_card_two_history_stage("main_query", main_query_started, db_timing=main_query_timing or None)
    def transform_rows() -> list[dict]:
        transformed = []
        for row in rows:
            record = _row_to_prediction_summary(row)
            if not is_production_prediction(record):
                continue
            record["read_layer"] = {
                "data_source": "database",
                "table_name": "prediction_history",
                "query_name": "production_prediction_history_summary_v1",
                "production_filtered": True,
            }
            transformed.append(record)
        return transformed

    records = _maybe_timed_card_two_history_stage(diagnostics_enabled, "transform", transform_rows)
    metadata_query_timing: dict[str, Any] = {"query_tag": "card_two_history.metadata_bulk"}
    metadata_by_record, metadata_queries = _maybe_timed_card_two_history_stage(
        diagnostics_enabled,
        "metadata_bulk",
        lambda: _with_card_two_query_timing(
            metadata_query_timing if diagnostics_enabled else None,
            lambda: _prediction_event_metadata_bulk(records),
        ),
    )
    enriched = [
        _enrich_prediction_metadata_from_map(record, metadata_by_record.get(id(record)))
        for record in records
    ]
    if diagnostics_enabled:
        if metadata_query_timing:
            for event in reversed(_CARD_TWO_HISTORY_TIMINGS):
                if event.get("type") == "stage" and event.get("stage") == "metadata_bulk":
                    event["db_timing"] = dict(metadata_query_timing)
                    break
            diagnostic_events = _DIAGNOSTIC_CARD_TWO_TIMING_EVENTS.get()
            if diagnostic_events is not None:
                for event in reversed(diagnostic_events):
                    if event.get("type") == "stage" and event.get("stage") == "metadata_bulk":
                        event["db_timing"] = dict(metadata_query_timing)
                        break
        total_ms = round((time.perf_counter() - total_started) * 1000, 2)
        _record_card_two_history_timing(
            {
                "type": "summary",
                "total_ms": total_ms,
                "rows": len(enriched),
                "metadata_queries": metadata_queries,
            }
        )
        logger.warning(
            "card_two_history_summary_latency total_ms=%s rows=%s metadata_queries=%s",
            total_ms,
            len(enriched),
            metadata_queries,
        )
    return enriched


def get_prediction_history_count() -> int:
    _ensure_initialized()
    rows = _query_with_fallback("select count(*) from prediction_history")
    if not rows:
        return 0
    try:
        return int(rows[0][0] or 0)
    except Exception:
        return 0


def get_prediction_lifecycle_aggregates(*, diagnostic_component: str | None = None) -> dict:
    learned_count = 0
    try:
        from database.learning_store import get_learned_live_target_count

        learned_count = _maybe_timed_dashboard_stage(
            diagnostic_component,
            "learned_live_target_count",
            get_learned_live_target_count,
        )
    except Exception:
        logger.exception("learned live target count failed")

    rows = _maybe_timed_dashboard_stage(
        diagnostic_component,
        "prediction_history_aggregate_query",
        lambda: _query_with_fallback(
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
        ),
    )
    row = rows[0] if rows else [0] * 7
    official_rows = _maybe_timed_dashboard_stage(
        diagnostic_component,
        "official_result_join_count",
        lambda: _query_with_fallback(
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
        ),
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


def get_prediction_summary_for_source_target(source_issue: str, target_issue: str) -> dict | None:
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
        """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS),
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
        """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS),
    )
    if not rows:
        return None
    record = _row_to_prediction_summary(rows[0])
    record["read_layer"] = {
        "data_source": "database",
        "table_name": "prediction_history",
        "query_name": "prediction_for_source_target_summary_v1",
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
    target_issue = str(int(source_issue) + 1) if source_issue else None
    if prediction is None and source_issue and target_issue:
        prediction = get_prediction_for_source_target(source_issue, target_issue)
    return {
        "draw": draw,
        "prediction": prediction,
        "target_issue": target_issue,
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


def get_latest_verified_prediction_summary_at_or_before(issue: str) -> dict | None:
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
        """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
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
        """.format(columns=PREDICTION_SUMMARY_SELECT_COLUMNS_P, min_issue_length=MIN_PRODUCTION_ISSUE_LENGTH),
    )
    if not rows:
        return None
    record = _row_to_prediction_summary(rows[0])
    record["read_layer"] = {
        "data_source": "database",
        "table_name": "prediction_history",
        "query_name": "latest_verified_prediction_summary_at_or_before",
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
    all_records = get_prediction_history_summary_records(limit)
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
