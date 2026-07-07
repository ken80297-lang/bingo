from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime
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


def init_data_quality_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists data_quality_reports (
                        id bigserial primary key,
                        report_date text,
                        source text,
                        total_records integer,
                        missing_issues jsonb,
                        duplicate_issues jsonb,
                        invalid_records jsonb,
                        latest_issue text,
                        earliest_issue text,
                        status text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud data_quality_reports table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists data_quality_reports (
                    id integer primary key autoincrement,
                    report_date text,
                    source text,
                    total_records integer,
                    missing_issues text,
                    duplicate_issues text,
                    invalid_records text,
                    latest_issue text,
                    earliest_issue text,
                    status text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite data_quality_reports table")

    return results


def _save_report_cloud(report: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into data_quality_reports
                (
                    report_date, source, total_records, missing_issues,
                    duplicate_issues, invalid_records, latest_issue,
                    earliest_issue, status, updated_at
                )
                values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, now())
                """,
                (
                    report.get("report_date"),
                    report.get("source"),
                    report.get("total_records", 0),
                    _json_dumps(report.get("missing_issues", [])),
                    _json_dumps(report.get("duplicate_issues", [])),
                    _json_dumps(report.get("invalid_records", [])),
                    report.get("latest_issue"),
                    report.get("earliest_issue"),
                    report.get("status"),
                ),
            )
        conn.commit()


def _save_report_sqlite(report: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into data_quality_reports
            (
                report_date, source, total_records, missing_issues,
                duplicate_issues, invalid_records, latest_issue,
                earliest_issue, status, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.get("report_date"),
                report.get("source"),
                report.get("total_records", 0),
                _json_dumps(report.get("missing_issues", [])),
                _json_dumps(report.get("duplicate_issues", [])),
                _json_dumps(report.get("invalid_records", [])),
                report.get("latest_issue"),
                report.get("earliest_issue"),
                report.get("status"),
                _now(),
            ),
        )


def save_data_quality_report(report: dict) -> dict:
    report = {
        "report_date": report.get("report_date") or date.today().isoformat(),
        "source": report.get("source", "kuaishou"),
        **report,
    }

    try:
        _save_report_cloud(report)
        return {"status": "ok", "storage": "cloud"}
    except Exception as exc:
        logger.exception("cloud data quality report insert failed")
        cloud_error = str(exc)

    try:
        _save_report_sqlite(report)
        return {"status": "ok", "storage": "sqlite", "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite data quality report insert failed")
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
        logger.exception("cloud data quality query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite data quality query failed")
        return []


def _row_to_report(row: Any) -> dict:
    return {
        "id": row[0],
        "report_date": row[1],
        "source": row[2],
        "total_records": row[3],
        "missing_issues": _json_loads(row[4]) or [],
        "duplicate_issues": _json_loads(row[5]) or [],
        "invalid_records": _json_loads(row[6]) or [],
        "latest_issue": row[7],
        "earliest_issue": row[8],
        "status": row[9],
        "created_at": str(row[10]) if row[10] is not None else None,
        "updated_at": str(row[11]) if row[11] is not None else None,
    }


def get_latest_data_quality_report() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, report_date, source, total_records, missing_issues,
               duplicate_issues, invalid_records, latest_issue,
               earliest_issue, status, created_at, updated_at
        from data_quality_reports
        order by created_at desc, id desc
        limit 1
        """,
    )
    return _row_to_report(rows[0]) if rows else None


def get_data_quality_reports(limit: int = 30) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, report_date, source, total_records, missing_issues,
               duplicate_issues, invalid_records, latest_issue,
               earliest_issue, status, created_at, updated_at
        from data_quality_reports
        order by created_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, report_date, source, total_records, missing_issues,
               duplicate_issues, invalid_records, latest_issue,
               earliest_issue, status, created_at, updated_at
        from data_quality_reports
        order by created_at desc, id desc
        limit ?
        """,
    )
    return [_row_to_report(row) for row in rows]


def get_data_quality_status() -> dict:
    latest = get_latest_data_quality_report()
    if not latest:
        return {
            "last_report_date": None,
            "status": "unknown",
            "missing_count": 0,
            "invalid_count": 0,
        }

    return {
        "last_report_date": latest.get("report_date"),
        "status": latest.get("status", "unknown"),
        "missing_count": len(latest.get("missing_issues") or []),
        "invalid_count": len(latest.get("invalid_records") or []),
    }
