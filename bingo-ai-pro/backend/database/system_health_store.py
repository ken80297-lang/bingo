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


def init_system_health_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists system_health_reports (
                        id bigserial primary key,
                        latest_issue text,
                        status text,
                        payload jsonb,
                        created_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists health_cache (
                        cache_key text primary key,
                        payload_json jsonb,
                        health_status text,
                        generated_at timestamptz,
                        last_checked timestamptz,
                        last_refresh_attempt timestamptz,
                        last_refresh_success timestamptz,
                        last_refresh_error text,
                        refresh_duration_ms double precision,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud system_health_reports table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists system_health_reports (
                    id integer primary key autoincrement,
                    latest_issue text,
                    status text,
                    payload text,
                    created_at text default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists health_cache (
                    cache_key text primary key,
                    payload_json text,
                    health_status text,
                    generated_at text,
                    last_checked text,
                    last_refresh_attempt text,
                    last_refresh_success text,
                    last_refresh_error text,
                    refresh_duration_ms real,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite system_health_reports table")

    return results


def save_system_health_report(payload: dict) -> dict:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into system_health_reports
                    (latest_issue, status, payload)
                    values (%s, %s, %s::jsonb)
                    returning id
                    """,
                    (
                        payload.get("latest_issue"),
                        payload.get("status"),
                        _json_dumps(payload),
                    ),
                )
                report_id = int(cur.fetchone()[0])
            conn.commit()
        return {"status": "ok", "storage": "cloud", "id": report_id}
    except Exception as exc:
        logger.exception("cloud system health save failed")
        cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into system_health_reports
                (latest_issue, status, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    payload.get("latest_issue"),
                    payload.get("status"),
                    _json_dumps(payload),
                    _now(),
                ),
            )
            report_id = int(cursor.lastrowid)
        return {"status": "ok", "storage": "sqlite", "id": report_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite system health save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def get_latest_system_health_report() -> dict | None:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select payload
                    from system_health_reports
                    order by created_at desc, id desc
                    limit 1
                    """
                )
                row = cur.fetchone()
                return _json_loads(row[0]) if row else None
    except Exception:
        logger.exception("cloud system health query failed")

    try:
        with _sqlite_connection() as conn:
            row = conn.execute(
                """
                select payload
                from system_health_reports
                order by created_at desc, id desc
                limit 1
                """
            ).fetchone()
            return _json_loads(row[0]) if row else None
    except Exception:
        logger.exception("sqlite system health query failed")
        return None


def upsert_health_cache(cache_key: str, payload: dict) -> dict:
    health_status = payload.get("health_status") or payload.get("status")
    generated_at = payload.get("generated_at")
    last_checked = payload.get("last_checked") or generated_at
    last_refresh_attempt = payload.get("last_refresh_attempt")
    last_refresh_success = payload.get("last_refresh_success")
    last_refresh_error = payload.get("last_refresh_error")
    refresh_duration_ms = payload.get("refresh_duration_ms")

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into health_cache (
                        cache_key, payload_json, health_status, generated_at,
                        last_checked, last_refresh_attempt, last_refresh_success,
                        last_refresh_error, refresh_duration_ms, updated_at
                    )
                    values (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (cache_key) do update set
                        payload_json = excluded.payload_json,
                        health_status = excluded.health_status,
                        generated_at = excluded.generated_at,
                        last_checked = excluded.last_checked,
                        last_refresh_attempt = excluded.last_refresh_attempt,
                        last_refresh_success = excluded.last_refresh_success,
                        last_refresh_error = excluded.last_refresh_error,
                        refresh_duration_ms = excluded.refresh_duration_ms,
                        updated_at = now()
                    """,
                    (
                        cache_key,
                        _json_dumps(payload),
                        health_status,
                        generated_at,
                        last_checked,
                        last_refresh_attempt,
                        last_refresh_success,
                        last_refresh_error,
                        refresh_duration_ms,
                    ),
                )
            conn.commit()
        return {"status": "ok", "storage": "cloud"}
    except Exception as exc:
        logger.exception("cloud health cache upsert failed")
        cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                insert into health_cache (
                    cache_key, payload_json, health_status, generated_at,
                    last_checked, last_refresh_attempt, last_refresh_success,
                    last_refresh_error, refresh_duration_ms, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(cache_key) do update set
                    payload_json = excluded.payload_json,
                    health_status = excluded.health_status,
                    generated_at = excluded.generated_at,
                    last_checked = excluded.last_checked,
                    last_refresh_attempt = excluded.last_refresh_attempt,
                    last_refresh_success = excluded.last_refresh_success,
                    last_refresh_error = excluded.last_refresh_error,
                    refresh_duration_ms = excluded.refresh_duration_ms,
                    updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    _json_dumps(payload),
                    health_status,
                    generated_at,
                    last_checked,
                    last_refresh_attempt,
                    last_refresh_success,
                    last_refresh_error,
                    refresh_duration_ms,
                    _now(),
                    _now(),
                ),
            )
        return {"status": "ok", "storage": "sqlite", "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite health cache upsert failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def get_health_cache(cache_key: str) -> dict | None:
    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select payload_json
                    from health_cache
                    where cache_key = %s
                    limit 1
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
                return _json_loads(row[0]) if row else None
    except Exception:
        logger.exception("cloud health cache query failed")

    try:
        with _sqlite_connection() as conn:
            row = conn.execute(
                """
                select payload_json
                from health_cache
                where cache_key = ?
                limit 1
                """,
                (cache_key,),
            ).fetchone()
            return _json_loads(row[0]) if row else None
    except Exception:
        logger.exception("sqlite health cache query failed")
        return None
