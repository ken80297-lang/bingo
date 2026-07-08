from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
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


def init_recommendation_center_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists recommendation_runs (
                        id bigserial primary key,
                        issue text,
                        target_issue text,
                        best_strategy text,
                        confidence double precision,
                        data_quality_status text,
                        super_recommendation jsonb,
                        explanation text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    alter table recommendation_runs
                    add column if not exists super_recommendation jsonb
                    """
                )
                cur.execute(
                    """
                    create table if not exists recommendation_results (
                        id bigserial primary key,
                        run_id bigint,
                        rank integer,
                        numbers jsonb,
                        confidence double precision,
                        total_score double precision,
                        strategy text,
                        explanation text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud recommendation center tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists recommendation_runs (
                    id integer primary key autoincrement,
                    issue text,
                    target_issue text,
                    best_strategy text,
                    confidence real,
                    data_quality_status text,
                    super_recommendation text,
                    explanation text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            _ensure_sqlite_column(conn, "super_recommendation", "text")
            conn.execute(
                """
                create table if not exists recommendation_results (
                    id integer primary key autoincrement,
                    run_id integer,
                    rank integer,
                    numbers text,
                    confidence real,
                    total_score real,
                    strategy text,
                    explanation text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite recommendation center tables")

    return results


def _ensure_sqlite_column(conn: sqlite3.Connection, column: str, column_type: str) -> None:
    existing = {
        row[1]
        for row in conn.execute("pragma table_info(recommendation_runs)").fetchall()
    }
    if column not in existing:
        conn.execute(f"alter table recommendation_runs add column {column} {column_type}")


def _run_params(run: dict) -> tuple:
    return (
        run.get("issue"),
        run.get("target_issue"),
        run.get("best_strategy"),
        run.get("confidence"),
        run.get("data_quality_status"),
        _json_dumps(run.get("super_recommendation", {})),
        run.get("explanation"),
    )


def _result_params(run_id: int, result: dict) -> tuple:
    return (
        run_id,
        result.get("rank"),
        _json_dumps(result.get("numbers", [])),
        result.get("confidence"),
        result.get("total_score"),
        result.get("strategy"),
        result.get("explanation"),
    )


def _save_cloud(run: dict, results: list[dict]) -> int:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into recommendation_runs
                (
                    issue, target_issue, best_strategy, confidence,
                    data_quality_status, super_recommendation, explanation, updated_at
                )
                values (%s, %s, %s, %s, %s, %s::jsonb, %s, now())
                returning id
                """,
                _run_params(run),
            )
            run_id = int(cur.fetchone()[0])
            for result in results:
                cur.execute(
                    """
                    insert into recommendation_results
                    (
                        run_id, rank, numbers, confidence, total_score,
                        strategy, explanation, updated_at
                    )
                    values (%s, %s, %s::jsonb, %s, %s, %s, %s, now())
                    """,
                    _result_params(run_id, result),
                )
        conn.commit()
        return run_id


def _save_sqlite(run: dict, results: list[dict]) -> int:
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into recommendation_runs
            (
                issue, target_issue, best_strategy, confidence,
                data_quality_status, super_recommendation, explanation, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*_run_params(run), _now()),
        )
        run_id = int(cursor.lastrowid)
        for result in results:
            conn.execute(
                """
                insert into recommendation_results
                (
                    run_id, rank, numbers, confidence, total_score,
                    strategy, explanation, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*_result_params(run_id, result), _now()),
            )
        return run_id


def save_recommendation_run(run: dict, results: list[dict]) -> dict:
    try:
        run_id = _save_cloud(run, results)
        return {"status": "ok", "storage": "cloud", "run_id": run_id}
    except Exception as exc:
        logger.exception("cloud recommendation center save failed")
        cloud_error = str(exc)

    try:
        run_id = _save_sqlite(run, results)
        return {
            "status": "ok",
            "storage": "sqlite",
            "run_id": run_id,
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite recommendation center save failed")
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
        logger.exception("cloud recommendation center query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite recommendation center query failed")
        return []


def _row_to_run(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "target_issue": row[2],
        "best_strategy": row[3],
        "confidence": row[4],
        "data_quality_status": row[5],
        "super_recommendation": _json_loads(row[6]) or {},
        "explanation": row[7],
        "created_at": str(row[8]) if row[8] is not None else None,
        "updated_at": str(row[9]) if row[9] is not None else None,
    }


def _row_to_result(row: Any) -> dict:
    return {
        "id": row[0],
        "run_id": row[1],
        "rank": row[2],
        "numbers": _json_loads(row[3]) or [],
        "confidence": row[4],
        "total_score": row[5],
        "strategy": row[6],
        "explanation": row[7],
        "created_at": str(row[8]) if row[8] is not None else None,
        "updated_at": str(row[9]) if row[9] is not None else None,
    }


def get_recommendation_results(run_id: int) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, run_id, rank, numbers, confidence, total_score,
               strategy, explanation, created_at, updated_at
        from recommendation_results
        where run_id = %s
        order by rank asc
        """,
        (run_id,),
        sqlite_sql="""
        select id, run_id, rank, numbers, confidence, total_score,
               strategy, explanation, created_at, updated_at
        from recommendation_results
        where run_id = ?
        order by rank asc
        """,
    )
    return [_row_to_result(row) for row in rows]


def _attach_results(run: dict | None) -> dict | None:
    if not run:
        return None
    run["results"] = get_recommendation_results(run["id"])
    return run


def get_latest_recommendation_run() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, target_issue, best_strategy, confidence,
               data_quality_status, super_recommendation, explanation, created_at, updated_at
        from recommendation_runs
        order by created_at desc, id desc
        limit 1
        """,
    )
    return _attach_results(_row_to_run(rows[0])) if rows else None


def get_today_recommendation_run() -> dict | None:
    today = date.today().isoformat()
    rows = _query_with_fallback(
        """
        select id, issue, target_issue, best_strategy, confidence,
               data_quality_status, super_recommendation, explanation, created_at, updated_at
        from recommendation_runs
        where created_at::date = %s::date
        order by created_at desc, id desc
        limit 1
        """,
        (today,),
        sqlite_sql="""
        select id, issue, target_issue, best_strategy, confidence,
               data_quality_status, super_recommendation, explanation, created_at, updated_at
        from recommendation_runs
        where date(created_at) = date(?)
        order by created_at desc, id desc
        limit 1
        """,
    )
    return _attach_results(_row_to_run(rows[0])) if rows else None


def get_recommendation_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, issue, target_issue, best_strategy, confidence,
               data_quality_status, super_recommendation, explanation, created_at, updated_at
        from recommendation_runs
        order by created_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, target_issue, best_strategy, confidence,
               data_quality_status, super_recommendation, explanation, created_at, updated_at
        from recommendation_runs
        order by created_at desc, id desc
        limit ?
        """,
    )
    return [_attach_results(_row_to_run(row)) for row in rows]
