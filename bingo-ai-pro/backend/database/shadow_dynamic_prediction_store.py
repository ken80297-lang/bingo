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
_INITIALIZED = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


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


def init_shadow_dynamic_prediction_tables() -> dict:
    global _INITIALIZED
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists shadow_dynamic_predictions (
                            id bigserial primary key,
                            based_on_issue text not null,
                            prediction_issue text not null,
                            strategy text not null,
                            recommend_numbers jsonb not null default '[]'::jsonb,
                            production_numbers jsonb not null default '[]'::jsonb,
                            rule_weights jsonb not null default '{}'::jsonb,
                            rule_metrics jsonb not null default '{}'::jsonb,
                            generated_at timestamptz not null default now(),
                            verified_at timestamptz,
                            actual_numbers jsonb,
                            hit_count integer,
                            matched_numbers jsonb,
                            production_hit_count integer,
                            delta_vs_production integer,
                            delta_vs_random_baseline double precision,
                            algorithm_version text not null,
                            git_commit text,
                            status text not null default 'pending',
                            created_at timestamptz not null default now(),
                            updated_at timestamptz not null default now()
                        )
                        """,
                        prepare=False,
                    )
                    cur.execute(
                        """
                        create unique index if not exists idx_shadow_dynamic_prediction_unique
                        on shadow_dynamic_predictions (prediction_issue, strategy, algorithm_version)
                        """,
                        prepare=False,
                    )
                    cur.execute(
                        "create index if not exists idx_shadow_dynamic_prediction_status on shadow_dynamic_predictions (status)",
                        prepare=False,
                    )
                    cur.execute(
                        "create index if not exists idx_shadow_dynamic_prediction_based_on on shadow_dynamic_predictions (based_on_issue)",
                        prepare=False,
                    )
                conn.commit()
            _INITIALIZED = True
            return {"status": "ok", "storage": "cloud"}
        except Exception as exc:
            logger.exception("cloud shadow_dynamic_predictions init failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists shadow_dynamic_predictions (
                    id integer primary key autoincrement,
                    based_on_issue text not null,
                    prediction_issue text not null,
                    strategy text not null,
                    recommend_numbers text not null default '[]',
                    production_numbers text not null default '[]',
                    rule_weights text not null default '{}',
                    rule_metrics text not null default '{}',
                    generated_at text not null,
                    verified_at text,
                    actual_numbers text,
                    hit_count integer,
                    matched_numbers text,
                    production_hit_count integer,
                    delta_vs_production integer,
                    delta_vs_random_baseline real,
                    algorithm_version text not null,
                    git_commit text,
                    status text not null default 'pending',
                    created_at text not null,
                    updated_at text not null
                )
                """
            )
            conn.execute(
                """
                create unique index if not exists idx_shadow_dynamic_prediction_unique
                on shadow_dynamic_predictions (prediction_issue, strategy, algorithm_version)
                """
            )
            conn.execute(
                "create index if not exists idx_shadow_dynamic_prediction_status on shadow_dynamic_predictions (status)"
            )
        _INITIALIZED = True
        return {"status": "ok", "storage": "sqlite", "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite shadow_dynamic_predictions init failed")
        return {"status": "error", "error": str(exc), "cloud_error": cloud_error}


def _ensure_initialized() -> None:
    if not _INITIALIZED:
        init_shadow_dynamic_prediction_tables()


def save_shadow_dynamic_prediction(item: dict) -> dict:
    _ensure_initialized()
    params = (
        str(item.get("based_on_issue") or ""),
        str(item.get("prediction_issue") or ""),
        str(item.get("strategy") or ""),
        _json_dumps(item.get("recommend_numbers") or []),
        _json_dumps(item.get("production_numbers") or []),
        _json_dumps(item.get("rule_weights") or {}),
        _json_dumps(item.get("rule_metrics") or {}),
        item.get("generated_at") or _now(),
        str(item.get("algorithm_version") or ""),
        item.get("git_commit"),
    )
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into shadow_dynamic_predictions (
                            based_on_issue, prediction_issue, strategy, recommend_numbers,
                            production_numbers, rule_weights, rule_metrics, generated_at,
                            algorithm_version, git_commit, status, updated_at
                        )
                        values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, 'pending', now())
                        on conflict (prediction_issue, strategy, algorithm_version) do update set
                            based_on_issue = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.based_on_issue else excluded.based_on_issue end,
                            recommend_numbers = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.recommend_numbers else excluded.recommend_numbers end,
                            production_numbers = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.production_numbers else excluded.production_numbers end,
                            rule_weights = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.rule_weights else excluded.rule_weights end,
                            rule_metrics = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.rule_metrics else excluded.rule_metrics end,
                            generated_at = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.generated_at else excluded.generated_at end,
                            git_commit = case
                                when shadow_dynamic_predictions.status = 'verified'
                                then shadow_dynamic_predictions.git_commit else excluded.git_commit end,
                            updated_at = now()
                        returning id, status
                        """,
                        params,
                        prepare=False,
                    )
                    row = cur.fetchone()
                conn.commit()
            return {"status": "ok", "storage": "cloud", "id": int(row[0]), "row_status": row[1]}
        except Exception as exc:
            logger.exception("cloud shadow_dynamic_predictions save failed")
            cloud_error = str(exc)

    try:
        now = _now()
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into shadow_dynamic_predictions (
                    based_on_issue, prediction_issue, strategy, recommend_numbers,
                    production_numbers, rule_weights, rule_metrics, generated_at,
                    algorithm_version, git_commit, status, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                on conflict(prediction_issue, strategy, algorithm_version) do update set
                    based_on_issue = case when status = 'verified' then based_on_issue else excluded.based_on_issue end,
                    recommend_numbers = case when status = 'verified' then recommend_numbers else excluded.recommend_numbers end,
                    production_numbers = case when status = 'verified' then production_numbers else excluded.production_numbers end,
                    rule_weights = case when status = 'verified' then rule_weights else excluded.rule_weights end,
                    rule_metrics = case when status = 'verified' then rule_metrics else excluded.rule_metrics end,
                    generated_at = case when status = 'verified' then generated_at else excluded.generated_at end,
                    git_commit = case when status = 'verified' then git_commit else excluded.git_commit end,
                    updated_at = excluded.updated_at
                """,
                (*params, now, now),
            )
        return {"status": "ok", "storage": "sqlite", "id": int(cursor.lastrowid or 0), "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite shadow_dynamic_predictions save failed")
        return {"status": "error", "error": str(exc), "cloud_error": cloud_error}


