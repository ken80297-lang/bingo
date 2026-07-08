from __future__ import annotations

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


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_adaptive_weight_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists adaptive_weights (
                        id bigserial primary key,
                        version integer,
                        strategy text,
                        "window" integer,
                        laowanjia_weight double precision,
                        hot_cold_weight double precision,
                        balance_weight double precision,
                        tail_weight double precision,
                        random_weight double precision,
                        average_hits double precision,
                        hit_rate double precision,
                        source_evaluation_id bigint,
                        is_active boolean default false,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud adaptive_weights table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists adaptive_weights (
                    id integer primary key autoincrement,
                    version integer,
                    strategy text,
                    "window" integer,
                    laowanjia_weight real,
                    hot_cold_weight real,
                    balance_weight real,
                    tail_weight real,
                    random_weight real,
                    average_hits real,
                    hit_rate real,
                    source_evaluation_id integer,
                    is_active integer default 0,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite adaptive_weights table")

    return results


def _weight_params(weights: dict) -> tuple:
    return (
        weights.get("version"),
        weights.get("strategy"),
        weights.get("window"),
        weights.get("laowanjia_weight"),
        weights.get("hot_cold_weight"),
        weights.get("balance_weight"),
        weights.get("tail_weight"),
        weights.get("random_weight"),
        weights.get("average_hits"),
        weights.get("hit_rate"),
        weights.get("source_evaluation_id"),
        bool(weights.get("is_active", True)),
    )


def _save_cloud(weights: dict) -> int:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("update adaptive_weights set is_active = false where is_active = true")
            cur.execute(
                """
                insert into adaptive_weights
                (
                    version, strategy, "window",
                    laowanjia_weight, hot_cold_weight, balance_weight,
                    tail_weight, random_weight, average_hits, hit_rate,
                    source_evaluation_id, is_active, updated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                returning id
                """,
                _weight_params(weights),
            )
            weight_id = cur.fetchone()[0]
        conn.commit()
        return int(weight_id)


def _save_sqlite(weights: dict) -> int:
    with _sqlite_connection() as conn:
        conn.execute("update adaptive_weights set is_active = 0 where is_active = 1")
        cursor = conn.execute(
            """
            insert into adaptive_weights
            (
                version, strategy, "window",
                laowanjia_weight, hot_cold_weight, balance_weight,
                tail_weight, random_weight, average_hits, hit_rate,
                source_evaluation_id, is_active, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*_weight_params(weights), _now()),
        )
        return int(cursor.lastrowid)


def save_adaptive_weights(weights: dict) -> dict:
    try:
        weight_id = _save_cloud(weights)
        return {"status": "ok", "storage": "cloud", "weight_id": weight_id}
    except Exception as exc:
        logger.exception("cloud adaptive weights save failed")
        cloud_error = str(exc)

    try:
        weight_id = _save_sqlite(weights)
        return {
            "status": "ok",
            "storage": "sqlite",
            "weight_id": weight_id,
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite adaptive weights save failed")
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
        logger.exception("cloud adaptive weights query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite adaptive weights query failed")
        return []


def _row_to_weights(row: Any) -> dict:
    return {
        "id": row[0],
        "version": row[1],
        "strategy": row[2],
        "window": row[3],
        "laowanjia_weight": row[4],
        "hot_cold_weight": row[5],
        "balance_weight": row[6],
        "tail_weight": row[7],
        "random_weight": row[8],
        "average_hits": row[9],
        "hit_rate": row[10],
        "source_evaluation_id": row[11],
        "is_active": bool(row[12]),
        "created_at": str(row[13]) if row[13] is not None else None,
        "updated_at": str(row[14]) if row[14] is not None else None,
    }


def get_active_adaptive_weights() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, version, strategy, "window",
               laowanjia_weight, hot_cold_weight, balance_weight,
               tail_weight, random_weight, average_hits, hit_rate,
               source_evaluation_id, is_active, created_at, updated_at
        from adaptive_weights
        where is_active = true
        order by updated_at desc, id desc
        limit 1
        """,
        sqlite_sql="""
        select id, version, strategy, "window",
               laowanjia_weight, hot_cold_weight, balance_weight,
               tail_weight, random_weight, average_hits, hit_rate,
               source_evaluation_id, is_active, created_at, updated_at
        from adaptive_weights
        where is_active = 1
        order by updated_at desc, id desc
        limit 1
        """,
    )
    return _row_to_weights(rows[0]) if rows else None


def get_latest_adaptive_weights() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, version, strategy, "window",
               laowanjia_weight, hot_cold_weight, balance_weight,
               tail_weight, random_weight, average_hits, hit_rate,
               source_evaluation_id, is_active, created_at, updated_at
        from adaptive_weights
        order by updated_at desc, id desc
        limit 1
        """,
    )
    return _row_to_weights(rows[0]) if rows else None


def get_adaptive_weight_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, version, strategy, "window",
               laowanjia_weight, hot_cold_weight, balance_weight,
               tail_weight, random_weight, average_hits, hit_rate,
               source_evaluation_id, is_active, created_at, updated_at
        from adaptive_weights
        order by updated_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, version, strategy, "window",
               laowanjia_weight, hot_cold_weight, balance_weight,
               tail_weight, random_weight, average_hits, hit_rate,
               source_evaluation_id, is_active, created_at, updated_at
        from adaptive_weights
        order by updated_at desc, id desc
        limit ?
        """,
    )
    return [_row_to_weights(row) for row in rows]

