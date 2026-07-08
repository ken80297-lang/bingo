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


def init_strategy_ranking_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists strategy_rankings (
                        id bigserial primary key,
                        strategy text,
                        "window" integer,
                        evaluated_issues integer,
                        average_hits double precision,
                        best_hits integer,
                        hit_rate double precision,
                        hit_distribution jsonb,
                        rank_score double precision,
                        is_current boolean default false,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud strategy_rankings table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists strategy_rankings (
                    id integer primary key autoincrement,
                    strategy text,
                    "window" integer,
                    evaluated_issues integer,
                    average_hits real,
                    best_hits integer,
                    hit_rate real,
                    hit_distribution text,
                    rank_score real,
                    is_current integer default 0,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite strategy_rankings table")

    return results


def _ranking_params(row: dict) -> tuple:
    return (
        row.get("strategy"),
        row.get("window"),
        row.get("evaluated_issues"),
        row.get("average_hits"),
        row.get("best_hits"),
        row.get("hit_rate"),
        _json_dumps(row.get("hit_distribution", {})),
        row.get("rank_score"),
        bool(row.get("is_current", True)),
    )


def _save_cloud(rankings: list[dict]) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("update strategy_rankings set is_current = false where is_current = true")
            for row in rankings:
                cur.execute(
                    """
                    insert into strategy_rankings
                    (
                        strategy, "window", evaluated_issues, average_hits,
                        best_hits, hit_rate, hit_distribution, rank_score,
                        is_current, updated_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, now())
                    """,
                    _ranking_params(row),
                )
        conn.commit()


def _save_sqlite(rankings: list[dict]) -> None:
    with _sqlite_connection() as conn:
        conn.execute("update strategy_rankings set is_current = 0 where is_current = 1")
        for row in rankings:
            conn.execute(
                """
                insert into strategy_rankings
                (
                    strategy, "window", evaluated_issues, average_hits,
                    best_hits, hit_rate, hit_distribution, rank_score,
                    is_current, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*_ranking_params(row), _now()),
            )


def save_strategy_rankings(rankings: list[dict]) -> dict:
    try:
        _save_cloud(rankings)
        return {"status": "ok", "storage": "cloud", "count": len(rankings)}
    except Exception as exc:
        logger.exception("cloud strategy rankings save failed")
        cloud_error = str(exc)

    try:
        _save_sqlite(rankings)
        return {
            "status": "ok",
            "storage": "sqlite",
            "count": len(rankings),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite strategy rankings save failed")
        return {"status": "error", "storage": None, "count": 0, "error": str(exc)}


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
        logger.exception("cloud strategy rankings query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite strategy rankings query failed")
        return []


def _row_to_ranking(row: Any) -> dict:
    return {
        "id": row[0],
        "strategy": row[1],
        "window": row[2],
        "evaluated_issues": row[3],
        "average_hits": row[4],
        "best_hits": row[5],
        "hit_rate": row[6],
        "hit_distribution": _json_loads(row[7]) or {},
        "rank_score": row[8],
        "is_current": bool(row[9]),
        "created_at": str(row[10]) if row[10] is not None else None,
        "updated_at": str(row[11]) if row[11] is not None else None,
    }


def get_latest_strategy_rankings() -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, strategy, "window", evaluated_issues, average_hits,
               best_hits, hit_rate, hit_distribution, rank_score,
               is_current, created_at, updated_at
        from strategy_rankings
        where is_current = true
        order by rank_score desc, id asc
        """,
        sqlite_sql="""
        select id, strategy, "window", evaluated_issues, average_hits,
               best_hits, hit_rate, hit_distribution, rank_score,
               is_current, created_at, updated_at
        from strategy_rankings
        where is_current = 1
        order by rank_score desc, id asc
        """,
    )
    return [_row_to_ranking(row) for row in rows]


def get_strategy_ranking_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, strategy, "window", evaluated_issues, average_hits,
               best_hits, hit_rate, hit_distribution, rank_score,
               is_current, created_at, updated_at
        from strategy_rankings
        order by updated_at desc, rank_score desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, strategy, "window", evaluated_issues, average_hits,
               best_hits, hit_rate, hit_distribution, rank_score,
               is_current, created_at, updated_at
        from strategy_rankings
        order by updated_at desc, rank_score desc, id desc
        limit ?
        """,
    )
    return [_row_to_ranking(row) for row in rows]

