from __future__ import annotations

import json
import logging
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


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_laowanjia_feature_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists laowanjia_features (
                        id bigserial primary key,
                        issue text unique,
                        numbers jsonb,
                        super_number integer,
                        consecutive_score double precision,
                        twin_score double precision,
                        diagonal_score double precision,
                        gap_score double precision,
                        missing_score double precision,
                        big_small_score double precision,
                        odd_even_score double precision,
                        repeat_score double precision,
                        total_laowanjia_feature_score double precision,
                        feature_json jsonb,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud laowanjia_features table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists laowanjia_features (
                    id integer primary key autoincrement,
                    issue text unique,
                    numbers text,
                    super_number integer,
                    consecutive_score real,
                    twin_score real,
                    diagonal_score real,
                    gap_score real,
                    missing_score real,
                    big_small_score real,
                    odd_even_score real,
                    repeat_score real,
                    total_laowanjia_feature_score real,
                    feature_json text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite laowanjia_features table")

    return results


def _feature_params(record: dict, include_updated_at: bool = False) -> tuple:
    params = (
        record.get("issue"),
        _json_dumps(record.get("numbers", [])),
        record.get("super_number"),
        record.get("consecutive_score", 0),
        record.get("twin_score", 0),
        record.get("diagonal_score", 0),
        record.get("gap_score", 0),
        record.get("missing_score", 0),
        record.get("big_small_score", 0),
        record.get("odd_even_score", 0),
        record.get("repeat_score", 0),
        record.get("total_laowanjia_feature_score", 0),
        _json_dumps(record.get("feature_json", {})),
    )
    if include_updated_at:
        return (*params, _now())
    return params


def _save_cloud(record: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into laowanjia_features
                (
                    issue, numbers, super_number, consecutive_score,
                    twin_score, diagonal_score, gap_score, missing_score,
                    big_small_score, odd_even_score, repeat_score,
                    total_laowanjia_feature_score, feature_json, updated_at
                )
                values (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                on conflict (issue) do update set
                    numbers = excluded.numbers,
                    super_number = excluded.super_number,
                    consecutive_score = excluded.consecutive_score,
                    twin_score = excluded.twin_score,
                    diagonal_score = excluded.diagonal_score,
                    gap_score = excluded.gap_score,
                    missing_score = excluded.missing_score,
                    big_small_score = excluded.big_small_score,
                    odd_even_score = excluded.odd_even_score,
                    repeat_score = excluded.repeat_score,
                    total_laowanjia_feature_score = excluded.total_laowanjia_feature_score,
                    feature_json = excluded.feature_json,
                    updated_at = now()
                """,
                _feature_params(record),
            )
        conn.commit()


def _save_sqlite(record: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into laowanjia_features
            (
                issue, numbers, super_number, consecutive_score,
                twin_score, diagonal_score, gap_score, missing_score,
                big_small_score, odd_even_score, repeat_score,
                total_laowanjia_feature_score, feature_json, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue) do update set
                numbers = excluded.numbers,
                super_number = excluded.super_number,
                consecutive_score = excluded.consecutive_score,
                twin_score = excluded.twin_score,
                diagonal_score = excluded.diagonal_score,
                gap_score = excluded.gap_score,
                missing_score = excluded.missing_score,
                big_small_score = excluded.big_small_score,
                odd_even_score = excluded.odd_even_score,
                repeat_score = excluded.repeat_score,
                total_laowanjia_feature_score = excluded.total_laowanjia_feature_score,
                feature_json = excluded.feature_json,
                updated_at = excluded.updated_at
            """,
            _feature_params(record, include_updated_at=True),
        )


def save_laowanjia_feature(record: dict) -> dict:
    if not record.get("issue"):
        return {"status": "error", "storage": None, "error": "missing issue"}

    try:
        _save_cloud(record)
        return {"status": "ok", "storage": "cloud", "issue": record.get("issue")}
    except Exception as exc:
        logger.exception("cloud laowanjia feature save failed")
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
        logger.exception("sqlite laowanjia feature save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


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
        logger.exception("cloud laowanjia feature query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite laowanjia feature query failed")
        return []


def _row_to_feature(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "numbers": _json_loads(row[2]) or [],
        "super_number": row[3],
        "consecutive_score": row[4],
        "twin_score": row[5],
        "diagonal_score": row[6],
        "gap_score": row[7],
        "missing_score": row[8],
        "big_small_score": row[9],
        "odd_even_score": row[10],
        "repeat_score": row[11],
        "total_laowanjia_feature_score": row[12],
        "feature_json": _json_loads(row[13]) or {},
        "created_at": str(row[14]) if row[14] is not None else None,
        "updated_at": str(row[15]) if row[15] is not None else None,
    }


def get_latest_laowanjia_feature() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, numbers, super_number, consecutive_score,
               twin_score, diagonal_score, gap_score, missing_score,
               big_small_score, odd_even_score, repeat_score,
               total_laowanjia_feature_score, feature_json, created_at, updated_at
        from laowanjia_features
        order by issue desc, updated_at desc, id desc
        limit 1
        """,
    )
    return _row_to_feature(rows[0]) if rows else None


def get_laowanjia_feature_by_issue(issue: str) -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, numbers, super_number, consecutive_score,
               twin_score, diagonal_score, gap_score, missing_score,
               big_small_score, odd_even_score, repeat_score,
               total_laowanjia_feature_score, feature_json, created_at, updated_at
        from laowanjia_features
        where issue = %s
        order by updated_at desc, id desc
        limit 1
        """,
        (str(issue),),
        sqlite_sql="""
        select id, issue, numbers, super_number, consecutive_score,
               twin_score, diagonal_score, gap_score, missing_score,
               big_small_score, odd_even_score, repeat_score,
               total_laowanjia_feature_score, feature_json, created_at, updated_at
        from laowanjia_features
        where issue = ?
        order by updated_at desc, id desc
        limit 1
        """,
    )
    return _row_to_feature(rows[0]) if rows else None


def get_laowanjia_feature_history(limit: int = 50) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, issue, numbers, super_number, consecutive_score,
               twin_score, diagonal_score, gap_score, missing_score,
               big_small_score, odd_even_score, repeat_score,
               total_laowanjia_feature_score, feature_json, created_at, updated_at
        from laowanjia_features
        order by issue desc, updated_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, numbers, super_number, consecutive_score,
               twin_score, diagonal_score, gap_score, missing_score,
               big_small_score, odd_even_score, repeat_score,
               total_laowanjia_feature_score, feature_json, created_at, updated_at
        from laowanjia_features
        order by issue desc, updated_at desc, id desc
        limit ?
        """,
    )
    return [_row_to_feature(row) for row in rows]
