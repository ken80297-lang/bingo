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


def init_simulation_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists simulation_runs (
                        id bigserial primary key,
                        "window" integer,
                        groups integer,
                        numbers_per_group integer,
                        source_issue text,
                        generated_at timestamptz default now(),
                        sample_size integer,
                        model_version text,
                        features jsonb,
                        status text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    alter table simulation_runs
                    add column if not exists source_issue text
                    """
                )
                cur.execute(
                    """
                    alter table simulation_runs
                    add column if not exists generated_at timestamptz default now()
                    """
                )
                cur.execute(
                    """
                    alter table simulation_runs
                    add column if not exists sample_size integer
                    """
                )
                cur.execute(
                    """
                    alter table simulation_runs
                    add column if not exists model_version text
                    """
                )
                cur.execute(
                    """
                    create table if not exists simulation_results (
                        id bigserial primary key,
                        run_id bigint,
                        rank integer,
                        numbers jsonb,
                        scores jsonb,
                        total_score double precision,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud simulation tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists simulation_runs (
                    id integer primary key autoincrement,
                    "window" integer,
                    groups integer,
                    numbers_per_group integer,
                    source_issue text,
                    generated_at text,
                    sample_size integer,
                    model_version text,
                    features text,
                    status text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            _ensure_sqlite_column(conn, "source_issue", "text")
            _ensure_sqlite_column(conn, "generated_at", "text")
            _ensure_sqlite_column(conn, "sample_size", "integer")
            _ensure_sqlite_column(conn, "model_version", "text")
            conn.execute(
                """
                create table if not exists simulation_results (
                    id integer primary key autoincrement,
                    run_id integer,
                    rank integer,
                    numbers text,
                    scores text,
                    total_score real,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite simulation tables")

    return results


def _ensure_sqlite_column(conn: sqlite3.Connection, column: str, column_type: str) -> None:
    existing = {
        row[1]
        for row in conn.execute("pragma table_info(simulation_runs)").fetchall()
    }
    if column not in existing:
        conn.execute(f"alter table simulation_runs add column {column} {column_type}")


def _insert_run_cloud(payload: dict) -> int:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into simulation_runs
                (
                    "window", groups, numbers_per_group, source_issue,
                    generated_at, sample_size, model_version, features,
                    status, updated_at
                )
                values (%s, %s, %s, %s, now(), %s, %s, %s::jsonb, %s, now())
                returning id
                """,
                (
                    payload.get("window"),
                    payload.get("groups"),
                    payload.get("numbers_per_group"),
                    payload.get("source_issue"),
                    payload.get("sample_size"),
                    payload.get("model_version"),
                    _json_dumps(payload.get("features", {})),
                    payload.get("status", "ok"),
                ),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        return int(run_id)


def _insert_run_sqlite(payload: dict) -> int:
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into simulation_runs
            (
                "window", groups, numbers_per_group, source_issue,
                generated_at, sample_size, model_version, features,
                status, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("window"),
                payload.get("groups"),
                payload.get("numbers_per_group"),
                payload.get("source_issue"),
                _now(),
                payload.get("sample_size"),
                payload.get("model_version"),
                _json_dumps(payload.get("features", {})),
                payload.get("status", "ok"),
                _now(),
            ),
        )
        return int(cursor.lastrowid)


def _insert_results_cloud(run_id: int, results: list[dict]) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            for item in results:
                cur.execute(
                    """
                    insert into simulation_results
                    (run_id, rank, numbers, scores, total_score, updated_at)
                    values (%s, %s, %s::jsonb, %s::jsonb, %s, now())
                    """,
                    (
                        run_id,
                        item.get("rank"),
                        _json_dumps(item.get("numbers", [])),
                        _json_dumps(item.get("scores", {})),
                        item.get("total_score", 0),
                    ),
                )
        conn.commit()


def _insert_results_sqlite(run_id: int, results: list[dict]) -> None:
    with _sqlite_connection() as conn:
        for item in results:
            conn.execute(
                """
                insert into simulation_results
                (run_id, rank, numbers, scores, total_score, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.get("rank"),
                    _json_dumps(item.get("numbers", [])),
                    _json_dumps(item.get("scores", {})),
                    item.get("total_score", 0),
                    _now(),
                ),
            )


def save_simulation_run(payload: dict, results: list[dict]) -> dict:
    try:
        run_id = _insert_run_cloud(payload)
        _insert_results_cloud(run_id, results)
        return {"status": "ok", "storage": "cloud", "run_id": run_id}
    except Exception as exc:
        logger.exception("cloud simulation save failed")
        cloud_error = str(exc)

    try:
        run_id = _insert_run_sqlite(payload)
        _insert_results_sqlite(run_id, results)
        return {
            "status": "ok",
            "storage": "sqlite",
            "run_id": run_id,
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite simulation save failed")
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
        logger.exception("cloud simulation query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite simulation query failed")
        return []


def _row_to_run(row: Any) -> dict:
    return {
        "id": row[0],
        "window": row[1],
        "groups": row[2],
        "numbers_per_group": row[3],
        "source_issue": row[4],
        "generated_at": str(row[5]) if row[5] is not None else None,
        "sample_size": row[6],
        "model_version": row[7],
        "features": _json_loads(row[8]) or {},
        "status": row[9],
        "created_at": str(row[10]) if row[10] is not None else None,
        "updated_at": str(row[11]) if row[11] is not None else None,
    }


def _row_to_result(row: Any) -> dict:
    return {
        "id": row[0],
        "run_id": row[1],
        "rank": row[2],
        "numbers": _json_loads(row[3]) or [],
        "scores": _json_loads(row[4]) or {},
        "total_score": row[5],
        "created_at": str(row[6]) if row[6] is not None else None,
        "updated_at": str(row[7]) if row[7] is not None else None,
    }


def get_simulation_results(run_id: int) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, run_id, rank, numbers, scores, total_score, created_at, updated_at
        from simulation_results
        where run_id = %s
        order by rank asc
        """,
        (run_id,),
        sqlite_sql="""
        select id, run_id, rank, numbers, scores, total_score, created_at, updated_at
        from simulation_results
        where run_id = ?
        order by rank asc
        """,
    )
    return [_row_to_result(row) for row in rows]


def get_latest_simulation_run() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, "window", groups, numbers_per_group, source_issue,
               generated_at, sample_size, model_version, features,
               status, created_at, updated_at
        from simulation_runs
        order by generated_at desc, created_at desc, id desc
        limit 1
        """,
    )
    if not rows:
        return None
    run = _row_to_run(rows[0])
    run["results"] = get_simulation_results(run["id"])
    return run


def get_simulation_run_by_issue(issue: str) -> dict | None:
    rows = _query_with_fallback(
        """
        select id, "window", groups, numbers_per_group, source_issue,
               generated_at, sample_size, model_version, features,
               status, created_at, updated_at
        from simulation_runs
        where source_issue = %s
        order by generated_at desc, created_at desc, id desc
        limit 1
        """,
        (str(issue),),
        sqlite_sql="""
        select id, "window", groups, numbers_per_group, source_issue,
               generated_at, sample_size, model_version, features,
               status, created_at, updated_at
        from simulation_runs
        where source_issue = ?
        order by generated_at desc, created_at desc, id desc
        limit 1
        """,
    )
    if not rows:
        return None
    run = _row_to_run(rows[0])
    run["results"] = get_simulation_results(run["id"])
    return run


def get_simulation_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, "window", groups, numbers_per_group, source_issue,
               generated_at, sample_size, model_version, features,
               status, created_at, updated_at
        from simulation_runs
        order by generated_at desc, created_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, "window", groups, numbers_per_group, source_issue,
               generated_at, sample_size, model_version, features,
               status, created_at, updated_at
        from simulation_runs
        order by generated_at desc, created_at desc, id desc
        limit ?
        """,
    )
    runs = [_row_to_run(row) for row in rows]
    for run in runs:
        run["results"] = get_simulation_results(run["id"])
    return runs
