from __future__ import annotations

import json
import logging
import os
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


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_official_draw_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists official_draw_history (
                            id bigserial primary key,
                            issue text unique,
                            draw_date date,
                            draw_time text,
                            numbers jsonb,
                            open_order_numbers jsonb,
                            super_number integer,
                            win_no_only boolean,
                            source text,
                            verification_status text default 'validated',
                            fetched_at timestamptz,
                            verified boolean default false,
                            raw_json jsonb,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now()
                        )
                        """
                    )
                    cur.execute("alter table official_draw_history add column if not exists verification_status text default 'validated'")
                    cur.execute("alter table official_draw_history add column if not exists fetched_at timestamptz")
                    cur.execute(
                        """
                        create table if not exists draw_verification (
                            id bigserial primary key,
                            issue text unique,
                            kuaishou_numbers jsonb,
                            official_numbers jsonb,
                            kuaishou_super integer,
                            official_super integer,
                            numbers_match boolean,
                            super_match boolean,
                            verified boolean,
                            status text,
                            verified_at timestamptz,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now()
                        )
                        """
                    )
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            logger.exception("failed to initialize cloud official draw tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists official_draw_history (
                    id integer primary key autoincrement,
                    issue text unique,
                    draw_date text,
                    draw_time text,
                    numbers text,
                    open_order_numbers text,
                    super_number integer,
                    win_no_only integer,
                    source text,
                    verification_status text default 'validated',
                    fetched_at text,
                    verified integer default 0,
                    raw_json text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            existing = {row[1] for row in conn.execute("pragma table_info(official_draw_history)").fetchall()}
            if "verification_status" not in existing:
                conn.execute("alter table official_draw_history add column verification_status text default 'validated'")
            if "fetched_at" not in existing:
                conn.execute("alter table official_draw_history add column fetched_at text")
            conn.execute(
                """
                create table if not exists draw_verification (
                    id integer primary key autoincrement,
                    issue text unique,
                    kuaishou_numbers text,
                    official_numbers text,
                    kuaishou_super integer,
                    official_super integer,
                    numbers_match integer,
                    super_match integer,
                    verified integer,
                    status text,
                    verified_at text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite official draw tables")

    return results


def _draw_params(draw: dict) -> tuple:
    return (
        draw.get("issue"),
        draw.get("draw_date"),
        draw.get("draw_time"),
        _json_dumps(draw.get("numbers", [])),
        _json_dumps(draw.get("open_order_numbers", [])),
        draw.get("super_number"),
        bool(draw.get("win_no_only")),
        draw.get("source", "taiwan_lottery"),
        draw.get("verification_status") or "validated",
        draw.get("fetched_at") or _now(),
        bool(draw.get("verified", False)),
        _json_dumps(draw.get("raw_json", {})),
    )


def _verification_params(item: dict) -> tuple:
    return (
        item.get("issue"),
        _json_dumps(item.get("kuaishou_numbers", [])),
        _json_dumps(item.get("official_numbers", [])),
        item.get("kuaishou_super"),
        item.get("official_super"),
        bool(item.get("numbers_match")),
        bool(item.get("super_match")),
        bool(item.get("verified")),
        item.get("status"),
        item.get("verified_at"),
    )


def _valid_draw(draw: dict) -> bool:
    issue = str(draw.get("issue") or "").strip()
    if not issue or not issue.isdigit():
        return False
    try:
        numbers = [int(value) for value in draw.get("numbers") or []]
    except Exception:
        return False
    if len(numbers) != 20 or len(set(numbers)) != 20:
        return False
    if any(number < 1 or number > 80 for number in numbers):
        return False
    super_number = draw.get("super_number")
    if super_number is not None:
        try:
            super_value = int(super_number)
        except Exception:
            return False
        if super_value < 1 or super_value > 80:
            return False
    return True


def _save_official_cloud(draws: list[dict]) -> None:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            for draw in draws:
                    cur.execute(
                        """
                        insert into official_draw_history
                    (
                        issue, draw_date, draw_time, numbers, open_order_numbers,
                        super_number, win_no_only, source, verification_status,
                        fetched_at, verified, raw_json, updated_at
                    )
                    values (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    on conflict (issue) do update set
                        draw_date = excluded.draw_date,
                        draw_time = coalesce(excluded.draw_time, official_draw_history.draw_time),
                        numbers = excluded.numbers,
                        open_order_numbers = excluded.open_order_numbers,
                        super_number = excluded.super_number,
                        win_no_only = excluded.win_no_only,
                        source = excluded.source,
                        verification_status = excluded.verification_status,
                        fetched_at = coalesce(excluded.fetched_at, official_draw_history.fetched_at),
                        raw_json = excluded.raw_json,
                        updated_at = now()
                    """,
                    _draw_params(draw),
                    prepare=False,
                )
        conn.commit()


def _save_official_sqlite(draws: list[dict]) -> None:
    with _sqlite_connection() as conn:
        for draw in draws:
            params = list(_draw_params(draw))
            params[6] = 1 if params[6] else 0
            params[10] = 1 if params[10] else 0
            conn.execute(
                """
                insert into official_draw_history
                (
                    issue, draw_date, draw_time, numbers, open_order_numbers,
                    super_number, win_no_only, source, verification_status,
                    fetched_at, verified, raw_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(issue) do update set
                    draw_date = excluded.draw_date,
                    draw_time = coalesce(excluded.draw_time, official_draw_history.draw_time),
                    numbers = excluded.numbers,
                    open_order_numbers = excluded.open_order_numbers,
                    super_number = excluded.super_number,
                    win_no_only = excluded.win_no_only,
                    source = excluded.source,
                    verification_status = excluded.verification_status,
                    fetched_at = coalesce(excluded.fetched_at, official_draw_history.fetched_at),
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (*params, _now()),
            )


def save_official_draws(draws: list[dict]) -> dict:
    valid_draws = [draw for draw in draws or [] if _valid_draw(draw)]
    invalid_count = len(draws or []) - len(valid_draws)
    if not valid_draws:
        return {"status": "ok", "saved": 0, "invalid": invalid_count, "storage": None}

    cloud_error = None
    if _cloud_enabled():
        try:
            _save_official_cloud(valid_draws)
            return {"status": "ok", "saved": len(valid_draws), "invalid": invalid_count, "storage": "cloud"}
        except Exception as exc:
            logger.exception("cloud official draws upsert failed")
            cloud_error = str(exc)

    try:
        _save_official_sqlite(valid_draws)
        return {
            "status": "ok",
            "saved": len(valid_draws),
            "invalid": invalid_count,
            "storage": "sqlite",
            "cloud_error": cloud_error,
        }
    except Exception as exc:
        logger.exception("sqlite official draws upsert failed")
        return {"status": "error", "saved": 0, "storage": None, "error": str(exc)}


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params, prepare=False)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _query_with_fallback(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    if _cloud_enabled():
        try:
            return _query_cloud(sql, params)
        except Exception as exc:
            logger.exception("cloud official query failed")
            if "verification_status" in str(exc) or "fetched_at" in str(exc):
                try:
                    init_official_draw_tables()
                    return _query_cloud(sql, params)
                except Exception:
                    logger.exception("cloud official query retry after init failed")

    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception as exc:
        logger.exception("sqlite official query failed")
        if "verification_status" in str(exc) or "fetched_at" in str(exc):
            try:
                init_official_draw_tables()
                return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
            except Exception:
                logger.exception("sqlite official query retry after init failed")
        return []


def _row_to_official(row: Any) -> dict:
    has_quality_columns = len(row) >= 15
    verification_status = row[10] if has_quality_columns else None
    fetched_at = row[11] if has_quality_columns else None
    offset = 2 if has_quality_columns else 0
    return {
        "id": row[0],
        "issue": row[1],
        "draw_date": str(row[2]) if row[2] is not None else None,
        "draw_time": row[3],
        "numbers": _json_loads(row[4]) or [],
        "open_order_numbers": _json_loads(row[5]) or [],
        "super_number": row[6],
        "win_no_only": bool(row[7]),
        "source": row[8],
        "verified": bool(row[9 + offset]),
        "verification_status": verification_status or "validated",
        "fetched_at": str(fetched_at) if fetched_at is not None else None,
        "raw_json": _json_loads(row[10 + offset]) or {},
        "created_at": str(row[11 + offset]) if row[11 + offset] is not None else None,
        "updated_at": str(row[12 + offset]) if row[12 + offset] is not None else None,
    }


def _row_to_verification(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "kuaishou_numbers": _json_loads(row[2]) or [],
        "official_numbers": _json_loads(row[3]) or [],
        "kuaishou_super": row[4],
        "official_super": row[5],
        "numbers_match": bool(row[6]),
        "super_match": bool(row[7]),
        "verified": bool(row[8]),
        "status": row[9],
        "verified_at": str(row[10]) if row[10] is not None else None,
        "created_at": str(row[11]) if row[11] is not None else None,
        "updated_at": str(row[12]) if row[12] is not None else None,
    }


def get_official_draw_by_issue(issue: str, verified_only: bool = False) -> dict | None:
    where_verified = "and verified = true" if verified_only else ""
    sqlite_where_verified = "and verified = 1" if verified_only else ""
    rows = _query_with_fallback(
        f"""
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue = %s {where_verified}
        limit 1
        """,
        (str(issue),),
        sqlite_sql=f"""
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue = ? {sqlite_where_verified}
        limit 1
        """,
    )
    return _row_to_official(rows[0]) if rows else None


def get_latest_official_draw() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue ~ '^[0-9]+$'
          and length(issue) >= 6
          and issue not like '99%%'
          and upper(issue) not like 'TEST%%'
        order by issue::bigint desc
        limit 1
        """,
        sqlite_sql="""
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue glob '[0-9]*'
          and length(issue) >= 6
          and issue not like '99%'
          and upper(issue) not like 'TEST%'
        order by cast(issue as integer) desc
        limit 1
        """,
    )
    return _row_to_official(rows[0]) if rows else None


def get_official_draw_history(limit: int = 30) -> list[dict]:
    limit = max(1, min(int(limit or 30), 200))
    rows = _query_with_fallback(
        """
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue ~ '^[0-9]+$'
          and length(issue) >= 6
          and issue not like '99%%'
          and upper(issue) not like 'TEST%%'
        order by issue::bigint desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, draw_date, draw_time, numbers, open_order_numbers,
               super_number, win_no_only, source, verification_status, fetched_at,
               verified, raw_json, created_at, updated_at
        from official_draw_history
        where issue glob '[0-9]*'
          and length(issue) >= 6
          and issue not like '99%'
          and upper(issue) not like 'TEST%'
        order by cast(issue as integer) desc
        limit ?
        """,
    )
    return [_row_to_official(row) for row in rows]


def save_draw_verification(item: dict) -> dict:
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into draw_verification
                        (
                            issue, kuaishou_numbers, official_numbers, kuaishou_super,
                            official_super, numbers_match, super_match, verified,
                            status, verified_at, updated_at
                        )
                        values (%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, now())
                        on conflict (issue) do update set
                            kuaishou_numbers = excluded.kuaishou_numbers,
                            official_numbers = excluded.official_numbers,
                            kuaishou_super = excluded.kuaishou_super,
                            official_super = excluded.official_super,
                            numbers_match = excluded.numbers_match,
                            super_match = excluded.super_match,
                            verified = excluded.verified,
                            status = excluded.status,
                            verified_at = excluded.verified_at,
                            updated_at = now()
                        returning id
                        """,
                        _verification_params(item),
                        prepare=False,
                    )
                    verification_id = int(cur.fetchone()[0])
                    if item.get("verified"):
                        cur.execute(
                            "update official_draw_history set verified = true, updated_at = now() where issue = %s",
                            (item.get("issue"),),
                            prepare=False,
                        )
                conn.commit()
            return {"status": "ok", "storage": "cloud", "id": verification_id}
        except Exception as exc:
            logger.exception("cloud draw verification save failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            params = list(_verification_params(item))
            for index in (5, 6, 7):
                params[index] = 1 if params[index] else 0
            cursor = conn.execute(
                """
                insert into draw_verification
                (
                    issue, kuaishou_numbers, official_numbers, kuaishou_super,
                    official_super, numbers_match, super_match, verified,
                    status, verified_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(issue) do update set
                    kuaishou_numbers = excluded.kuaishou_numbers,
                    official_numbers = excluded.official_numbers,
                    kuaishou_super = excluded.kuaishou_super,
                    official_super = excluded.official_super,
                    numbers_match = excluded.numbers_match,
                    super_match = excluded.super_match,
                    verified = excluded.verified,
                    status = excluded.status,
                    verified_at = excluded.verified_at,
                    updated_at = excluded.updated_at
                """,
                (*params, _now()),
            )
            if item.get("verified"):
                conn.execute(
                    "update official_draw_history set verified = 1, updated_at = ? where issue = ?",
                    (_now(), item.get("issue")),
                )
            verification_id = int(cursor.lastrowid or 0)
        return {"status": "ok", "storage": "sqlite", "id": verification_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite draw verification save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def save_draw_verifications(items: list[dict]) -> dict:
    if not items:
        return {"status": "ok", "saved": 0, "storage": None}

    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    for item in items:
                        cur.execute(
                            """
                            insert into draw_verification
                            (
                                issue, kuaishou_numbers, official_numbers, kuaishou_super,
                                official_super, numbers_match, super_match, verified,
                                status, verified_at, updated_at
                            )
                            values (%s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, now())
                            on conflict (issue) do update set
                                kuaishou_numbers = excluded.kuaishou_numbers,
                                official_numbers = excluded.official_numbers,
                                kuaishou_super = excluded.kuaishou_super,
                                official_super = excluded.official_super,
                                numbers_match = excluded.numbers_match,
                                super_match = excluded.super_match,
                                verified = excluded.verified,
                                status = excluded.status,
                                verified_at = excluded.verified_at,
                                updated_at = now()
                            """,
                            _verification_params(item),
                            prepare=False,
                        )
                        if item.get("verified"):
                            cur.execute(
                                "update official_draw_history set verified = true, updated_at = now() where issue = %s",
                                (item.get("issue"),),
                                prepare=False,
                            )
                conn.commit()
            return {"status": "ok", "storage": "cloud", "saved": len(items)}
        except Exception as exc:
            logger.exception("cloud draw verification batch save failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            for item in items:
                params = list(_verification_params(item))
                for index in (5, 6, 7):
                    params[index] = 1 if params[index] else 0
                conn.execute(
                    """
                    insert into draw_verification
                    (
                        issue, kuaishou_numbers, official_numbers, kuaishou_super,
                        official_super, numbers_match, super_match, verified,
                        status, verified_at, updated_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(issue) do update set
                        kuaishou_numbers = excluded.kuaishou_numbers,
                        official_numbers = excluded.official_numbers,
                        kuaishou_super = excluded.kuaishou_super,
                        official_super = excluded.official_super,
                        numbers_match = excluded.numbers_match,
                        super_match = excluded.super_match,
                        verified = excluded.verified,
                        status = excluded.status,
                        verified_at = excluded.verified_at,
                        updated_at = excluded.updated_at
                    """,
                    (*params, _now()),
                )
                if item.get("verified"):
                    conn.execute(
                        "update official_draw_history set verified = 1, updated_at = ? where issue = ?",
                        (_now(), item.get("issue")),
                    )
        return {"status": "ok", "storage": "sqlite", "saved": len(items), "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite draw verification batch save failed")
        return {"status": "error", "storage": None, "saved": 0, "error": str(exc)}


def get_latest_verification() -> dict | None:
    rows = _query_with_fallback(
        """
        select id, issue, kuaishou_numbers, official_numbers, kuaishou_super,
               official_super, numbers_match, super_match, verified, status,
               verified_at, created_at, updated_at
        from draw_verification
        order by issue desc
        limit 1
        """,
    )
    return _row_to_verification(rows[0]) if rows else None


def get_verification_history(limit: int = 30) -> list[dict]:
    limit = max(1, min(int(limit or 30), 200))
    rows = _query_with_fallback(
        """
        select id, issue, kuaishou_numbers, official_numbers, kuaishou_super,
               official_super, numbers_match, super_match, verified, status,
               verified_at, created_at, updated_at
        from draw_verification
        order by updated_at desc, id desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select id, issue, kuaishou_numbers, official_numbers, kuaishou_super,
               official_super, numbers_match, super_match, verified, status,
               verified_at, created_at, updated_at
        from draw_verification
        order by updated_at desc, id desc
        limit ?
        """,
    )
    return [_row_to_verification(row) for row in rows]


def get_official_statistics_counts() -> dict:
    rows = _query_with_fallback(
        """
        select
            sum(case when v.status = 'verified' then 1 else 0 end),
            sum(case when v.status = 'mismatch' then 1 else 0 end),
            sum(case when v.status = 'waiting_kuaishou' then 1 else 0 end),
            sum(case when v.status = 'waiting_official' then 1 else 0 end),
            sum(case when v.status = 'waiting_super_number' then 1 else 0 end),
            count(*)
        from draw_verification v
        left join official_draw_history o on o.issue = v.issue
        where v.issue is not null
          and v.issue not like '99%%'
          and upper(v.issue) not like 'TEST%%'
          and coalesce(lower(o.source), '') not like '%%test%%'
          and coalesce(lower(o.source), '') not like '%%phase%%'
        """,
        sqlite_sql="""
        select
            sum(case when v.status = 'verified' then 1 else 0 end),
            sum(case when v.status = 'mismatch' then 1 else 0 end),
            sum(case when v.status = 'waiting_kuaishou' then 1 else 0 end),
            sum(case when v.status = 'waiting_official' then 1 else 0 end),
            sum(case when v.status = 'waiting_super_number' then 1 else 0 end),
            count(*)
        from draw_verification v
        left join official_draw_history o on o.issue = v.issue
        where v.issue is not null
          and v.issue not like '99%'
          and upper(v.issue) not like 'TEST%'
          and coalesce(lower(o.source), '') not like '%test%'
          and coalesce(lower(o.source), '') not like '%phase%'
        """,
    )
    row = rows[0] if rows else (0, 0, 0, 0, 0, 0)
    waiting_kuaishou = int(row[2] or 0)
    waiting_official = int(row[3] or 0)
    waiting_super = int(row[4] or 0)
    return {
        "verified_count": int(row[0] or 0),
        "mismatch_count": int(row[1] or 0),
        "waiting_kuaishou_count": waiting_kuaishou,
        "waiting_official_count": waiting_official,
        "waiting_super_number_count": waiting_super,
        "waiting_count": waiting_kuaishou + waiting_official + waiting_super,
        "total_count": int(row[5] or 0),
    }
