from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_recovery_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists daily_recovery_reports (
                            id bigserial primary key,
                            status text not null,
                            started_at timestamptz not null default now(),
                            finished_at timestamptz,
                            lookback_days integer not null default 1,
                            checked_issue_count integer not null default 0,
                            repaired_issue_count integer not null default 0,
                            failed_issue_count integer not null default 0,
                            analysis_status text,
                            prediction_lifecycle_status text,
                            verification_status text,
                            learning_status text,
                            health_status text,
                            report_json jsonb not null default '{}'::jsonb
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
                create table if not exists daily_recovery_reports (
                    id integer primary key autoincrement,
                    status text not null,
                    started_at text not null default current_timestamp,
                    finished_at text,
                    lookback_days integer not null default 1,
                    checked_issue_count integer not null default 0,
                    repaired_issue_count integer not null default 0,
                    failed_issue_count integer not null default 0,
                    analysis_status text,
                    prediction_lifecycle_status text,
                    verification_status text,
                    learning_status text,
                    health_status text,
                    report_json text not null default '{}'
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        results["sqlite"] = "error"
    return results


def save_recovery_report(report: dict) -> dict:
    params = (
        report.get("status") or "unknown",
        report.get("started_at") or _now(),
        report.get("finished_at"),
        int(report.get("lookback_days") or 1),
        int(report.get("checked_issue_count") or 0),
        int(report.get("repaired_issue_count") or 0),
        int(report.get("failed_issue_count") or 0),
        report.get("analysis_status"),
        report.get("prediction_lifecycle_status"),
        report.get("verification_status"),
        report.get("learning_status"),
        report.get("health_status"),
        _json_dumps(report),
    )
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into daily_recovery_reports (
                            status, started_at, finished_at, lookback_days, checked_issue_count,
                            repaired_issue_count, failed_issue_count, analysis_status,
                            prediction_lifecycle_status, verification_status, learning_status,
                            health_status, report_json
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        returning id
                        """,
                        params,
                        prepare=False,
                    )
                    row_id = int(cur.fetchone()[0])
                conn.commit()
            return {"status": "ok", "storage": "cloud", "id": row_id}
        except Exception:
            pass
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into daily_recovery_reports (
                status, started_at, finished_at, lookback_days, checked_issue_count,
                repaired_issue_count, failed_issue_count, analysis_status,
                prediction_lifecycle_status, verification_status, learning_status,
                health_status, report_json
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    return {"status": "ok", "storage": "sqlite", "id": int(cursor.lastrowid or 0)}


def get_latest_recovery_report() -> dict | None:
    select_sql = """
        select id, status, started_at, finished_at, lookback_days, checked_issue_count,
               repaired_issue_count, failed_issue_count, analysis_status,
               prediction_lifecycle_status, verification_status, learning_status,
               health_status, report_json
        from daily_recovery_reports
        order by id desc
        limit 1
    """
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(select_sql, prepare=False)
                    row = cur.fetchone()
            if not row:
                return None
            payload = _json_loads(row[13]) or {}
            payload.update(
                {
                    "id": row[0],
                    "status": row[1],
                    "started_at": str(row[2]) if row[2] is not None else None,
                    "finished_at": str(row[3]) if row[3] is not None else None,
                    "lookback_days": row[4],
                    "checked_issue_count": row[5],
                    "repaired_issue_count": row[6],
                    "failed_issue_count": row[7],
                    "analysis_status": row[8],
                    "prediction_lifecycle_status": row[9],
                    "verification_status": row[10],
                    "learning_status": row[11],
                    "health_status": row[12],
                }
            )
            return payload
        except Exception:
            pass
    try:
        with _sqlite_connection() as conn:
            row = conn.execute(select_sql).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    payload = _json_loads(row[13]) or {}
    payload.update(
        {
            "id": row[0],
            "status": row[1],
            "started_at": str(row[2]) if row[2] is not None else None,
            "finished_at": str(row[3]) if row[3] is not None else None,
            "lookback_days": row[4],
            "checked_issue_count": row[5],
            "repaired_issue_count": row[6],
            "failed_issue_count": row[7],
            "analysis_status": row[8],
            "prediction_lifecycle_status": row[9],
            "verification_status": row[10],
            "learning_status": row[11],
            "health_status": row[12],
        }
    )
    return payload
