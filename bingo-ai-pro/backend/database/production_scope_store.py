from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_production_scope_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists production_scope_registry (
                            id bigserial primary key,
                            source_table text not null,
                            source_id text not null,
                            issue text,
                            production_generation integer not null default 2,
                            production_valid boolean not null default true,
                            is_legacy boolean not null default false,
                            is_test_data boolean not null default false,
                            excluded_from_learning boolean not null default false,
                            excluded_from_statistics boolean not null default false,
                            excluded_from_recommendation boolean not null default false,
                            excluded_from_dashboard boolean not null default false,
                            exclusion_reason text,
                            created_at timestamptz not null default now(),
                            updated_at timestamptz not null default now(),
                            unique(source_table, source_id)
                        )
                        """,
                        prepare=False,
                    )
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            results["cloud"] = "error"
    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists production_scope_registry (
                    id integer primary key autoincrement,
                    source_table text not null,
                    source_id text not null,
                    issue text,
                    production_generation integer not null default 2,
                    production_valid integer not null default 1,
                    is_legacy integer not null default 0,
                    is_test_data integer not null default 0,
                    excluded_from_learning integer not null default 0,
                    excluded_from_statistics integer not null default 0,
                    excluded_from_recommendation integer not null default 0,
                    excluded_from_dashboard integer not null default 0,
                    exclusion_reason text,
                    created_at text not null default current_timestamp,
                    updated_at text not null default current_timestamp,
                    unique(source_table, source_id)
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        results["sqlite"] = "error"
    return results

