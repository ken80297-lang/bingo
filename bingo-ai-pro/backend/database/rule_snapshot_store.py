from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


def init_rule_snapshot_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists rule_snapshots (
                            id bigserial primary key,
                            source_issue text,
                            target_issue text,
                            rule_library_version text,
                            snapshot_json jsonb,
                            generated_at timestamptz,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now(),
                            unique(source_issue, target_issue, rule_library_version)
                        )
                        """,
                        prepare=False,
                    )
                    cur.execute(
                        """
                        create index if not exists idx_rule_snapshots_source_target
                        on rule_snapshots (source_issue, target_issue)
                        """,
                        prepare=False,
                    )
                    cur.execute(
                        """
                        create index if not exists idx_rule_snapshots_updated_at
                        on rule_snapshots (updated_at)
                        """,
                        prepare=False,
                    )
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            logger.exception("failed to initialize cloud rule_snapshots table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists rule_snapshots (
                    id integer primary key autoincrement,
                    source_issue text,
                    target_issue text,
                    rule_library_version text,
                    snapshot_json text,
                    generated_at text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp,
                    unique(source_issue, target_issue, rule_library_version)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_rule_snapshots_source_target
                on rule_snapshots (source_issue, target_issue)
                """
            )
            conn.execute(
                """
                create index if not exists idx_rule_snapshots_updated_at
                on rule_snapshots (updated_at)
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite rule_snapshots table")

    return results


def save_rule_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {"status": "error", "storage": None, "error": "invalid snapshot"}
    source_issue = _string_or_none(snapshot.get("source_issue"))
    target_issue = _string_or_none(snapshot.get("target_issue"))
    version = _string_or_none(snapshot.get("rule_library_version"))
    if not source_issue or not version:
        return {
            "status": "error",
            "storage": None,
            "source_issue": source_issue,
            "target_issue": target_issue,
            "error": "missing source_issue or rule_library_version",
        }

    result = {
        "status": "ok",
        "storage": None,
        "source_issue": source_issue,
        "target_issue": target_issue,
        "rule_library_version": version,
    }
    if _cloud_enabled():
        try:
            result.update(_save_cloud(snapshot))
            return result
        except Exception as exc:
            logger.exception("cloud rule_snapshots upsert failed")
            result = {**result, "status": "error", "storage": "cloud", "error": str(exc)}

    try:
        result.update(_save_sqlite(snapshot))
        return result
    except Exception as exc:
        logger.exception("sqlite rule_snapshots upsert failed")
        return {**result, "status": "error", "storage": "sqlite", "error": str(exc)}


def get_rule_snapshot(
    *,
    source_issue: str | None = None,
    target_issue: str | None = None,
    rule_library_version: str | None = None,
) -> dict | None:
    filters = {
        "source_issue": _string_or_none(source_issue),
        "target_issue": _string_or_none(target_issue),
        "rule_library_version": _string_or_none(rule_library_version),
    }
    if _cloud_enabled():
        try:
            return _get_cloud(filters)
        except Exception:
            logger.exception("cloud rule_snapshots query failed")
    try:
        return _get_sqlite(filters)
    except Exception:
        logger.exception("sqlite rule_snapshots query failed")
        return None


def get_latest_rule_snapshot() -> dict | None:
    return get_rule_snapshot()


def get_rule_snapshots(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 1000))
    if _cloud_enabled():
        try:
            return _get_cloud_list(limit)
        except Exception:
            logger.exception("cloud rule_snapshots list query failed")
    try:
        return _get_sqlite_list(limit)
    except Exception:
        logger.exception("sqlite rule_snapshots list query failed")
        return []


def _save_cloud(snapshot: dict) -> dict:
    now = _now()
    target_issue = _storage_target_issue(snapshot.get("target_issue"))
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into rule_snapshots
                (
                    source_issue, target_issue, rule_library_version,
                    snapshot_json, generated_at, updated_at
                )
                values (%s, %s, %s, %s::jsonb, %s, %s)
                on conflict (source_issue, target_issue, rule_library_version)
                do update set
                    snapshot_json = excluded.snapshot_json,
                    generated_at = excluded.generated_at,
                    updated_at = excluded.updated_at
                returning id
                """,
                (
                    snapshot.get("source_issue"),
                    target_issue,
                    snapshot.get("rule_library_version"),
                    _json_dumps(snapshot),
                    snapshot.get("generated_at") or now,
                    now,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return {"status": "ok", "storage": "cloud", "id": int(row[0]) if row else None}


def _save_sqlite(snapshot: dict) -> dict:
    now = _now()
    target_issue = _storage_target_issue(snapshot.get("target_issue"))
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into rule_snapshots
            (
                source_issue, target_issue, rule_library_version,
                snapshot_json, generated_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?)
            on conflict(source_issue, target_issue, rule_library_version)
            do update set
                snapshot_json = excluded.snapshot_json,
                generated_at = excluded.generated_at,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.get("source_issue"),
                target_issue,
                snapshot.get("rule_library_version"),
                _json_dumps(snapshot),
                snapshot.get("generated_at") or now,
                now,
            ),
        )
        row = conn.execute(
            """
            select id
            from rule_snapshots
            where source_issue = ?
              and coalesce(target_issue, '') = coalesce(?, '')
              and rule_library_version = ?
            """,
            (
                snapshot.get("source_issue"),
                target_issue,
                snapshot.get("rule_library_version"),
            ),
        ).fetchone()
    return {"status": "ok", "storage": "sqlite", "id": int(row[0]) if row else cursor.lastrowid}


