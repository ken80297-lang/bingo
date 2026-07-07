from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
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
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_analysis_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists analysis_history (
                        issue text primary key,
                        draw_time text,
                        numbers jsonb,
                        super_number integer,
                        big_small text,
                        odd_even text,
                        consecutive_numbers jsonb,
                        repeated_numbers jsonb,
                        hot_numbers jsonb,
                        cold_numbers jsonb,
                        missing_numbers jsonb,
                        difference_values jsonb,
                        diagonal_pattern jsonb,
                        laowanjia_score jsonb,
                        ai_score jsonb,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud analysis_history table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists analysis_history (
                    issue text primary key,
                    draw_time text,
                    numbers text,
                    super_number integer,
                    big_small text,
                    odd_even text,
                    consecutive_numbers text,
                    repeated_numbers text,
                    hot_numbers text,
                    cold_numbers text,
                    missing_numbers text,
                    difference_values text,
                    diagonal_pattern text,
                    laowanjia_score text,
                    ai_score text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite analysis_history table")

    return results


def _as_int_list(values: Any) -> list[int]:
    result = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            result.append(number)
    return result


def _recent_draws(limit: int = 120) -> list[dict]:
    try:
        from database.collector_store import get_draw_history

        return get_draw_history(limit)
    except Exception:
        logger.exception("failed to load recent draw history for analysis")
        return []


def build_analysis_record(draw: dict, recent_draws: list[dict] | None = None) -> dict:
    numbers = sorted(_as_int_list(draw.get("numbers")))
    recent = recent_draws if recent_draws is not None else _recent_draws()
    previous_numbers = _as_int_list(recent[0].get("numbers")) if recent else []

    all_numbers = []
    for item in recent:
        all_numbers.extend(_as_int_list(item.get("numbers")))
    if not all_numbers:
        all_numbers = numbers

    counter = Counter(all_numbers)
    hot_numbers = [number for number, _ in counter.most_common(10)]
    cold_numbers = [number for number, _ in counter.most_common()[-10:]]

    consecutive_numbers = []
    for number in numbers:
        if number + 1 in numbers:
            consecutive_numbers.append([number, number + 1])

    repeated_numbers = sorted(set(numbers) & set(previous_numbers))
    missing_numbers = [number for number in range(1, 81) if number not in set(all_numbers)][:30]

    difference_values = {}
    for previous in previous_numbers:
        for diff in [1, -1, 9, -9, 10, -10, 11, -11]:
            candidate = previous + diff
            if 1 <= candidate <= 80:
                difference_values.setdefault(str(diff), []).append(candidate)
    difference_values = {
        key: sorted(set(values))
        for key, values in difference_values.items()
    }

    diagonal_pattern = []
    for number in numbers:
        if number + 9 in numbers:
            diagonal_pattern.append([number, number + 9])
        if number + 11 in numbers:
            diagonal_pattern.append([number, number + 11])

    laowanjia_score = {
        "hot": len(set(numbers) & set(hot_numbers)),
        "repeat": len(repeated_numbers),
        "diagonal": len(diagonal_pattern),
        "consecutive": len(consecutive_numbers),
    }
    ai_score = {
        "score": min(
            100,
            laowanjia_score["hot"] * 5
            + laowanjia_score["repeat"] * 8
            + laowanjia_score["diagonal"] * 6
            + laowanjia_score["consecutive"] * 4,
        )
    }

    return {
        "issue": str(draw.get("issue")) if draw.get("issue") is not None else None,
        "draw_time": draw.get("draw_time") or draw.get("time_text"),
        "numbers": numbers,
        "super_number": draw.get("super_number"),
        "big_small": draw.get("big_small"),
        "odd_even": draw.get("odd_even"),
        "consecutive_numbers": consecutive_numbers,
        "repeated_numbers": repeated_numbers,
        "hot_numbers": hot_numbers,
        "cold_numbers": cold_numbers,
        "missing_numbers": missing_numbers,
        "difference_values": difference_values,
        "diagonal_pattern": diagonal_pattern,
        "laowanjia_score": laowanjia_score,
        "ai_score": ai_score,
    }


def _save_cloud(record: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into analysis_history
                (
                    issue, draw_time, numbers, super_number, big_small, odd_even,
                    consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                    missing_numbers, difference_values, diagonal_pattern,
                    laowanjia_score, ai_score, updated_at
                )
                values (
                    %s, %s, %s::jsonb, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, now()
                )
                on conflict (issue) do update set
                    draw_time = excluded.draw_time,
                    numbers = excluded.numbers,
                    super_number = excluded.super_number,
                    big_small = excluded.big_small,
                    odd_even = excluded.odd_even,
                    consecutive_numbers = excluded.consecutive_numbers,
                    repeated_numbers = excluded.repeated_numbers,
                    hot_numbers = excluded.hot_numbers,
                    cold_numbers = excluded.cold_numbers,
                    missing_numbers = excluded.missing_numbers,
                    difference_values = excluded.difference_values,
                    diagonal_pattern = excluded.diagonal_pattern,
                    laowanjia_score = excluded.laowanjia_score,
                    ai_score = excluded.ai_score,
                    updated_at = now()
                """,
                _record_params(record),
            )
        conn.commit()


def _save_sqlite(record: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into analysis_history
            (
                issue, draw_time, numbers, super_number, big_small, odd_even,
                consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
                missing_numbers, difference_values, diagonal_pattern,
                laowanjia_score, ai_score, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue) do update set
                draw_time = excluded.draw_time,
                numbers = excluded.numbers,
                super_number = excluded.super_number,
                big_small = excluded.big_small,
                odd_even = excluded.odd_even,
                consecutive_numbers = excluded.consecutive_numbers,
                repeated_numbers = excluded.repeated_numbers,
                hot_numbers = excluded.hot_numbers,
                cold_numbers = excluded.cold_numbers,
                missing_numbers = excluded.missing_numbers,
                difference_values = excluded.difference_values,
                diagonal_pattern = excluded.diagonal_pattern,
                laowanjia_score = excluded.laowanjia_score,
                ai_score = excluded.ai_score,
                updated_at = excluded.updated_at
            """,
            _record_params(record, include_updated_at=True),
        )


def _record_params(record: dict, include_updated_at: bool = False) -> tuple:
    params = (
        record["issue"],
        record.get("draw_time"),
        _json_dumps(record.get("numbers", [])),
        record.get("super_number"),
        record.get("big_small"),
        record.get("odd_even"),
        _json_dumps(record.get("consecutive_numbers", [])),
        _json_dumps(record.get("repeated_numbers", [])),
        _json_dumps(record.get("hot_numbers", [])),
        _json_dumps(record.get("cold_numbers", [])),
        _json_dumps(record.get("missing_numbers", [])),
        _json_dumps(record.get("difference_values", {})),
        _json_dumps(record.get("diagonal_pattern", [])),
        _json_dumps(record.get("laowanjia_score", {})),
        _json_dumps(record.get("ai_score", {})),
    )
    if include_updated_at:
        return (*params, _now())
    return params


def save_analysis_history(draw: dict) -> dict:
    if not draw.get("issue"):
        return {"status": "error", "storage": None, "error": "missing issue"}

    record = build_analysis_record(draw)
    if not record.get("numbers"):
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": "missing numbers"}

    try:
        _save_cloud(record)
        return {"status": "ok", "storage": "cloud", "issue": record.get("issue")}
    except Exception as exc:
        logger.exception("cloud analysis_history upsert failed")
        cloud_error = str(exc)

    try:
        _save_sqlite(record)
        return {
            "status": "ok",
            "storage": "sqlite",
            "issue": record.get("issue"),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite analysis_history upsert failed")
        return {"status": "error", "storage": None, "issue": record.get("issue"), "error": str(exc)}


def _row_to_record(row: Any) -> dict:
    return {
        "issue": row[0],
        "draw_time": row[1],
        "numbers": _json_loads(row[2]) or [],
        "super_number": row[3],
        "big_small": row[4],
        "odd_even": row[5],
        "consecutive_numbers": _json_loads(row[6]) or [],
        "repeated_numbers": _json_loads(row[7]) or [],
        "hot_numbers": _json_loads(row[8]) or [],
        "cold_numbers": _json_loads(row[9]) or [],
        "missing_numbers": _json_loads(row[10]) or [],
        "difference_values": _json_loads(row[11]) or {},
        "diagonal_pattern": _json_loads(row[12]) or [],
        "laowanjia_score": _json_loads(row[13]) or {},
        "ai_score": _json_loads(row[14]) or {},
        "created_at": str(row[15]) if row[15] is not None else None,
        "updated_at": str(row[16]) if row[16] is not None else None,
    }


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    try:
        return _query_cloud(sql, params)
    except Exception:
        logger.exception("cloud analysis_history query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite analysis_history query failed")
        return []


def get_latest_analysis_history() -> dict | None:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at
        from analysis_history
        order by issue desc
        limit 1
        """,
    )
    return _row_to_record(rows[0]) if rows else None


def get_analysis_history(limit: int = 100) -> list[dict]:
    rows = _query_with_fallback(
        """
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at
        from analysis_history
        order by issue desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select issue, draw_time, numbers, super_number, big_small, odd_even,
               consecutive_numbers, repeated_numbers, hot_numbers, cold_numbers,
               missing_numbers, difference_values, diagonal_pattern,
               laowanjia_score, ai_score, created_at, updated_at
        from analysis_history
        order by issue desc
        limit ?
        """,
    )
    return [_row_to_record(row) for row in rows]
