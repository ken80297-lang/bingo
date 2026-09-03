from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"
_DB_PATH_STATUS_LOCK = threading.Lock()
_DB_PATH_STATUS: dict[str, Any] = {
    "backend": None,
    "result": None,
    "fallback_occurred": False,
    "error_type": None,
}


def _record_db_path_status(
    *,
    backend: str | None,
    result: str | None,
    fallback_occurred: bool,
    error_type: str | None = None,
) -> None:
    with _DB_PATH_STATUS_LOCK:
        _DB_PATH_STATUS.update(
            {
                "backend": backend,
                "result": result,
                "fallback_occurred": fallback_occurred,
                "error_type": error_type,
            }
        )


def get_collector_db_path_status() -> dict[str, Any]:
    with _DB_PATH_STATUS_LOCK:
        return dict(_DB_PATH_STATUS)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _dashboard_read_connection():
    from database.postgres import dashboard_read_connection

    return dashboard_read_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_collector_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    try:
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists draw_history (
                        id bigserial primary key,
                        issue text unique not null,
                        draw_time text,
                        numbers jsonb not null,
                        super_number integer,
                        big_small text,
                        odd_even text,
                        source text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
                cur.execute(
                    """
                    create table if not exists kuaishou_snapshots (
                        id bigserial primary key,
                        issue text unique null,
                        draw_time text null,
                        raw_html text,
                        parsed_json jsonb,
                        source text,
                        created_at timestamptz default now(),
                        updated_at timestamptz default now()
                    )
                    """
                )
            conn.commit()
        results["cloud"] = "available"
    except Exception:
        logger.exception("failed to initialize cloud collector tables")
        results["cloud"] = "unknown"

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists draw_history (
                    id integer primary key autoincrement,
                    issue text unique not null,
                    draw_time text,
                    numbers text not null,
                    super_number integer,
                    big_small text,
                    odd_even text,
                    source text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists kuaishou_snapshots (
                    id integer primary key autoincrement,
                    issue text unique null,
                    draw_time text null,
                    raw_html text,
                    parsed_json text,
                    source text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite collector tables")
        results["sqlite"] = "unknown"

    return results


def _save_draw_history_cloud(data: dict) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into draw_history
                (issue, draw_time, numbers, super_number, big_small, odd_even, source, updated_at)
                values (%s, %s, %s::jsonb, %s, %s, %s, %s, now())
                on conflict (issue) do update set
                    draw_time = excluded.draw_time,
                    numbers = excluded.numbers,
                    super_number = excluded.super_number,
                    big_small = excluded.big_small,
                    odd_even = excluded.odd_even,
                    source = excluded.source,
                    updated_at = now()
                """,
                (
                    data["issue"],
                    data.get("draw_time"),
                    _json_dumps(data.get("numbers", [])),
                    data.get("super_number"),
                    data.get("big_small"),
                    data.get("odd_even"),
                    data.get("source", "unknown"),
                ),
            )
        conn.commit()


def _save_draw_history_sqlite(data: dict) -> None:
    with _sqlite_connection() as conn:
        conn.execute(
            """
            insert into draw_history
            (issue, draw_time, numbers, super_number, big_small, odd_even, source, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(issue) do update set
                draw_time = excluded.draw_time,
                numbers = excluded.numbers,
                super_number = excluded.super_number,
                big_small = excluded.big_small,
                odd_even = excluded.odd_even,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                data["issue"],
                data.get("draw_time"),
                _json_dumps(data.get("numbers", [])),
                data.get("super_number"),
                data.get("big_small"),
                data.get("odd_even"),
                data.get("source", "unknown"),
                _now(),
            ),
        )


def save_draw_history(data: dict) -> dict:
    if not data.get("issue"):
        return {"status": "error", "storage": None, "error": "missing issue"}

    try:
        _save_draw_history_cloud(data)
        return {"status": "ok", "storage": "cloud", "issue": data.get("issue")}
    except Exception as exc:
        logger.exception("cloud draw_history upsert failed")
        cloud_error = str(exc)

    try:
        _save_draw_history_sqlite(data)
        return {
            "status": "ok",
            "storage": "sqlite",
            "issue": data.get("issue"),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite draw_history upsert failed")
        return {"status": "error", "storage": None, "issue": data.get("issue"), "error": str(exc)}


def _save_kuaishou_snapshot_cloud(data: dict) -> None:
    issue = data.get("issue")
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            if issue:
                cur.execute(
                    """
                    insert into kuaishou_snapshots
                    (issue, draw_time, raw_html, parsed_json, source, updated_at)
                    values (%s, %s, %s, %s::jsonb, %s, now())
                    on conflict (issue) do update set
                        draw_time = excluded.draw_time,
                        raw_html = excluded.raw_html,
                        parsed_json = excluded.parsed_json,
                        source = excluded.source,
                        updated_at = now()
                    """,
                    (
                        issue,
                        data.get("draw_time"),
                        data.get("raw_html", ""),
                        _json_dumps(data.get("parsed_json", {})),
                        data.get("source", "kuaishou"),
                    ),
                )
            else:
                cur.execute(
                    """
                    insert into kuaishou_snapshots
                    (issue, draw_time, raw_html, parsed_json, source, updated_at)
                    values (%s, %s, %s, %s::jsonb, %s, now())
                    """,
                    (
                        None,
                        data.get("draw_time"),
                        data.get("raw_html", ""),
                        _json_dumps(data.get("parsed_json", {})),
                        data.get("source", "kuaishou"),
                    ),
                )
        conn.commit()


def _save_kuaishou_snapshot_sqlite(data: dict) -> None:
    issue = data.get("issue")
    with _sqlite_connection() as conn:
        if issue:
            conn.execute(
                """
                insert into kuaishou_snapshots
                (issue, draw_time, raw_html, parsed_json, source, updated_at)
                values (?, ?, ?, ?, ?, ?)
                on conflict(issue) do update set
                    draw_time = excluded.draw_time,
                    raw_html = excluded.raw_html,
                    parsed_json = excluded.parsed_json,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    issue,
                    data.get("draw_time"),
                    data.get("raw_html", ""),
                    _json_dumps(data.get("parsed_json", {})),
                    data.get("source", "kuaishou"),
                    _now(),
                ),
            )
        else:
            conn.execute(
                """
                insert into kuaishou_snapshots
                (issue, draw_time, raw_html, parsed_json, source, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    None,
                    data.get("draw_time"),
                    data.get("raw_html", ""),
                    _json_dumps(data.get("parsed_json", {})),
                    data.get("source", "kuaishou"),
                    _now(),
                ),
            )


def save_kuaishou_snapshot(data: dict) -> dict:
    try:
        _save_kuaishou_snapshot_cloud(data)
        return {"status": "ok", "storage": "cloud", "issue": data.get("issue")}
    except Exception as exc:
        logger.exception("cloud kuaishou snapshot upsert failed")
        cloud_error = str(exc)

    try:
        _save_kuaishou_snapshot_sqlite(data)
        return {
            "status": "ok",
            "storage": "sqlite",
            "issue": data.get("issue"),
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite kuaishou snapshot upsert failed")
        return {"status": "error", "storage": None, "issue": data.get("issue"), "error": str(exc)}


def _row_to_draw(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "draw_time": row[2],
        "numbers": _json_loads(row[3]) or [],
        "super_number": row[4],
        "big_small": row[5],
        "odd_even": row[6],
        "source": row[7],
        "created_at": str(row[8]) if row[8] is not None else None,
        "updated_at": str(row[9]) if row[9] is not None else None,
    }


def _row_to_snapshot(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "draw_time": row[2],
        "raw_html": row[3],
        "parsed_json": _json_loads(row[4]) or {},
        "source": row[5],
        "created_at": str(row[6]) if row[6] is not None else None,
        "updated_at": str(row[7]) if row[7] is not None else None,
    }


def _row_to_snapshot_summary(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "draw_time": row[2],
        "raw_html": None,
        "parsed_json": {},
        "source": row[3],
        "created_at": str(row[4]) if row[4] is not None else None,
        "updated_at": str(row[5]) if row[5] is not None else None,
    }


def _query_cloud(sql: str, params: tuple = (), *, operation: str | None = None) -> list[Any]:
    connect_start = time.perf_counter()
    try:
        conn = _dashboard_read_connection() if operation == "kuaishou_latest" else _cloud_connection()
    except Exception as exc:
        if operation:
            logger.warning(
                "postgres_latency operation=%s connect_ms=%s result=failed error_type=%s",
                operation,
                round((time.perf_counter() - connect_start) * 1000, 2),
                type(exc).__name__,
            )
        raise

    connect_ms = round((time.perf_counter() - connect_start) * 1000, 2)
    with conn:
        query_start = time.perf_counter()
        with conn.cursor() as cur:
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            except Exception as exc:
                if operation:
                    logger.warning(
                        "postgres_latency operation=%s connect_ms=%s query_ms=%s result=failed error_type=%s",
                        operation,
                        connect_ms,
                        round((time.perf_counter() - query_start) * 1000, 2),
                        type(exc).__name__,
                    )
                raise
            if operation:
                logger.warning(
                    "postgres_latency operation=%s connect_ms=%s query_ms=%s result=success",
                    operation,
                    connect_ms,
                    round((time.perf_counter() - query_start) * 1000, 2),
                )
            return rows


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def get_latest_draw_history() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, numbers, super_number, big_small, odd_even, source, created_at, updated_at
        from draw_history order by issue desc limit 1
        """,
        (),
    )
    return _row_to_draw(rows[0]) if rows else None


def get_draw_history(limit: int = 50) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, numbers, super_number, big_small, odd_even, source, created_at, updated_at
        from draw_history order by issue desc limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, draw_time, numbers, super_number, big_small, odd_even, source, created_at, updated_at
        from draw_history order by issue desc limit ?
        """,
    )
    return [_row_to_draw(row) for row in rows]


def get_latest_kuaishou_snapshot() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, raw_html, parsed_json, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit 1
        """,
        (),
    )
    return _row_to_snapshot(rows[0]) if rows else None


def get_latest_kuaishou_summary() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit 1
        """,
        (),
        sqlite_sql="""
        select id, issue, draw_time, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit 1
        """,
        operation="kuaishou_latest",
    )
    return _row_to_snapshot_summary(rows[0]) if rows else None


def get_kuaishou_history(limit: int = 50) -> list[dict]:
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, raw_html, parsed_json, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, draw_time, raw_html, parsed_json, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit ?
        """,
    )
    return [_row_to_snapshot(row) for row in rows]


def get_kuaishou_summary_history(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    rows = _query_with_fallback(
        """
        select id, issue, draw_time, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, draw_time, source, created_at, updated_at
        from kuaishou_snapshots order by updated_at desc, id desc limit ?
        """,
    )
    return [_row_to_snapshot_summary(row) for row in rows]


def _query_with_fallback(
    sql: str,
    params: tuple = (),
    sqlite_sql: str | None = None,
    *,
    operation: str | None = None,
) -> list[Any]:
    try:
        rows = _query_cloud(sql, params, operation=operation) if operation else _query_cloud(sql, params)
        _record_db_path_status(
            backend="postgres",
            result="success",
            fallback_occurred=False,
            error_type=None,
        )
        logger.info("collector_store_query backend=postgres result=success")
        return rows
    except Exception as exc:
        cloud_error_type = type(exc).__name__
        _record_db_path_status(
            backend="postgres",
            result="failed",
            fallback_occurred=False,
            error_type=cloud_error_type,
        )
        logger.warning(
            "collector_store_query backend=postgres result=failed error_type=%s",
            cloud_error_type,
        )

    try:
        logger.info("collector_store_query backend=sqlite result=fallback")
        rows = _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
        _record_db_path_status(
            backend="sqlite",
            result="success",
            fallback_occurred=True,
            error_type=cloud_error_type,
        )
        logger.info("collector_store_query backend=sqlite result=success")
        return rows
    except Exception as exc:
        sqlite_error_type = type(exc).__name__
        _record_db_path_status(
            backend="sqlite",
            result="failed",
            fallback_occurred=True,
            error_type=sqlite_error_type,
        )
        logger.warning(
            "collector_store_query backend=sqlite result=failed error_type=%s",
            sqlite_error_type,
        )
        return []


def get_collector_status() -> dict:
    latest_kuaishou = get_latest_kuaishou_snapshot()
    latest_pilio = get_latest_draw_history()

    return {
        "kuaishou": {
            "latest_issue": latest_kuaishou.get("issue") if latest_kuaishou else None,
            "last_update": latest_kuaishou.get("updated_at") if latest_kuaishou else None,
            "status": "ok" if latest_kuaishou else "unknown",
        },
        "pilio": {
            "latest_issue": latest_pilio.get("issue") if latest_pilio else None,
            "last_update": latest_pilio.get("updated_at") if latest_pilio else None,
            "status": "ok" if latest_pilio else "unknown",
        },
    }
