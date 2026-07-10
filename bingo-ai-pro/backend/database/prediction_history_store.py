from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"


def _now() -> str:
    return datetime.utcnow().isoformat()


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


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_prediction_history_tables() -> dict:
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
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite prediction_history table")

    return results


def _prediction_params(item: dict) -> tuple:
    return (
        item.get("issue"),
        item.get("prediction_issue"),
        item.get("predict_time") or _now(),
        item.get("strategy"),
        item.get("confidence"),
        _json_dumps(item.get("recommend_numbers", [])),
        item.get("super_number"),
        _json_dumps(item.get("three_star", [])),
        _json_dumps(item.get("four_star", [])),
        _json_dumps(item.get("twins", [])),
        _json_dumps(item.get("consecutive", [])),
        _json_dumps(item.get("patch_numbers", [])),
        _json_dumps(item.get("tails", [])),
        item.get("big_small"),
        item.get("odd_even"),
        _json_dumps(item.get("reasons", [])),
        _json_dumps(item.get("model_scores", {})),
        item.get("winning_model"),
    )


def save_prediction_history(item: dict) -> dict:
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
                            model_scores, winning_model, updated_at
                        )
                        values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb,
                                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                                %s::jsonb, %s, now())
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
                            updated_at = now()
                        returning id
                        """,
                        _prediction_params(item),
                        prepare=False,
                    )
                    row_id = int(cur.fetchone()[0])
                conn.commit()
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
                    model_scores, winning_model, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    updated_at = excluded.updated_at
                """,
                (*_prediction_params(item), _now()),
            )
            row_id = int(cursor.lastrowid or 0)
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
    return {
        "id": row[0],
        "issue": row[1],
        "prediction_issue": row[2],
        "predict_time": str(row[3]) if row[3] is not None else None,
        "strategy": row[4],
        "confidence": row[5],
        "recommend_numbers": _json_loads(row[6]) or [],
        "super_number": row[7],
        "three_star": _json_loads(row[8]) or [],
        "four_star": _json_loads(row[9]) or [],
        "twins": _json_loads(row[10]) or [],
        "consecutive": _json_loads(row[11]) or [],
        "patch_numbers": _json_loads(row[12]) or [],
        "tails": _json_loads(row[13]) or [],
        "big_small": row[14],
        "odd_even": row[15],
        "reasons": _json_loads(row[16]) or [],
        "winning_numbers": _json_loads(row[17]) or [],
        "hit_count": row[18] or 0,
        "super_hit": bool(row[19]),
        "three_star_hit": bool(row[20]),
        "four_star_hit": bool(row[21]),
        "accuracy": row[22] or 0,
        "created_at": str(row[23]) if row[23] is not None else None,
        "updated_at": str(row[24]) if row[24] is not None else None,
        "model_scores": _json_loads(row[25]) if len(row) > 25 else {},
        "winning_model": row[26] if len(row) > 26 else None,
    }


def get_latest_prediction_history() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, prediction_issue, predict_time, strategy, confidence,
               recommend_numbers, super_number, three_star, four_star, twins,
               consecutive, patch_numbers, tails, big_small, odd_even, reasons,
               winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
               accuracy, created_at, updated_at, model_scores, winning_model
        from prediction_history
        order by prediction_issue desc, updated_at desc
        limit 1
        """,
    )
    return _row_to_prediction(rows[0]) if rows else None


def get_prediction_history_records(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    rows = _query_with_fallback(
        """
        select id, issue, prediction_issue, predict_time, strategy, confidence,
               recommend_numbers, super_number, three_star, four_star, twins,
               consecutive, patch_numbers, tails, big_small, odd_even, reasons,
               winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
               accuracy, created_at, updated_at, model_scores, winning_model
        from prediction_history
        order by prediction_issue desc, updated_at desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, prediction_issue, predict_time, strategy, confidence,
               recommend_numbers, super_number, three_star, four_star, twins,
               consecutive, patch_numbers, tails, big_small, odd_even, reasons,
               winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
               accuracy, created_at, updated_at, model_scores, winning_model
        from prediction_history
        order by prediction_issue desc, updated_at desc
        limit ?
        """,
    )
    return [_row_to_prediction(row) for row in rows]


def get_prediction_history_count() -> int:
    rows = _query_with_fallback("select count(*) from prediction_history")
    if not rows:
        return 0
    try:
        return int(rows[0][0] or 0)
    except Exception:
        return 0


def update_prediction_history_result(actual: dict) -> dict:
    issue = str(actual.get("issue"))
    winning_numbers = [int(n) for n in actual.get("numbers") or []]
    actual_super = actual.get("super_number")
    updated = 0
    for item in get_prediction_history_records(200):
        if str(item.get("prediction_issue")) != issue:
            continue
        recommended = [int(n) for n in item.get("recommend_numbers") or []]
        hit_count = len(set(recommended) & set(winning_numbers))
        super_hit = bool(actual_super is not None and item.get("super_number") == actual_super)
        three_star_hit = len(set(item.get("three_star") or []) & set(winning_numbers)) >= 3
        four_star_hit = len(set(item.get("four_star") or []) & set(winning_numbers)) >= 4
        accuracy = round(hit_count / max(1, len(recommended)), 4)
        winning_model = _winning_model(item.get("model_scores") or {}, winning_numbers)
        params = (
            _json_dumps(winning_numbers),
            hit_count,
            super_hit,
            three_star_hit,
            four_star_hit,
            accuracy,
            winning_model,
            _now(),
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
                                updated_at = %s
                            where id = %s
                            """,
                            params,
                            prepare=False,
                        )
                    conn.commit()
                updated += 1
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
                        item.get("id"),
                    ),
                )
            updated += 1
        except Exception:
            logger.exception("sqlite prediction_history result update failed")
    return {"status": "ok", "updated": updated}


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


def _as_int_list(values: Any) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            numbers.append(number)
    return numbers


def get_prediction_history_statistics(limit: int = 100) -> dict:
    records = [item for item in get_prediction_history_records(limit) if item.get("winning_numbers")]
    total = len(records)
    if not total:
        return {
            "status": "empty",
            "message": "尚未累積 AI 預測紀錄，系統已開始保存後續推薦。",
            "sample_size": 0,
            "three_star_rate": 0,
            "four_star_rate": 0,
            "five_star_rate": 0,
            "super_hit_rate": 0,
            "average_hits": 0,
        }
    return {
        "status": "ok",
        "sample_size": total,
        "three_star_rate": round(sum(1 for item in records if item.get("three_star_hit")) / total * 100, 2),
        "four_star_rate": round(sum(1 for item in records if item.get("four_star_hit")) / total * 100, 2),
        "five_star_rate": round(sum(1 for item in records if (item.get("hit_count") or 0) >= 5) / total * 100, 2),
        "super_hit_rate": round(sum(1 for item in records if item.get("super_hit")) / total * 100, 2),
        "average_hits": round(sum(item.get("hit_count") or 0 for item in records) / total, 2),
    }
