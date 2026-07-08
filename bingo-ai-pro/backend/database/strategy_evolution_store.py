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


def init_strategy_evolution_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists strategy_versions (
                        id bigserial primary key,
                        version integer unique,
                        created_at timestamptz default now(),
                        "window" integer,
                        evaluated_predictions integer,
                        average_hits double precision,
                        super_hit_rate double precision,
                        rank_score double precision,
                        is_active boolean default false,
                        is_candidate boolean default false,
                        description text
                    )
                    """
                )
                cur.execute("alter table strategy_versions add column if not exists is_candidate boolean default false")
                cur.execute(
                    """
                    create table if not exists strategy_weights (
                        id bigserial primary key,
                        version_id bigint,
                        hot_weight double precision,
                        cold_weight double precision,
                        missing_weight double precision,
                        gap_weight double precision,
                        tail_weight double precision,
                        balance_weight double precision,
                        laowanjia_weight double precision,
                        exploration_rate double precision,
                        created_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud strategy evolution tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists strategy_versions (
                    id integer primary key autoincrement,
                    version integer unique,
                    created_at text default current_timestamp,
                    "window" integer,
                    evaluated_predictions integer,
                    average_hits real,
                    super_hit_rate real,
                    rank_score real,
                    is_active integer default 0,
                    is_candidate integer default 0,
                    description text
                )
                """
            )
            _ensure_sqlite_column(conn, "strategy_versions", "is_candidate", "integer default 0")
            conn.execute(
                """
                create table if not exists strategy_weights (
                    id integer primary key autoincrement,
                    version_id integer,
                    hot_weight real,
                    cold_weight real,
                    missing_weight real,
                    gap_weight real,
                    tail_weight real,
                    balance_weight real,
                    laowanjia_weight real,
                    exploration_rate real,
                    created_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite strategy evolution tables")

    return results


def _ensure_sqlite_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    existing = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"alter table {table} add column {column} {column_type}")


def _version_params(version: dict) -> tuple:
    return (
        version.get("version"),
        version.get("window"),
        version.get("evaluated_predictions"),
        version.get("average_hits"),
        version.get("super_hit_rate"),
        version.get("rank_score"),
        bool(version.get("is_active", False)),
        bool(version.get("is_candidate", False)),
        version.get("description"),
    )


def _weight_params(version_id: int, weights: dict) -> tuple:
    return (
        version_id,
        weights.get("hot"),
        weights.get("cold"),
        weights.get("missing"),
        weights.get("gap"),
        weights.get("tail"),
        weights.get("balance"),
        weights.get("laowanjia"),
        weights.get("exploration"),
    )


def save_strategy_version(version: dict, weights: dict) -> dict:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into strategy_versions
                    (
                        version, "window", evaluated_predictions, average_hits,
                        super_hit_rate, rank_score, is_active, is_candidate,
                        description
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    _version_params(version),
                )
                version_id = int(cur.fetchone()[0])
                cur.execute(
                    """
                    insert into strategy_weights
                    (
                        version_id, hot_weight, cold_weight, missing_weight,
                        gap_weight, tail_weight, balance_weight,
                        laowanjia_weight, exploration_rate
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    _weight_params(version_id, weights),
                )
            conn.commit()
        return {"status": "ok", "storage": "cloud", "version_id": version_id}
    except Exception as exc:
        logger.exception("cloud strategy version save failed")
        cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into strategy_versions
                (
                    version, "window", evaluated_predictions, average_hits,
                    super_hit_rate, rank_score, is_active, is_candidate,
                    description
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _version_params(version),
            )
            version_id = int(cursor.lastrowid)
            conn.execute(
                """
                insert into strategy_weights
                (
                    version_id, hot_weight, cold_weight, missing_weight,
                    gap_weight, tail_weight, balance_weight,
                    laowanjia_weight, exploration_rate
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _weight_params(version_id, weights),
            )
        return {"status": "ok", "storage": "sqlite", "version_id": version_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite strategy version save failed")
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
        logger.exception("cloud strategy evolution query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite strategy evolution query failed")
        return []


def _row_to_version(row: Any) -> dict:
    weights = {
        "hot": row[11],
        "cold": row[12],
        "missing": row[13],
        "gap": row[14],
        "tail": row[15],
        "balance": row[16],
        "laowanjia": row[17],
        "exploration": row[18],
    }
    return {
        "id": row[0],
        "version": row[1],
        "created_at": str(row[2]) if row[2] is not None else None,
        "window": row[3],
        "evaluated_predictions": row[4],
        "average_hits": row[5],
        "super_hit_rate": row[6],
        "rank_score": row[7],
        "is_active": bool(row[8]),
        "is_candidate": bool(row[9]),
        "candidate": bool(row[9]),
        "description": row[10],
        "recommended_weights": weights,
    }


SELECT_VERSION_SQL = """
select
    sv.id, sv.version, sv.created_at, sv."window",
    sv.evaluated_predictions, sv.average_hits, sv.super_hit_rate,
    sv.rank_score, sv.is_active, sv.is_candidate, sv.description,
    sw.hot_weight, sw.cold_weight, sw.missing_weight, sw.gap_weight,
    sw.tail_weight, sw.balance_weight, sw.laowanjia_weight,
    sw.exploration_rate
from strategy_versions sv
left join strategy_weights sw on sw.version_id = sv.id
"""


def get_latest_strategy_version() -> dict | None:
    rows = _query_with_fallback(
        SELECT_VERSION_SQL + " order by sv.version desc, sv.id desc limit 1",
    )
    return _row_to_version(rows[0]) if rows else None


def get_strategy_version_history(limit: int = 20) -> list[dict]:
    rows = _query_with_fallback(
        SELECT_VERSION_SQL + " order by sv.version desc, sv.id desc limit %s",
        (limit,),
        sqlite_sql=SELECT_VERSION_SQL + " order by sv.version desc, sv.id desc limit ?",
    )
    return [_row_to_version(row) for row in rows]


def get_next_strategy_version_number() -> int:
    latest = get_latest_strategy_version()
    if not latest:
        return 1
    try:
        return int(latest.get("version") or 0) + 1
    except Exception:
        return 1
