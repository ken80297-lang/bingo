from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data" / "bingo.db"

DATABASE_TABLES = {
    "kuaishou_snapshots": {"issue": "issue", "updated": "updated_at"},
    "analysis_history": {"issue": "issue", "updated": "updated_at"},
    "laowanjia_features": {"issue": "issue", "updated": "updated_at"},
    "simulation_runs": {"issue": "source_issue", "updated": "created_at"},
    "recommendation_runs": {"issue": "issue", "updated": "created_at"},
    "prediction_runs": {"issue": "issue", "updated": "created_at"},
    "prediction_results": {"issue": None, "updated": "created_at"},
    "prediction_history": {"issue": "prediction_issue", "updated": "updated_at"},
    "learning_history": {"issue": "issue", "updated": "updated_at"},
    "strategy_versions": {"issue": None, "updated": "created_at"},
    "official_draw_history": {"issue": "issue", "updated": "updated_at"},
    "draw_verification": {"issue": "issue", "updated": "updated_at"},
}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _today() -> str:
    return date.today().isoformat()


def _cloud_connection():
    from database import get_connection

    return get_connection()


def _cloud_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL") or os.getenv("DATABASE_TYPE") == "postgres")


def _sqlite_connection() -> sqlite3.Connection:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(SQLITE_PATH, check_same_thread=False)


