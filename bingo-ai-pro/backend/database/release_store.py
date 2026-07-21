from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.production_scope import get_production_generation, get_production_start_issue
from config.release import release_payload

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"
LEGACY_PLACEHOLDER_COMMIT_HASH = "0bf03c8416b1026c3483ff4de8bb10e62331e42c"


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


def init_release_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists production_release_registry (
                            id bigserial primary key,
                            release_name text not null,
                            phase text not null,
                            release_version text not null unique,
                            git_commit_hash text,
                            git_commit_short text,
                            git_branch text,
                            git_commit_message text,
                            deployed_at timestamptz,
                            deployment_provider text,
                            deployment_instance_id text,
                            production_generation integer not null default 2,
                            production_start_issue text not null default '115040780',
                            first_live_based_on_issue text,
                            first_live_prediction_issue text,
                            last_known_draw_issue_at_release text,
                            model_version text,
                            feature_version text,
                            learning_engine_version text,
                            observation_version text,
                            rule_library_version text,
                            dashboard_version text,
                            database_schema_version text,
                            environment_fingerprint text,
                            release_status text not null default 'registered',
                            is_active boolean not null default false,
                            is_rollback_candidate boolean not null default true,
                            previous_release_id bigint,
                            release_notes jsonb not null default '[]'::jsonb,
                            created_at timestamptz not null default now(),
                            updated_at timestamptz not null default now()
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
                create table if not exists production_release_registry (
                    id integer primary key autoincrement,
                    release_name text not null,
                    phase text not null,
                    release_version text not null unique,
                    git_commit_hash text,
                    git_commit_short text,
                    git_branch text,
                    git_commit_message text,
                    deployed_at text,
                    deployment_provider text,
                    deployment_instance_id text,
                    production_generation integer not null default 2,
                    production_start_issue text not null default '115040780',
                    first_live_based_on_issue text,
                    first_live_prediction_issue text,
                    last_known_draw_issue_at_release text,
                    model_version text,
                    feature_version text,
                    learning_engine_version text,
                    observation_version text,
                    rule_library_version text,
                    dashboard_version text,
                    database_schema_version text,
                    environment_fingerprint text,
                    release_status text not null default 'registered',
                    is_active integer not null default 0,
                    is_rollback_candidate integer not null default 1,
                    previous_release_id integer,
                    release_notes text not null default '[]',
                    created_at text not null default current_timestamp,
                    updated_at text not null default current_timestamp
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        results["sqlite"] = "error"
    ensure_default_release()
    return results


def _row_to_release(row: Any) -> dict:
    return {
        "id": row[0],
        "release_name": row[1],
        "phase": row[2],
        "release_version": row[3],
        "git_commit_hash": row[4],
        "git_commit_short": row[5],
        "git_branch": row[6],
        "git_commit_message": row[7],
        "deployed_at": str(row[8]) if row[8] is not None else None,
        "deployment_provider": row[9],
        "deployment_instance_id": row[10],
        "production_generation": row[11],
        "production_start_issue": row[12],
        "first_live_based_on_issue": row[13],
        "first_live_prediction_issue": row[14],
        "last_known_draw_issue_at_release": row[15],
        "model_version": row[16],
        "feature_version": row[17],
        "learning_engine_version": row[18],
        "observation_version": row[19],
        "rule_library_version": row[20],
        "dashboard_version": row[21],
        "database_schema_version": row[22],
        "environment_fingerprint": row[23],
        "release_status": row[24],
        "is_active": bool(row[25]),
        "is_rollback_candidate": bool(row[26]),
        "previous_release_id": row[27],
        "release_notes": _json_loads(row[28]) or [],
        "created_at": str(row[29]) if row[29] is not None else None,
        "updated_at": str(row[30]) if row[30] is not None else None,
    }


def _release_params(payload: dict) -> tuple:
    return (
        payload.get("release_name"),
        payload.get("phase"),
        payload.get("release_version"),
        payload.get("git_commit_hash"),
        payload.get("git_commit_short"),
        payload.get("git_branch"),
        payload.get("git_commit_message"),
        payload.get("deployed_at"),
        payload.get("deployment_provider"),
        payload.get("deployment_instance_id"),
        int(payload.get("production_generation") or get_production_generation()),
        str(payload.get("production_start_issue") or get_production_start_issue()),
        payload.get("first_live_based_on_issue"),
        payload.get("first_live_prediction_issue"),
        payload.get("last_known_draw_issue_at_release"),
        payload.get("model_version"),
        payload.get("feature_version"),
        payload.get("learning_engine_version"),
        payload.get("observation_version"),
        payload.get("rule_library_version"),
        payload.get("dashboard_version"),
        payload.get("database_schema_version"),
        payload.get("environment_fingerprint"),
        payload.get("release_status") or "registered",
        bool(payload.get("is_active", False)),
        bool(payload.get("is_rollback_candidate", True)),
        payload.get("previous_release_id"),
        _json_dumps(payload.get("release_notes") or []),
    )


def register_release(payload: dict, *, activate: bool = False) -> dict:
    init_payload = {**release_payload(), **payload}
    if activate:
        init_payload["is_active"] = True
        init_payload["release_status"] = "active"
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    if activate:
                        cur.execute("update production_release_registry set is_active = false", prepare=False)
                    cur.execute(
                        """
                        insert into production_release_registry (
                            release_name, phase, release_version, git_commit_hash, git_commit_short,
                            git_branch, git_commit_message, deployed_at, deployment_provider,
                            deployment_instance_id, production_generation, production_start_issue,
                            first_live_based_on_issue, first_live_prediction_issue,
                            last_known_draw_issue_at_release, model_version, feature_version,
                            learning_engine_version, observation_version, rule_library_version,
                            dashboard_version, database_schema_version, environment_fingerprint,
                            release_status, is_active, is_rollback_candidate, previous_release_id,
                            release_notes
                        )
                        values (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb
                        )
                        on conflict (release_version) do update set
                            release_status = excluded.release_status,
                            is_active = excluded.is_active,
                            updated_at = now()
                        returning id
                        """,
                        _release_params(init_payload),
                        prepare=False,
                    )
                    row_id = int(cur.fetchone()[0])
                conn.commit()
            return {"status": "ok", "storage": "cloud", "id": row_id}
        except Exception:
            pass
    with _sqlite_connection() as conn:
        if activate:
            conn.execute("update production_release_registry set is_active = 0")
        cursor = conn.execute(
            """
            insert into production_release_registry (
                release_name, phase, release_version, git_commit_hash, git_commit_short,
                git_branch, git_commit_message, deployed_at, deployment_provider,
                deployment_instance_id, production_generation, production_start_issue,
                first_live_based_on_issue, first_live_prediction_issue,
                last_known_draw_issue_at_release, model_version, feature_version,
                learning_engine_version, observation_version, rule_library_version,
                dashboard_version, database_schema_version, environment_fingerprint,
                release_status, is_active, is_rollback_candidate, previous_release_id,
                release_notes, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(release_version) do update set
                release_status = excluded.release_status,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
            """,
            (*_release_params(init_payload), _now()),
        )
    return {"status": "ok", "storage": "sqlite", "id": int(cursor.lastrowid or 0)}


def ensure_default_release() -> dict:
    payload = {
        **release_payload(),
        "production_generation": get_production_generation(),
        "production_start_issue": str(get_production_start_issue()),
        "git_commit_message": "chore: finalize v28.0.0 release metadata",
        "release_notes": [
            "Production data reset begins at issue 115040780.",
            "Legacy, test, pending, and invalid generation records are excluded from production reads.",
            "Prediction history includes release, commit, model, feature, and generation traceability.",
        ],
    }
    return register_release(payload, activate=True)


def list_releases(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    select_sql = """
        select id, release_name, phase, release_version, git_commit_hash, git_commit_short,
               git_branch, git_commit_message, deployed_at, deployment_provider,
               deployment_instance_id, production_generation, production_start_issue,
               first_live_based_on_issue, first_live_prediction_issue,
               last_known_draw_issue_at_release, model_version, feature_version,
               learning_engine_version, observation_version, rule_library_version,
               dashboard_version, database_schema_version, environment_fingerprint,
               release_status, is_active, is_rollback_candidate, previous_release_id,
               release_notes, created_at, updated_at
        from production_release_registry
        order by is_active desc, created_at desc, id desc
        limit {placeholder}
    """
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(select_sql.format(placeholder="%s"), (limit,), prepare=False)
                    rows = cur.fetchall()
            return [_row_to_release(row) for row in rows]
        except Exception:
            pass
    try:
        with _sqlite_connection() as conn:
            return [_row_to_release(row) for row in conn.execute(select_sql.format(placeholder="?"), (limit,)).fetchall()]
    except sqlite3.OperationalError:
        return []


def get_current_release() -> dict:
    rows = list_releases(100)
    current = next((item for item in rows if item.get("is_active")), None)
    if current:
        return current
    payload = release_payload()
    return {
        **payload,
        "production_generation": get_production_generation(),
        "production_start_issue": str(get_production_start_issue()),
        "release_status": "runtime_fallback",
        "is_active": True,
        "release_notes": [],
    }


def get_release_by_version(release_version: str) -> dict | None:
    target = str(release_version or "").strip()
    return next((item for item in list_releases(200) if item.get("release_version") == target), None)


def activate_release(release_version: str) -> dict:
    release = get_release_by_version(release_version)
    if not release:
        return {"status": "not_found", "release_version": release_version}
    with _sqlite_connection() as conn:
        conn.execute("update production_release_registry set is_active = 0")
        conn.execute(
            """
            update production_release_registry
            set is_active = 1, release_status = 'active', updated_at = ?
            where release_version = ?
            """,
            (_now(), release_version),
        )
    return {"status": "ok", "release_version": release_version}


def get_release_for_issue(issue: str) -> dict:
    return {
        "status": "ok",
        "issue": str(issue),
        "release": get_current_release(),
        "resolution": "current_active_release",
    }


def rollback_readiness(release_version: str) -> dict:
    release = get_release_by_version(release_version)
    if not release:
        return {"status": "blocked", "ready": False, "reason": "release_not_found"}
    warnings = []
    if release.get("git_commit_hash") in (None, ""):
        warnings.append("git_commit_hash_missing")
    elif release.get("git_commit_hash") == LEGACY_PLACEHOLDER_COMMIT_HASH:
        warnings.append("git_commit_hash_legacy_placeholder")
    if int(release.get("production_generation") or 0) != get_production_generation():
        warnings.append("generation_mismatch")
    status = "warning" if warnings else "ready"
    return {
        "status": status,
        "ready": not warnings,
        "warnings": warnings,
        "release": release,
    }