def _get_cloud(filters: dict) -> dict | None:
    where, params = _where_clause(filters, placeholder="%s")
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select id, source_issue, target_issue, rule_library_version,
                       snapshot_json, generated_at, created_at, updated_at
                from rule_snapshots
                {where}
                order by updated_at desc, id desc
                limit 1
                """,
                params,
            )
            row = cur.fetchone()
    return _row_to_record(row) if row else None


def _get_cloud_list(limit: int) -> list[dict]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id, source_issue, target_issue, rule_library_version,
                       snapshot_json, generated_at, created_at, updated_at
                from rule_snapshots
                order by updated_at desc, id desc
                limit %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def _get_sqlite(filters: dict) -> dict | None:
    where, params = _where_clause(filters, placeholder="?")
    with _sqlite_connection() as conn:
        row = conn.execute(
            f"""
            select id, source_issue, target_issue, rule_library_version,
                   snapshot_json, generated_at, created_at, updated_at
            from rule_snapshots
            {where}
            order by updated_at desc, id desc
            limit 1
            """,
            params,
        ).fetchone()
    return _row_to_record(row) if row else None


def _get_sqlite_list(limit: int) -> list[dict]:
    with _sqlite_connection() as conn:
        rows = conn.execute(
            """
            select id, source_issue, target_issue, rule_library_version,
                   snapshot_json, generated_at, created_at, updated_at
            from rule_snapshots
            order by updated_at desc, id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def _where_clause(filters: dict, *, placeholder: str) -> tuple[str, tuple]:
    clauses = []
    params = []
    for key in ("source_issue", "target_issue", "rule_library_version"):
        value = filters.get(key)
        if value is None:
            continue
        clauses.append(f"{key} = {placeholder}")
        params.append(value)
    return ("where " + " and ".join(clauses), tuple(params)) if clauses else ("", tuple())


def _row_to_record(row: Any) -> dict:
    snapshot = _json_loads(row[4]) or {}
    return {
        "id": row[0],
        "source_issue": row[1],
        "target_issue": _string_or_none(row[2]),
        "rule_library_version": row[3],
        "snapshot_json": snapshot,
        "generated_at": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _storage_target_issue(value: Any) -> str:
    return _string_or_none(value) or ""
