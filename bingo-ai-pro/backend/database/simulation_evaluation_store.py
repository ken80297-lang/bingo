from __future__ import annotations

import logging
import sqlite3
import json
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


def init_simulation_evaluation_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists simulation_evaluations (
                        id bigserial primary key,
                        run_id bigint,
                        strategy text,
                        "window" integer,
                        evaluated_issues integer,
                        hit_0 integer,
                        hit_1 integer,
                        hit_2 integer,
                        hit_3 integer,
                        hit_4 integer,
                        hit_5_plus integer,
                        average_hits double precision,
                        best_hits integer,
                        hit_rate double precision,
                        hit_distribution jsonb,
                        leakage_safe boolean default false,
                        created_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    alter table simulation_evaluations
                    add column if not exists hit_distribution jsonb
                    """
                )
                cur.execute(
                    """
                    alter table simulation_evaluations
                    add column if not exists leakage_safe boolean default false
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud simulation_evaluations table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists simulation_evaluations (
                    id integer primary key autoincrement,
                    run_id integer,
                    strategy text,
                    "window" integer,
                    evaluated_issues integer,
                    hit_0 integer,
                    hit_1 integer,
                    hit_2 integer,
                    hit_3 integer,
                    hit_4 integer,
                    hit_5_plus integer,
                    average_hits real,
                    best_hits integer,
                    hit_rate real,
                    hit_distribution text,
                    leakage_safe integer default 0,
                    created_at text default current_timestamp
                )
                """
            )
            _ensure_sqlite_column(conn, "hit_distribution", "text")
            _ensure_sqlite_column(conn, "leakage_safe", "integer default 0")
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite simulation_evaluations table")

    return results


def _ensure_sqlite_column(conn: sqlite3.Connection, column: str, column_type: str) -> None:
    existing = {
        row[1]
        for row in conn.execute("pragma table_info(simulation_evaluations)").fetchall()
    }
    if column not in existing:
        conn.execute(f"alter table simulation_evaluations add column {column} {column_type}")


def _save_cloud(evaluation: dict) -> int:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into simulation_evaluations
                (
                    run_id, strategy, "window", evaluated_issues,
                    hit_0, hit_1, hit_2, hit_3, hit_4, hit_5_plus,
                    average_hits, best_hits, hit_rate, hit_distribution, leakage_safe
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                returning id
                """,
                _evaluation_params(evaluation),
            )
            evaluation_id = cur.fetchone()[0]
        conn.commit()
        return int(evaluation_id)


def _save_sqlite(evaluation: dict) -> int:
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into simulation_evaluations
            (
                run_id, strategy, "window", evaluated_issues,
                hit_0, hit_1, hit_2, hit_3, hit_4, hit_5_plus,
                average_hits, best_hits, hit_rate, hit_distribution, leakage_safe
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _evaluation_params(evaluation),
        )
        return int(cursor.lastrowid)


def _evaluation_params(evaluation: dict) -> tuple:
    return (
        evaluation.get("run_id"),
        evaluation.get("strategy"),
        evaluation.get("window"),
        evaluation.get("evaluated_issues", 0),
        evaluation.get("hit_0", 0),
        evaluation.get("hit_1", 0),
        evaluation.get("hit_2", 0),
        evaluation.get("hit_3", 0),
        evaluation.get("hit_4", 0),
        evaluation.get("hit_5_plus", 0),
        evaluation.get("average_hits", 0),
        evaluation.get("best_hits", 0),
        evaluation.get("hit_rate", 0),
        _json_dumps(evaluation.get("hit_distribution", {})),
        bool(evaluation.get("leakage_safe", False)),
    )


def save_simulation_evaluation(evaluation: dict) -> dict:
    try:
        evaluation_id = _save_cloud(evaluation)
        return {"status": "ok", "storage": "cloud", "evaluation_id": evaluation_id}
    except Exception as exc:
        logger.exception("cloud simulation evaluation save failed")
        cloud_error = str(exc)

    try:
        evaluation_id = _save_sqlite(evaluation)
        return {
            "status": "ok",
            "storage": "sqlite",
            "evaluation_id": evaluation_id,
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite simulation evaluation save failed")
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
        logger.exception("cloud simulation evaluation query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite simulation evaluation query failed")
        return []


def _row_to_evaluation(row: Any) -> dict:
    return {
        "id": row[0],
        "run_id": row[1],
        "strategy": row[2],
        "window": row[3],
        "evaluated_issues": row[4],
        "hit_0": row[5],
        "hit_1": row[6],
        "hit_2": row[7],
        "hit_3": row[8],
        "hit_4": row[9],
        "hit_5_plus": row[10],
        "average_hits": row[11],
        "best_hits": row[12],
        "hit_rate": row[13],
        "hit_distribution": _json_loads(row[14]) or {},
        "leakage_safe": bool(row[15]),
        "created_at": str(row[16]) if row[16] is not None else None,
    }


def get_latest_simulation_evaluation() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, run_id, strategy, "window", evaluated_issues,
               hit_0, hit_1, hit_2, hit_3, hit_4, hit_5_plus,
               average_hits, best_hits, hit_rate,
               hit_distribution, leakage_safe, created_at
        from simulation_evaluations
        order by created_at desc, id desc
        limit 1
        """,
    )
    return _row_to_evaluation(rows[0]) if rows else None


def get_simulation_evaluation_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, run_id, strategy, "window", evaluated_issues,
               hit_0, hit_1, hit_2, hit_3, hit_4, hit_5_plus,
               average_hits, best_hits, hit_rate,
               hit_distribution, leakage_safe, created_at
        from simulation_evaluations
        order by created_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, run_id, strategy, "window", evaluated_issues,
               hit_0, hit_1, hit_2, hit_3, hit_4, hit_5_plus,
               average_hits, best_hits, hit_rate,
               hit_distribution, leakage_safe, created_at
        from simulation_evaluations
        order by created_at desc, id desc
        limit ?
        """,
    )
    return [_row_to_evaluation(row) for row in rows]
