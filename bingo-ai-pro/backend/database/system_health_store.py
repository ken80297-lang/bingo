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
