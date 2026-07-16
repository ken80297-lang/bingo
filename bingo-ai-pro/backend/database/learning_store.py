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


def init_learning_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists learning_history (
                            id bigserial primary key,
                            issue text,
                            source_issue text,
                            target_issue text,
                            history_cutoff_issue text,
                            prediction_created_at timestamptz,
                            draw_time text,
                            model_name text,
                            model_version text,
                            prediction_type text,
                            predicted_numbers jsonb,
                            predicted_scores jsonb,
                            model_weight jsonb,
                            official_numbers jsonb,
                            hit_numbers jsonb,
                            predicted_count integer default 0,
                            hit_count integer default 0,
                            precision_score double precision default 0,
                            official_coverage double precision default 0,
                            rank_score double precision default 0,
                            top_n integer,
                            prediction_snapshot jsonb,
                            analysis_snapshot jsonb,
                            verification_status text,
                            learned_status text,
                            learned_at timestamptz,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now(),
                            error_message text,
                            unique(issue, model_name, model_version, prediction_type, top_n)
                        )
                        """,
                        prepare=False,
                    )
                    for column, column_type in {
                        "source_issue": "text",
                        "target_issue": "text",
                        "history_cutoff_issue": "text",
                        "prediction_created_at": "timestamptz",
                        "model_weight": "jsonb",
                        "predicted_count": "integer default 0",
                        "official_coverage": "double precision default 0",
                    }.items():
                        cur.execute(
                            f"alter table learning_history add column if not exists {column} {column_type}",
                            prepare=False,
                        )
                    cur.execute(
                        """
                        create index if not exists idx_learning_history_target_live_status
                        on learning_history (target_issue, prediction_type, learned_status)
                        """,
                        prepare=False,
                    )
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            logger.exception("failed to initialize cloud learning_history table")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists learning_history (
                    id integer primary key autoincrement,
                    issue text,
                    source_issue text,
                    target_issue text,
                    history_cutoff_issue text,
                    prediction_created_at text,
                    draw_time text,
                    model_name text,
                    model_version text,
                    prediction_type text,
                    predicted_numbers text,
                    predicted_scores text,
                    model_weight text,
                    official_numbers text,
                    hit_numbers text,
                    predicted_count integer default 0,
                    hit_count integer default 0,
                    precision_score real default 0,
                    official_coverage real default 0,
                    rank_score real default 0,
                    top_n integer,
                    prediction_snapshot text,
                    analysis_snapshot text,
                    verification_status text,
                    learned_status text,
                    learned_at text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp,
                    error_message text,
                    unique(issue, model_name, model_version, prediction_type, top_n)
                )
                """
            )
            existing = {row[1] for row in conn.execute("pragma table_info(learning_history)").fetchall()}
            for column, column_type in {
                "source_issue": "text",
                "target_issue": "text",
                "history_cutoff_issue": "text",
                "prediction_created_at": "text",
                "model_weight": "text",
                "predicted_count": "integer default 0",
                "official_coverage": "real default 0",
            }.items():
                if column not in existing:
                    conn.execute(f"alter table learning_history add column {column} {column_type}")
            conn.execute(
                """
                create index if not exists idx_learning_history_target_live_status
                on learning_history (target_issue, prediction_type, learned_status)
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite learning_history table")

    return results


def _record_params(record: dict) -> tuple:
    return (
        record.get("issue"),
        record.get("source_issue"),
        record.get("target_issue"),
        record.get("history_cutoff_issue"),
        record.get("prediction_created_at"),
        record.get("draw_time"),
        record.get("model_name"),
        record.get("model_version"),
        record.get("prediction_type"),
        _json_dumps(record.get("predicted_numbers", [])),
        _json_dumps(record.get("predicted_scores", {})),
        _json_dumps(record.get("model_weight", {})),
        _json_dumps(record.get("official_numbers", [])),
        _json_dumps(record.get("hit_numbers", [])),
        int(record.get("predicted_count") or len(record.get("predicted_numbers") or [])),
        int(record.get("hit_count") or 0),
        float(record.get("precision_score") or 0),
        float(record.get("official_coverage") or 0),
        float(record.get("rank_score") or 0),
        int(record.get("top_n") or 0),
        _json_dumps(record.get("prediction_snapshot", {})),
        _json_dumps(record.get("analysis_snapshot", {})),
        record.get("verification_status"),
        record.get("learned_status"),
        record.get("learned_at"),
        record.get("error_message"),
    )


def upsert_learning_record(record: dict) -> dict:
    cloud_error = None
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into learning_history
                        (
                            issue, source_issue, target_issue, history_cutoff_issue,
                            prediction_created_at, draw_time, model_name, model_version, prediction_type,
                            predicted_numbers, predicted_scores, model_weight, official_numbers, hit_numbers,
                            predicted_count, hit_count, precision_score, official_coverage,
                            rank_score, top_n, prediction_snapshot,
                            analysis_snapshot, verification_status, learned_status, learned_at,
                            error_message, updated_at
                        )
                        values (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, now()
                        )
                        on conflict (issue, model_name, model_version, prediction_type, top_n)
                        do update set
                            draw_time = excluded.draw_time,
                            source_issue = excluded.source_issue,
                            target_issue = excluded.target_issue,
                            history_cutoff_issue = excluded.history_cutoff_issue,
                            prediction_created_at = coalesce(learning_history.prediction_created_at, excluded.prediction_created_at),
                            predicted_numbers = excluded.predicted_numbers,
                            predicted_scores = excluded.predicted_scores,
                            model_weight = excluded.model_weight,
                            official_numbers = excluded.official_numbers,
                            hit_numbers = excluded.hit_numbers,
                            predicted_count = excluded.predicted_count,
                            hit_count = excluded.hit_count,
                            precision_score = excluded.precision_score,
                            official_coverage = excluded.official_coverage,
                            rank_score = excluded.rank_score,
                            prediction_snapshot = excluded.prediction_snapshot,
                            analysis_snapshot = excluded.analysis_snapshot,
                            verification_status = excluded.verification_status,
                            learned_status = excluded.learned_status,
                            learned_at = excluded.learned_at,
                            error_message = excluded.error_message,
                            updated_at = now()
                        returning id
                        """,
                        _record_params(record),
                        prepare=False,
                    )
                    row_id = int(cur.fetchone()[0])
                conn.commit()
            return {"status": "ok", "storage": "cloud", "id": row_id}
        except Exception as exc:
            logger.exception("cloud learning_history upsert failed")
            cloud_error = str(exc)

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                insert into learning_history
                (
                    issue, source_issue, target_issue, history_cutoff_issue,
                    prediction_created_at, draw_time, model_name, model_version, prediction_type,
                    predicted_numbers, predicted_scores, model_weight, official_numbers, hit_numbers,
                    predicted_count, hit_count, precision_score, official_coverage,
                    rank_score, top_n, prediction_snapshot,
                    analysis_snapshot, verification_status, learned_status, learned_at,
                    error_message, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(issue, model_name, model_version, prediction_type, top_n)
                do update set
                    draw_time = excluded.draw_time,
                    source_issue = excluded.source_issue,
                    target_issue = excluded.target_issue,
                    history_cutoff_issue = excluded.history_cutoff_issue,
                    prediction_created_at = coalesce(learning_history.prediction_created_at, excluded.prediction_created_at),
                    predicted_numbers = excluded.predicted_numbers,
                    predicted_scores = excluded.predicted_scores,
                    model_weight = excluded.model_weight,
                    official_numbers = excluded.official_numbers,
                    hit_numbers = excluded.hit_numbers,
                    predicted_count = excluded.predicted_count,
                    hit_count = excluded.hit_count,
                    precision_score = excluded.precision_score,
                    official_coverage = excluded.official_coverage,
                    rank_score = excluded.rank_score,
                    prediction_snapshot = excluded.prediction_snapshot,
                    analysis_snapshot = excluded.analysis_snapshot,
                    verification_status = excluded.verification_status,
                    learned_status = excluded.learned_status,
                    learned_at = excluded.learned_at,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (*_record_params(record), _now()),
            )
            row_id = int(cursor.lastrowid or 0)
        return {"status": "ok", "storage": "sqlite", "id": row_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite learning_history upsert failed")
        return {"status": "error", "storage": None, "error": str(exc), "cloud_error": cloud_error}


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
        except Exception:
            logger.exception("cloud learning_history query failed")
    try:
        return _query_sqlite(sqlite_sql or sql.replace("%s", "?"), params)
    except Exception:
        logger.exception("sqlite learning_history query failed")
        return []


def _row_to_record(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "draw_time": row[2],
        "model_name": row[3],
        "model_version": row[4],
        "prediction_type": row[5],
        "predicted_numbers": _json_loads(row[6]) or [],
        "predicted_scores": _json_loads(row[7]) or {},
        "official_numbers": _json_loads(row[8]) or [],
        "hit_numbers": _json_loads(row[9]) or [],
        "hit_count": row[10] or 0,
        "precision_score": row[11] or 0,
        "rank_score": row[12] or 0,
        "top_n": row[13],
        "prediction_snapshot": _json_loads(row[14]) or {},
        "analysis_snapshot": _json_loads(row[15]) or {},
        "verification_status": row[16],
        "learned_status": row[17],
        "learned_at": str(row[18]) if row[18] is not None else None,
        "created_at": str(row[19]) if row[19] is not None else None,
        "updated_at": str(row[20]) if row[20] is not None else None,
        "error_message": row[21],
        "source_issue": row[22] if len(row) > 22 else None,
        "target_issue": row[23] if len(row) > 23 else None,
        "history_cutoff_issue": row[24] if len(row) > 24 else None,
        "prediction_created_at": str(row[25]) if len(row) > 25 and row[25] is not None else None,
        "model_weight": _json_loads(row[26]) if len(row) > 26 else {},
        "predicted_count": row[27] if len(row) > 27 else 0,
        "official_coverage": row[28] if len(row) > 28 else 0,
    }


def get_learning_records(
    limit: int = 100,
    offset: int = 0,
    issue: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    prediction_type: str | None = None,
    verification_status: str | None = None,
    learned_status: str | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    clauses = []
    params: list[Any] = []
    for column, value in (
        ("issue", issue),
        ("model_name", model_name),
        ("model_version", model_version),
        ("prediction_type", prediction_type),
        ("verification_status", verification_status),
        ("learned_status", learned_status),
    ):
        if value not in (None, ""):
            clauses.append(f"{column} = %s")
            params.append(str(value))
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = _query_with_fallback(
        f"""
        select id, issue, draw_time, model_name, model_version, prediction_type,
               predicted_numbers, predicted_scores, official_numbers, hit_numbers,
               hit_count, precision_score, rank_score, top_n, prediction_snapshot,
               analysis_snapshot, verification_status, learned_status, learned_at,
               created_at, updated_at, error_message, source_issue, target_issue,
               history_cutoff_issue, prediction_created_at, model_weight,
               predicted_count, official_coverage
        from learning_history
        {where}
        order by issue desc, model_name asc, top_n asc
        limit %s offset %s
        """,
        (*params, limit, offset),
        sqlite_sql=f"""
        select id, issue, draw_time, model_name, model_version, prediction_type,
               predicted_numbers, predicted_scores, official_numbers, hit_numbers,
               hit_count, precision_score, rank_score, top_n, prediction_snapshot,
               analysis_snapshot, verification_status, learned_status, learned_at,
               created_at, updated_at, error_message, source_issue, target_issue,
               history_cutoff_issue, prediction_created_at, model_weight,
               predicted_count, official_coverage
        from learning_history
        {where.replace('%s', '?')}
        order by issue desc, model_name asc, top_n asc
        limit ? offset ?
        """,
    )
    return [_row_to_record(row) for row in rows]


def get_learning_status_counts() -> dict:
    rows = _query_with_fallback(
        """
        select
            count(*) as total_records,
            sum(case when learned_status = 'learned' then 1 else 0 end) as learned_records,
            sum(case when learned_status = 'pending' then 1 else 0 end) as pending_records,
            sum(case when learned_status = 'missing_snapshot' then 1 else 0 end) as missing_snapshot_records,
            sum(case when learned_status = 'failed' then 1 else 0 end) as failed_records,
            count(distinct model_name) as model_count,
            sum(case when prediction_type = 'live_prediction' then 1 else 0 end) as live_prediction_count,
            sum(case when prediction_type = 'historical_backtest' then 1 else 0 end) as historical_backtest_count,
            max(issue) as latest_learned_issue,
            max(learned_at) as latest_learned_at
        from learning_history
        """,
    )
    if not rows:
        return {
            "total_records": 0,
            "learned_records": 0,
            "pending_records": 0,
            "missing_snapshot_records": 0,
            "failed_records": 0,
            "model_count": 0,
            "live_prediction_count": 0,
            "historical_backtest_count": 0,
            "latest_learned_issue": None,
            "latest_learned_at": None,
        }
    row = rows[0]
    return {
        "total_records": int(row[0] or 0),
        "learned_records": int(row[1] or 0),
        "pending_records": int(row[2] or 0),
        "missing_snapshot_records": int(row[3] or 0),
        "failed_records": int(row[4] or 0),
        "model_count": int(row[5] or 0),
        "live_prediction_count": int(row[6] or 0),
        "historical_backtest_count": int(row[7] or 0),
        "latest_learned_issue": row[8],
        "latest_learned_at": str(row[9]) if row[9] is not None else None,
    }


def get_learned_live_target_issues(limit: int = 1000) -> set[str]:
    limit = max(1, min(int(limit or 1000), 5000))
    rows = _query_with_fallback(
        """
        select distinct coalesce(target_issue, issue) as target
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%%'
        order by target desc
        limit %s
        """,
        (limit,),
        sqlite_sql="""
        select distinct coalesce(target_issue, issue) as target
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%'
        order by target desc
        limit ?
        """,
    )
    return {str(row[0]) for row in rows if row and row[0]}


def get_learned_live_target_count() -> int:
    rows = _query_with_fallback(
        """
        select count(distinct coalesce(target_issue, issue))
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%%'
        """,
        sqlite_sql="""
        select count(distinct coalesce(target_issue, issue))
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%'
        """,
    )
    try:
        return int(rows[0][0] or 0) if rows else 0
    except Exception:
        return 0


def get_learning_model_performance(
    model_name: str | None = None,
    window: int | str = 100,
    top_n: int | None = None,
    prediction_type: str | None = None,
) -> list[dict]:
    clauses = ["learned_status = 'learned'"]
    params: list[Any] = []
    if model_name:
        clauses.append("model_name = %s")
        params.append(model_name)
    if top_n:
        clauses.append("top_n = %s")
        params.append(int(top_n))
    if prediction_type:
        clauses.append("prediction_type = %s")
        params.append(prediction_type)
    where = " and ".join(clauses)
    limit_clause = ""
    if str(window) != "all":
        limit_clause = "limit %s"
        params.append(max(1, min(int(window or 100), 500)))

    rows = _query_with_fallback(
        f"""
        select model_name, model_version, top_n, hit_count, precision_score,
               rank_score, issue, learned_at, official_coverage
        from learning_history
        where {where}
        order by issue desc
        {limit_clause}
        """,
        tuple(params),
        sqlite_sql=f"""
        select model_name, model_version, top_n, hit_count, precision_score,
               rank_score, issue, learned_at, official_coverage
        from learning_history
        where {where.replace('%s', '?')}
        order by issue desc
        {limit_clause.replace('%s', '?')}
        """,
    )
    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        grouped.setdefault((row[0], row[1]), []).append(row)

    output = []
    for (name, version), items in grouped.items():
        hits = [float(item[3] or 0) for item in items]
        precision = [float(item[4] or 0) for item in items]
        ranks = [float(item[5] or 0) for item in items]
        coverage = [float(item[8] or 0) for item in items]
        output.append(
            {
                "model_name": name,
                "model_version": version,
                "sample_size": len(items),
                "top_n": top_n,
                "average_hits": round(sum(hits) / len(hits), 2) if hits else 0,
                "precision_score": round(sum(precision) / len(precision), 4) if precision else 0,
                "official_coverage": round(sum(coverage) / len(coverage), 4) if coverage else 0,
                "rank_score": round(sum(ranks) / len(ranks), 4) if ranks else 0,
                "latest_issue": items[0][6] if items else None,
                "latest_learned_at": str(items[0][7]) if items and items[0][7] is not None else None,
            }
        )
    return sorted(output, key=lambda item: item["rank_score"], reverse=True)
