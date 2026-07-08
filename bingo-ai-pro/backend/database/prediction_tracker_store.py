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


def init_prediction_tracker_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists prediction_runs (
                        id bigserial primary key,
                        recommendation_run_id bigint unique,
                        simulation_run_id bigint,
                        issue text,
                        target_issue text,
                        actual_issue text,
                        status text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists prediction_results (
                        id bigserial primary key,
                        prediction_run_id bigint,
                        rank integer,
                        recommended_numbers jsonb,
                        actual_numbers jsonb,
                        hit_count integer,
                        hit_numbers jsonb,
                        miss_numbers jsonb,
                        super_prediction integer,
                        actual_super integer,
                        super_hit boolean,
                        confidence double precision,
                        strategy text,
                        created_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud prediction tracker tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists prediction_runs (
                    id integer primary key autoincrement,
                    recommendation_run_id integer unique,
                    simulation_run_id integer,
                    issue text,
                    target_issue text,
                    actual_issue text,
                    status text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists prediction_results (
                    id integer primary key autoincrement,
                    prediction_run_id integer,
                    rank integer,
                    recommended_numbers text,
                    actual_numbers text,
                    hit_count integer,
                    hit_numbers text,
                    miss_numbers text,
                    super_prediction integer,
                    actual_super integer,
                    super_hit integer,
                    confidence real,
                    strategy text,
                    created_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite prediction tracker tables")

    return results


def _run_params(run: dict, include_updated_at: bool = False) -> tuple:
    params = (
        run.get("recommendation_run_id"),
        run.get("simulation_run_id"),
        run.get("issue"),
        run.get("target_issue"),
        run.get("actual_issue"),
        run.get("status", "pending"),
    )
    if include_updated_at:
        return (*params, _now())
    return params


def _result_params(prediction_run_id: int, result: dict) -> tuple:
    return (
        prediction_run_id,
        result.get("rank"),
        _json_dumps(result.get("recommended_numbers", [])),
        _json_dumps(result.get("actual_numbers", [])),
        result.get("hit_count", 0),
        _json_dumps(result.get("hit_numbers", [])),
        _json_dumps(result.get("miss_numbers", [])),
        result.get("super_prediction"),
        result.get("actual_super"),
        bool(result.get("super_hit")),
        result.get("confidence"),
        result.get("strategy"),
    )


def save_prediction_run(run: dict) -> dict:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into prediction_runs
                    (
                        recommendation_run_id, simulation_run_id, issue,
                        target_issue, actual_issue, status, updated_at
                    )
                    values (%s, %s, %s, %s, %s, %s, now())
                    on conflict (recommendation_run_id) do update set
                        simulation_run_id = excluded.simulation_run_id,
                        issue = excluded.issue,
                        target_issue = excluded.target_issue,
                        actual_issue = coalesce(prediction_runs.actual_issue, excluded.actual_issue),
                        status = prediction_runs.status,
                        updated_at = now()
                    returning id
                    """,
                    _run_params(run),
                )
                run_id = int(cur.fetchone()[0])
            conn.commit()
        return {"status": "ok", "storage": "cloud", "run_id": run_id}
    except Exception as exc:
        logger.exception("cloud prediction run save failed")
        cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into prediction_runs
                (
                    recommendation_run_id, simulation_run_id, issue,
                    target_issue, actual_issue, status, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(recommendation_run_id) do update set
                    simulation_run_id = excluded.simulation_run_id,
                    issue = excluded.issue,
                    target_issue = excluded.target_issue,
                    actual_issue = coalesce(prediction_runs.actual_issue, excluded.actual_issue),
                    status = prediction_runs.status,
                    updated_at = excluded.updated_at
                """,
                _run_params(run, include_updated_at=True),
            )
            if cursor.lastrowid:
                run_id = int(cursor.lastrowid)
            else:
                row = conn.execute(
                    "select id from prediction_runs where recommendation_run_id = ?",
                    (run.get("recommendation_run_id"),),
                ).fetchone()
                run_id = int(row[0]) if row else 0
        return {"status": "ok", "storage": "sqlite", "run_id": run_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite prediction run save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def save_prediction_results(prediction_run_id: int, actual_issue: str, results: list[dict]) -> dict:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("delete from prediction_results where prediction_run_id = %s", (prediction_run_id,))
                for result in results:
                    cur.execute(
                        """
                        insert into prediction_results
                        (
                            prediction_run_id, rank, recommended_numbers, actual_numbers,
                            hit_count, hit_numbers, miss_numbers, super_prediction,
                            actual_super, super_hit, confidence, strategy
                        )
                        values (%s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                        """,
                        _result_params(prediction_run_id, result),
                    )
                cur.execute(
                    """
                    update prediction_runs
                    set actual_issue = %s, status = 'evaluated', updated_at = now()
                    where id = %s
                    """,
                    (actual_issue, prediction_run_id),
                )
            conn.commit()
        return {"status": "ok", "storage": "cloud", "run_id": prediction_run_id}
    except Exception as exc:
        logger.exception("cloud prediction result save failed")
        cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            conn.execute("delete from prediction_results where prediction_run_id = ?", (prediction_run_id,))
            for result in results:
                params = list(_result_params(prediction_run_id, result))
                params[9] = 1 if result.get("super_hit") else 0
                conn.execute(
                    """
                    insert into prediction_results
                    (
                        prediction_run_id, rank, recommended_numbers, actual_numbers,
                        hit_count, hit_numbers, miss_numbers, super_prediction,
                        actual_super, super_hit, confidence, strategy
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(params),
                )
            conn.execute(
                """
                update prediction_runs
                set actual_issue = ?, status = 'evaluated', updated_at = ?
                where id = ?
                """,
                (actual_issue, _now(), prediction_run_id),
            )
        return {"status": "ok", "storage": "sqlite", "run_id": prediction_run_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite prediction result save failed")
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
        logger.exception("cloud prediction query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite prediction query failed")
        return []


def _row_to_run(row: Any) -> dict:
    return {
        "id": row[0],
        "recommendation_run_id": row[1],
        "simulation_run_id": row[2],
        "issue": row[3],
        "target_issue": row[4],
        "actual_issue": row[5],
        "status": row[6],
        "created_at": str(row[7]) if row[7] is not None else None,
        "updated_at": str(row[8]) if row[8] is not None else None,
    }


def _row_to_result(row: Any) -> dict:
    return {
        "id": row[0],
        "prediction_run_id": row[1],
        "rank": row[2],
        "recommended_numbers": _json_loads(row[3]) or [],
        "actual_numbers": _json_loads(row[4]) or [],
        "hit_count": row[5],
        "hit_numbers": _json_loads(row[6]) or [],
        "miss_numbers": _json_loads(row[7]) or [],
        "super_prediction": row[8],
        "actual_super": row[9],
        "super_hit": bool(row[10]),
        "confidence": row[11],
        "strategy": row[12],
        "created_at": str(row[13]) if row[13] is not None else None,
    }


def get_prediction_results(prediction_run_id: int) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, prediction_run_id, rank, recommended_numbers, actual_numbers,
               hit_count, hit_numbers, miss_numbers, super_prediction,
               actual_super, super_hit, confidence, strategy, created_at
        from prediction_results
        where prediction_run_id = %s
        order by rank asc
        """,
        (prediction_run_id,),
        sqlite_sql="""
        select id, prediction_run_id, rank, recommended_numbers, actual_numbers,
               hit_count, hit_numbers, miss_numbers, super_prediction,
               actual_super, super_hit, confidence, strategy, created_at
        from prediction_results
        where prediction_run_id = ?
        order by rank asc
        """,
    )
    return [_row_to_result(row) for row in rows]


def _attach_results(run: dict | None) -> dict | None:
    if not run:
        return None
    run["results"] = get_prediction_results(run["id"])
    return run


def get_pending_prediction_runs() -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, recommendation_run_id, simulation_run_id, issue, target_issue,
               actual_issue, status, created_at, updated_at
        from prediction_runs
        where status = 'pending'
        order by created_at asc, id asc
        """,
    )
    return [_row_to_run(row) for row in rows]


def get_latest_prediction_run() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, recommendation_run_id, simulation_run_id, issue, target_issue,
               actual_issue, status, created_at, updated_at
        from prediction_runs
        order by updated_at desc, id desc
        limit 1
        """,
    )
    return _attach_results(_row_to_run(rows[0])) if rows else None


def get_prediction_history(limit: int = 30) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, recommendation_run_id, simulation_run_id, issue, target_issue,
               actual_issue, status, created_at, updated_at
        from prediction_runs
        order by updated_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, recommendation_run_id, simulation_run_id, issue, target_issue,
               actual_issue, status, created_at, updated_at
        from prediction_runs
        order by updated_at desc, id desc
        limit ?
        """,
    )
    return [_attach_results(_row_to_run(row)) for row in rows]


def get_prediction_statistics() -> dict:
    rows = _query_with_fallback(
        """
        select hit_count, super_hit, confidence
        from prediction_results
        order by created_at desc, id desc
        """,
    )
    if not rows:
        return {
            "average_hits": 0,
            "best_hits": 0,
            "worst_hits": 0,
            "super_hit_rate": 0,
            "average_confidence": 0,
            "evaluated": 0,
        }

    hits = [int(row[0] or 0) for row in rows]
    super_hits = [bool(row[1]) for row in rows]
    confidences = [float(row[2] or 0) for row in rows]
    evaluated = len(rows)
    return {
        "average_hits": round(sum(hits) / evaluated, 2),
        "best_hits": max(hits),
        "worst_hits": min(hits),
        "super_hit_rate": round(sum(1 for value in super_hits if value) / evaluated, 4),
        "average_confidence": round(sum(confidences) / evaluated, 2),
        "evaluated": evaluated,
    }