def init_operations_tables() -> dict:
    results = {"cloud": "unknown", "sqlite": "unknown"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists operation_events (
                            id bigserial primary key,
                            issue text,
                            component text,
                            event_type text,
                            status text,
                            message text,
                            duration_ms double precision,
                            created_at timestamptz default now()
                        )
                        """
                    )
                    cur.execute(
                        """
                        create table if not exists operation_errors (
                            id bigserial primary key,
                            component text,
                            issue text,
                            error_type text,
                            error_message text,
                            retry_count integer default 0,
                            resolved boolean default false,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now()
                        )
                        """
                    )
                    cur.execute(
                        """
                        create table if not exists operation_metrics (
                            id bigserial primary key,
                            metric_date date,
                            component text,
                            total_runs integer default 0,
                            success_count integer default 0,
                            error_count integer default 0,
                            average_duration_ms double precision default 0,
                            latest_issue text,
                            created_at timestamptz default now(),
                            updated_at timestamptz default now(),
                            unique(metric_date, component)
                        )
                        """
                    )
                conn.commit()
            results["cloud"] = "available"
        except Exception:
            logger.exception("failed to initialize cloud operations tables")

    try:
        with _sqlite_connection() as conn:
            conn.execute(
                """
                create table if not exists operation_events (
                    id integer primary key autoincrement,
                    issue text,
                    component text,
                    event_type text,
                    status text,
                    message text,
                    duration_ms real,
                    created_at text default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists operation_errors (
                    id integer primary key autoincrement,
                    component text,
                    issue text,
                    error_type text,
                    error_message text,
                    retry_count integer default 0,
                    resolved integer default 0,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp
                )
                """
            )
            conn.execute(
                """
                create table if not exists operation_metrics (
                    id integer primary key autoincrement,
                    metric_date text,
                    component text,
                    total_runs integer default 0,
                    success_count integer default 0,
                    error_count integer default 0,
                    average_duration_ms real default 0,
                    latest_issue text,
                    created_at text default current_timestamp,
                    updated_at text default current_timestamp,
                    unique(metric_date, component)
                )
                """
            )
        results["sqlite"] = "available"
    except Exception:
        logger.exception("failed to initialize sqlite operations tables")

    return results


def _event_tuple(event: dict) -> tuple:
    return (
        event.get("issue"),
        event.get("component"),
        event.get("event_type", "pipeline_stage"),
        event.get("status", "unknown"),
        event.get("message"),
        event.get("duration_ms"),
    )


def _metric_values(existing: tuple | None, event: dict) -> tuple[int, int, int, float]:
    duration = float(event.get("duration_ms") or 0)
    status = str(event.get("status") or "unknown").lower()
    if existing:
        total_runs, success_count, error_count, average_duration_ms = existing
    else:
        total_runs, success_count, error_count, average_duration_ms = 0, 0, 0, 0.0

    new_total = int(total_runs or 0) + 1
    new_success = int(success_count or 0) + (0 if status == "error" else 1)
    new_error = int(error_count or 0) + (1 if status == "error" else 0)
    previous_total = int(total_runs or 0)
    new_average = ((float(average_duration_ms or 0) * previous_total) + duration) / new_total
    return new_total, new_success, new_error, round(new_average, 2)


def _save_event_cloud(event: dict) -> int:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into operation_events
                (issue, component, event_type, status, message, duration_ms)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                _event_tuple(event),
            )
            event_id = int(cur.fetchone()[0])
            _update_error_state_cloud(cur, event)
            _update_metrics_cloud(cur, event)
        conn.commit()
    return event_id


def _save_event_sqlite(event: dict) -> int:
    with _sqlite_connection() as conn:
        cursor = conn.execute(
            """
            insert into operation_events
            (issue, component, event_type, status, message, duration_ms, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (*_event_tuple(event), _now()),
        )
        _update_error_state_sqlite(conn, event)
        _update_metrics_sqlite(conn, event)
        return int(cursor.lastrowid)


def _update_error_state_cloud(cur: Any, event: dict) -> None:
    component = event.get("component")
    status = str(event.get("status") or "unknown").lower()
    if not component:
        return

    if status == "error":
        cur.execute(
            """
            insert into operation_errors
            (component, issue, error_type, error_message, retry_count, resolved, updated_at)
            values (%s, %s, %s, %s, 0, false, now())
            """,
            (
                component,
                event.get("issue"),
                event.get("error_type") or event.get("event_type") or "error",
                event.get("error_message") or event.get("message"),
            ),
        )
        return

    if status in ("ok", "warning"):
        issue = event.get("issue")
        if issue:
            cur.execute(
                """
                update operation_errors
                set resolved = true, updated_at = now()
                where component = %s
                  and resolved = false
                  and (issue is null or issue <= %s)
                """,
                (component, str(issue)),
            )
        else:
            cur.execute(
                """
                update operation_errors
                set resolved = true, updated_at = now()
                where component = %s and resolved = false
                """,
                (component,),
            )


def _update_error_state_sqlite(conn: sqlite3.Connection, event: dict) -> None:
    component = event.get("component")
    status = str(event.get("status") or "unknown").lower()
    if not component:
        return

    if status == "error":
        conn.execute(
            """
            insert into operation_errors
            (component, issue, error_type, error_message, retry_count, resolved, created_at, updated_at)
            values (?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                component,
                event.get("issue"),
                event.get("error_type") or event.get("event_type") or "error",
                event.get("error_message") or event.get("message"),
                _now(),
                _now(),
            ),
        )
        return

    if status in ("ok", "warning"):
        issue = event.get("issue")
        if issue:
            conn.execute(
                """
                update operation_errors
                set resolved = 1, updated_at = ?
                where component = ?
                  and resolved = 0
                  and (issue is null or issue <= ?)
                """,
                (_now(), component, str(issue)),
            )
        else:
            conn.execute(
                """
                update operation_errors
                set resolved = 1, updated_at = ?
                where component = ? and resolved = 0
                """,
                (_now(), component),
            )


def _update_metrics_cloud(cur: Any, event: dict) -> None:
    metric_date = _today()
    component = event.get("component")
    if not component:
        return

    cur.execute(
        """
        select total_runs, success_count, error_count, average_duration_ms
        from operation_metrics
        where metric_date = %s and component = %s
        """,
        (metric_date, component),
    )
    values = _metric_values(cur.fetchone(), event)
    cur.execute(
        """
        insert into operation_metrics
        (
            metric_date, component, total_runs, success_count, error_count,
            average_duration_ms, latest_issue, updated_at
        )
        values (%s, %s, %s, %s, %s, %s, %s, now())
        on conflict (metric_date, component) do update set
            total_runs = excluded.total_runs,
            success_count = excluded.success_count,
            error_count = excluded.error_count,
            average_duration_ms = excluded.average_duration_ms,
            latest_issue = excluded.latest_issue,
            updated_at = now()
        """,
        (metric_date, component, *values, event.get("issue")),
    )


def _update_metrics_sqlite(conn: sqlite3.Connection, event: dict) -> None:
    metric_date = _today()
    component = event.get("component")
    if not component:
        return

    row = conn.execute(
        """
        select total_runs, success_count, error_count, average_duration_ms
        from operation_metrics
        where metric_date = ? and component = ?
        """,
        (metric_date, component),
    ).fetchone()
    values = _metric_values(row, event)
    conn.execute(
        """
        insert into operation_metrics
        (
            metric_date, component, total_runs, success_count, error_count,
            average_duration_ms, latest_issue, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(metric_date, component) do update set
            total_runs = excluded.total_runs,
            success_count = excluded.success_count,
            error_count = excluded.error_count,
            average_duration_ms = excluded.average_duration_ms,
            latest_issue = excluded.latest_issue,
            updated_at = excluded.updated_at
        """,
        (metric_date, component, *values, event.get("issue"), _now(), _now()),
    )


def save_operation_event(event: dict) -> dict:
    cloud_error = None
    if _cloud_enabled():
        try:
            event_id = _save_event_cloud(event)
            return {"status": "ok", "storage": "cloud", "id": event_id}
        except Exception as exc:
            logger.exception("cloud operation event save failed")
            cloud_error = str(exc)

    try:
        event_id = _save_event_sqlite(event)
        return {"status": "ok", "storage": "sqlite", "id": event_id, "cloud_error": cloud_error}
    except Exception as exc:
        logger.exception("sqlite operation event save failed")
        return {"status": "error", "storage": None, "error": str(exc)}


def _query_cloud(sql: str, params: tuple = ()) -> list[Any]:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _query_sqlite(sql: str, params: tuple = ()) -> list[Any]:
    with _sqlite_connection() as conn:
        return conn.execute(sql, params).fetchall()


def _event_from_row(row: Any) -> dict:
    return {
        "id": row[0],
        "issue": row[1],
        "component": row[2],
        "event_type": row[3],
        "status": row[4],
        "message": row[5],
        "duration_ms": row[6],
        "created_at": str(row[7]) if row[7] is not None else None,
    }


def _error_from_row(row: Any) -> dict:
    return {
        "id": row[0],
        "component": row[1],
        "issue": row[2],
        "error_type": row[3],
        "error_message": row[4],
        "retry_count": row[5],
        "resolved": bool(row[6]),
        "created_at": str(row[7]) if row[7] is not None else None,
        "updated_at": str(row[8]) if row[8] is not None else None,
    }


def _metric_from_row(row: Any) -> dict:
    return {
        "id": row[0],
        "metric_date": str(row[1]) if row[1] is not None else None,
        "component": row[2],
        "total_runs": row[3],
        "success_count": row[4],
        "error_count": row[5],
        "average_duration_ms": row[6],
        "latest_issue": row[7],
        "created_at": str(row[8]) if row[8] is not None else None,
        "updated_at": str(row[9]) if row[9] is not None else None,
    }


def get_operation_timeline(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    sql = """
        select id, issue, component, event_type, status, message, duration_ms, created_at
        from operation_events
        order by created_at desc, id desc
        limit %s
    """
    if _cloud_enabled():
        try:
            return [_event_from_row(row) for row in _query_cloud(sql, (limit,))]
        except Exception:
            logger.exception("cloud operation timeline query failed")

    try:
        return [
            _event_from_row(row)
            for row in _query_sqlite(sql.replace("%s", "?"), (limit,))
        ]
    except Exception:
        logger.exception("sqlite operation timeline query failed")
        return []


def get_operation_errors(limit: int = 50, unresolved_only: bool = False) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    where = "where resolved = false" if unresolved_only else ""
    sql = f"""
        select id, component, issue, error_type, error_message, retry_count,
               resolved, created_at, updated_at
        from operation_errors
        {where}
        order by created_at desc, id desc
        limit %s
    """
    if _cloud_enabled():
        try:
            return [_error_from_row(row) for row in _query_cloud(sql, (limit,))]
        except Exception:
            logger.exception("cloud operation errors query failed")

    try:
        sqlite_sql = sql.replace("resolved = false", "resolved = 0").replace("%s", "?")
        return [_error_from_row(row) for row in _query_sqlite(sqlite_sql, (limit,))]
    except Exception:
        logger.exception("sqlite operation errors query failed")
        return []


def get_operation_error_summary() -> dict:
    sql = """
        select
            count(*),
            sum(case when resolved = false then 1 else 0 end),
            sum(case when resolved = true then 1 else 0 end)
        from operation_errors
    """
    by_component_sql = """
        select component, count(*)
        from operation_errors
        where resolved = false
        group by component
        order by count(*) desc, component
    """
    if _cloud_enabled():
        try:
            row = _query_cloud(sql)[0]
            component_rows = _query_cloud(by_component_sql)
            return {
                "total": int(row[0] or 0),
                "unresolved": int(row[1] or 0),
                "resolved": int(row[2] or 0),
                "unresolved_by_component": {str(item[0]): int(item[1] or 0) for item in component_rows},
            }
        except Exception:
            logger.exception("cloud operation error summary query failed")

    try:
        sqlite_sql = sql.replace("resolved = false", "resolved = 0").replace("resolved = true", "resolved = 1")
        row = _query_sqlite(sqlite_sql)[0]
        component_rows = _query_sqlite(by_component_sql.replace("resolved = false", "resolved = 0"))
        return {
            "total": int(row[0] or 0),
            "unresolved": int(row[1] or 0),
            "resolved": int(row[2] or 0),
            "unresolved_by_component": {str(item[0]): int(item[1] or 0) for item in component_rows},
        }
    except Exception:
        logger.exception("sqlite operation error summary query failed")
        return {"total": 0, "unresolved": 0, "resolved": 0, "unresolved_by_component": {}}


def resolve_component_errors(component: str, issue: str | None = None) -> dict:
    if not component:
        return {"status": "error", "resolved": 0, "error": "missing component"}

    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    if issue:
                        cur.execute(
                            """
                            update operation_errors
                            set resolved = true, updated_at = now()
                            where component = %s
                              and resolved = false
                              and (issue is null or issue <= %s)
                            """,
                            (component, str(issue)),
                        )
                    else:
                        cur.execute(
                            """
                            update operation_errors
                            set resolved = true, updated_at = now()
                            where component = %s and resolved = false
                            """,
                            (component,),
                        )
                    resolved = int(cur.rowcount or 0)
                conn.commit()
            return {"status": "ok", "storage": "cloud", "resolved": resolved}
        except Exception:
            logger.exception("cloud resolve component errors failed")

    try:
        with _sqlite_connection() as conn:
            if issue:
                cursor = conn.execute(
                    """
                    update operation_errors
                    set resolved = 1, updated_at = ?
                    where component = ?
                      and resolved = 0
                      and (issue is null or issue <= ?)
                    """,
                    (_now(), component, str(issue)),
                )
            else:
                cursor = conn.execute(
                    """
                    update operation_errors
                    set resolved = 1, updated_at = ?
                    where component = ? and resolved = 0
                    """,
                    (_now(), component),
                )
            return {"status": "ok", "storage": "sqlite", "resolved": int(cursor.rowcount or 0)}
    except Exception as exc:
        logger.exception("sqlite resolve component errors failed")
        return {"status": "error", "storage": None, "resolved": 0, "error": str(exc)}


def resolve_stale_operation_errors() -> dict:
    before = get_operation_error_summary()
    checked = before.get("unresolved", 0)
    if _cloud_enabled():
        try:
            with _cloud_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        update operation_errors e
                        set resolved = true, updated_at = now()
                        where e.resolved = false
                          and exists (
                              select 1
                              from operation_events ev
                              where ev.component = e.component
                                and ev.status in ('ok', 'warning')
                                and ev.created_at > e.created_at
                                and (
                                    e.issue is null
                                    or ev.issue is null
                                    or ev.issue >= e.issue
                                )
                          )
                        """
                    )
                    resolved = int(cur.rowcount or 0)
                conn.commit()
            after = get_operation_error_summary()
            return {
                "status": "ok",
                "storage": "cloud",
                "checked": checked,
                "resolved": resolved,
                "remaining_unresolved": after.get("unresolved", 0),
                "error_summary": after,
            }
        except Exception:
            logger.exception("cloud resolve stale operation errors failed")

    try:
        with _sqlite_connection() as conn:
            cursor = conn.execute(
                """
                update operation_errors
                set resolved = 1, updated_at = ?
                where resolved = 0
                  and exists (
                      select 1
                      from operation_events ev
                      where ev.component = operation_errors.component
                        and ev.status in ('ok', 'warning')
                        and ev.created_at > operation_errors.created_at
                        and (
                            operation_errors.issue is null
                            or ev.issue is null
                            or ev.issue >= operation_errors.issue
                        )
                  )
                """,
                (_now(),),
            )
            resolved = int(cursor.rowcount or 0)
        after = get_operation_error_summary()
        return {
            "status": "ok",
            "storage": "sqlite",
            "checked": checked,
            "resolved": resolved,
            "remaining_unresolved": after.get("unresolved", 0),
            "error_summary": after,
        }
    except Exception as exc:
        logger.exception("sqlite resolve stale operation errors failed")
        return {"status": "error", "checked": checked, "resolved": 0, "remaining_unresolved": checked, "error": str(exc)}


def get_operation_metrics() -> dict:
    sql = """
        select id, metric_date, component, total_runs, success_count, error_count,
               average_duration_ms, latest_issue, created_at, updated_at
        from operation_metrics
        where metric_date = %s
        order by component
    """
    params = (_today(),)
    if _cloud_enabled():
        try:
            rows = [_metric_from_row(row) for row in _query_cloud(sql, params)]
            return {"status": "ok", "storage": "cloud", "date": _today(), "components": rows}
        except Exception:
            logger.exception("cloud operation metrics query failed")

    try:
        rows = [_metric_from_row(row) for row in _query_sqlite(sql.replace("%s", "?"), params)]
        return {"status": "ok", "storage": "sqlite", "date": _today(), "components": rows}
    except Exception as exc:
        logger.exception("sqlite operation metrics query failed")
        return {"status": "error", "storage": None, "date": _today(), "components": [], "error": str(exc)}


def _table_health_cloud(table: str, issue_column: str | None, updated_column: str) -> dict:
    with _cloud_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select coalesce(
                    (select reltuples::bigint from pg_class where oid = to_regclass(%s)),
                    0
                )
                """,
                (table,),
            )
            total = int(cur.fetchone()[0] or 0)
            latest_issue = None
            latest_update = None
            if issue_column:
                cur.execute(
                    f"""
                    select {issue_column}, {updated_column}
                    from {table}
                    order by {updated_column} desc nulls last
                    limit 1
                    """
                )
                row = cur.fetchone()
                if row:
                    latest_issue = row[0]
                    latest_update = str(row[1]) if row[1] is not None else None
            else:
                cur.execute(
                    f"""
                    select {updated_column}
                    from {table}
                    order by {updated_column} desc nulls last
                    limit 1
                    """
                )
                row = cur.fetchone()
                latest_update = str(row[0]) if row else None
    return {"status": "ok", "count": total, "latest_issue": latest_issue, "latest_update": latest_update}


def _table_health_sqlite(table: str, issue_column: str | None, updated_column: str) -> dict:
    with _sqlite_connection() as conn:
        total = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
        latest_issue = None
        latest_update = None
        if issue_column:
            row = conn.execute(
                f"""
                select {issue_column}, {updated_column}
                from {table}
                order by {updated_column} desc
                limit 1
                """
            ).fetchone()
            if row:
                latest_issue = row[0]
                latest_update = str(row[1]) if row[1] is not None else None
        else:
            row = conn.execute(
                f"""
                select {updated_column}
                from {table}
                order by {updated_column} desc
                limit 1
                """
            ).fetchone()
            latest_update = str(row[0]) if row else None
    return {"status": "ok", "count": total, "latest_issue": latest_issue, "latest_update": latest_update}


def get_database_health() -> dict:
    tables: dict[str, dict] = {}
    overall = "ok"
    storage = "unknown"

    for table, config in DATABASE_TABLES.items():
        cloud_error = None
        if _cloud_enabled():
            try:
                tables[table] = _table_health_cloud(table, config["issue"], config["updated"])
                tables[table]["storage"] = "cloud"
                storage = "cloud"
                continue
            except Exception as exc:
                logger.exception("cloud database health failed for %s", table)
                cloud_error = str(exc)

        try:
            tables[table] = _table_health_sqlite(table, config["issue"], config["updated"])
            tables[table]["storage"] = "sqlite"
            tables[table]["cloud_error"] = cloud_error
            storage = "sqlite" if storage == "unknown" else storage
        except Exception as exc:
            logger.exception("sqlite database health failed for %s", table)
            tables[table] = {"status": "error", "count": 0, "latest_issue": None, "latest_update": None, "error": str(exc)}
            overall = "warning"

    return {"status": overall, "storage": storage, "tables": tables}