def verify_shadow_dynamic_predictions(prediction_issue: str, actual_numbers: list[int], production_numbers: list[int] | None = None) -> dict:
    _ensure_initialized()
    issue = str(prediction_issue or "")
    actual = sorted({int(n) for n in actual_numbers if 1 <= int(n) <= 80})
    supplied_production = sorted({int(n) for n in (production_numbers or []) if 1 <= int(n) <= 80})
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        update shadow_dynamic_predictions
                        set actual_numbers = %s::jsonb,
                            matched_numbers = (
                                select coalesce(jsonb_agg((value)::int order by (value)::int), '[]'::jsonb)
                                from jsonb_array_elements_text(recommend_numbers) item(value)
                                where (value)::int = any(%s)
                            ),
                            hit_count = (
                                select count(*)::int
                                from jsonb_array_elements_text(recommend_numbers) item(value)
                                where (value)::int = any(%s)
                            ),
                            production_hit_count = (
                                select count(*)::int
                                from jsonb_array_elements_text(production_numbers) item(value)
                                where (value)::int = any(%s)
                            ),
                            delta_vs_production = (
                                select count(*)::int
                                from jsonb_array_elements_text(recommend_numbers) item(value)
                                where (value)::int = any(%s)
                            ) - (
                                select count(*)::int
                                from jsonb_array_elements_text(production_numbers) item(value)
                                where (value)::int = any(%s)
                            ),
                            delta_vs_random_baseline = (
                                select count(*)::double precision
                                from jsonb_array_elements_text(recommend_numbers) item(value)
                                where (value)::int = any(%s)
                            ) - (jsonb_array_length(recommend_numbers)::double precision * 20.0 / 80.0),
                            verified_at = now(),
                            status = 'verified',
                            updated_at = now()
                        where prediction_issue = %s and status = 'pending'
                        returning id, production_hit_count
                        """,
                        (_json_dumps(actual), actual, actual, actual, actual, actual, actual, issue),
                        prepare=False,
                    )
                    updated = cur.fetchall()
                conn.commit()
            production_hits = int(updated[0][1]) if updated else (len(set(actual) & set(supplied_production)) if supplied_production else 0)
            return {"status": "ok", "storage": "cloud", "updated": len(updated), "production_hit_count": production_hits}
        except Exception as exc:
            logger.exception("cloud shadow_dynamic_predictions verify failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            rows = conn.execute(
                """
                select id, recommend_numbers, production_numbers
                from shadow_dynamic_predictions
                where prediction_issue = ? and status = 'pending'
                """,
                (issue,),
            ).fetchall()
            updated = 0
            production_hits_result = 0
            for row_id, raw_numbers, raw_production in rows:
                recommended = sorted({int(n) for n in (_json_loads(raw_numbers) or []) if 1 <= int(n) <= 80})
                production = sorted({int(n) for n in (_json_loads(raw_production) or supplied_production) if 1 <= int(n) <= 80})
                matched = sorted(set(recommended) & set(actual))
                hit_count = len(matched)
                production_hits = len(set(actual) & set(production))
                production_hits_result = production_hits
                conn.execute(
                    """
                    update shadow_dynamic_predictions
                    set actual_numbers = ?, matched_numbers = ?, hit_count = ?,
                        production_hit_count = ?, delta_vs_production = ?,
                        delta_vs_random_baseline = ?, verified_at = ?, status = 'verified',
                        updated_at = ?
                    where id = ?
                    """,
                    (
                        _json_dumps(actual),
                        _json_dumps(matched),
                        hit_count,
                        production_hits,
                        hit_count - production_hits,
                        hit_count - (len(recommended) * 20.0 / 80.0),
                        _now(),
                        _now(),
                        row_id,
                    ),
                )
                updated += 1
        return {"status": "ok", "storage": "sqlite", "updated": updated, "production_hit_count": production_hits_result, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite shadow_dynamic_predictions verify failed")
        return {"status": "error", "error": str(exc), "cloud_error": cloud_error}
