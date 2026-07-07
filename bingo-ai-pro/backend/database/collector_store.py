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
    if isinstance(value, (list, dict)):
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


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


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


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    try:
        return _query_cloud(sql, params)
    except Exception:
        logger.exception("cloud collector query failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite collector query failed")
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
