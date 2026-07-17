from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any

from database.learning_store import get_learned_live_target_count
from database.prediction_history_store import (
    _json_loads,
    _normalize_numbers,
    _query_with_fallback,
    get_latest_prediction_history,
    get_prediction_lifecycle_aggregates,
)
from services.collector_runtime import collector_runtime_status
from services.prediction_lifecycle_repair import dry_run_all, official_draw_time_investigation

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
ACTIVE_WINDOW_START_HOUR = 7
ACTIVE_WINDOW_END_HOUR = 24
PREDICTION_INTERVAL_MINUTES = 5
EXPECTED_PREDICTIONS_PER_DAY = int(
    ((ACTIVE_WINDOW_END_HOUR - ACTIVE_WINDOW_START_HOUR) * 60) / PREDICTION_INTERVAL_MINUTES
)
RECOVERY_DRY_RUN_HEALTH_TTL_SECONDS = 60
_RECOVERY_DRY_RUN_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0}


def _today_taipei() -> str:
    return datetime.now(TAIPEI_TZ).date().isoformat()


def _expected_so_far(now: datetime | None = None) -> int:
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    start = current.replace(hour=ACTIVE_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    end = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if current <= start:
        return 0
    active_until = min(current, end)
    elapsed_minutes = max(0, int((active_until - start).total_seconds() // 60))
    return min(EXPECTED_PREDICTIONS_PER_DAY, elapsed_minutes // PREDICTION_INTERVAL_MINUTES)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except Exception:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percent(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round((part / total) * 100, 2)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return round(ordered[index], 2)


def _duration_minutes(start: Any, end: Any) -> float | None:
    started = _parse_datetime(start)
    ended = _parse_datetime(end)
    if not started or not ended:
        return None
    return round(max(0.0, (ended - started).total_seconds() / 60), 2)


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


def _today_clause(column: str) -> tuple[str, str, tuple[str]]:
    today = _today_taipei()
    return (
        f"date({column} at time zone 'Asia/Taipei') = %s",
        f"date({column}) = ?",
        (today,),
    )


def _query(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    return _query_with_fallback(sql, params, sqlite_sql=sqlite_sql)


def prediction_pipeline_validation() -> dict:
    latest = get_latest_prediction_history()
    if not latest:
        return {"status": "warning", "message": "no live prediction history found"}
    based_on = _valid_issue(latest.get("issue"))
    target = _valid_issue(latest.get("prediction_issue"))
    numbers = _normalize_numbers(latest.get("recommend_numbers"))
    target_matches = False
    try:
        target_matches = bool(based_on and target and int(target) == int(based_on) + 1)
    except Exception:
        target_matches = False
    numbers_valid = bool(numbers and len(numbers) == len(set(numbers)) and all(1 <= n <= 80 for n in numbers))
    status_ok = latest.get("prediction_status") in ("waiting_draw", "verified")
    checks = {
        "based_on_issue_valid": bool(based_on),
        "prediction_issue_valid": bool(target),
        "prediction_issue_matches_next_issue": target_matches,
        "prediction_issue_not_null": target is not None,
        "recommend_numbers_valid": numbers_valid,
        "recommend_numbers_count": len(numbers),
        "recommend_numbers_has_20": len(numbers) == 20,
        "prediction_status": latest.get("prediction_status"),
        "prediction_status_expected_for_live": latest.get("prediction_status") == "waiting_draw",
        "prediction_status_acceptable": status_ok,
    }
    hard_failures = [
        name for name in (
            "based_on_issue_valid",
            "prediction_issue_valid",
            "prediction_issue_matches_next_issue",
            "prediction_issue_not_null",
            "recommend_numbers_valid",
            "prediction_status_acceptable",
        )
        if not checks.get(name)
    ]
    return {
        "status": "ok" if not hard_failures else "warning",
        "latest": {
            "based_on_issue": based_on,
            "prediction_issue": target,
            "created_at": latest.get("created_at"),
            "updated_at": latest.get("updated_at"),
            "record_id": latest.get("id"),
            "read_layer": latest.get("read_layer"),
        },
        "checks": checks,
        "warnings": hard_failures,
    }


def prediction_coverage(today: str | None = None) -> dict:
    target_date = today or _today_taipei()
    rows = _query(
        """
        select prediction_issue
        from prediction_history
        where date(created_at at time zone 'Asia/Taipei') = %s
          and prediction_issue is not null
          and prediction_issue not like '99%%'
          and upper(prediction_issue) not like 'TEST%%'
        order by prediction_issue
        """,
        (target_date,),
        sqlite_sql="""
        select prediction_issue
        from prediction_history
        where date(created_at) = ?
          and prediction_issue is not null
          and prediction_issue not like '99%'
          and upper(prediction_issue) not like 'TEST%'
        order by prediction_issue
        """,
    )
    issues = sorted({int(row[0]) for row in rows if row and _valid_issue(row[0])})
    missing_between_observed: list[str] = []
    if issues:
        issue_set = set(issues)
        for issue in range(min(issues), max(issues) + 1):
            if issue not in issue_set:
                missing_between_observed.append(str(issue))
    created = len(issues)
    missing_count = max(EXPECTED_PREDICTIONS_PER_DAY - created, 0)
    expected_so_far = _expected_so_far()
    return {
        "date": target_date,
        "active_window": {
            "start": "07:00",
            "end": "24:00",
            "interval_minutes": PREDICTION_INTERVAL_MINUTES,
        },
        "prediction_expected_today": EXPECTED_PREDICTIONS_PER_DAY,
        "schedule_expected_count": expected_so_far,
        "prediction_created_today": created,
        "prediction_coverage": _percent(created, EXPECTED_PREDICTIONS_PER_DAY),
        "schedule_coverage": _percent(created, expected_so_far) if expected_so_far else 100.0,
        "missing_prediction_count": missing_count,
        "missing_prediction_so_far": max(expected_so_far - created, 0),
        "missing_target_issues": missing_between_observed[:50],
        "observed_first_target_issue": str(issues[0]) if issues else None,
        "observed_last_target_issue": str(issues[-1]) if issues else None,
    }


def lifecycle_pending_counts() -> dict:
    verification_rows = _query(
        """
        select count(*)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and jsonb_typeof(o.numbers) = 'array'
          and jsonb_array_length(o.numbers) = 20
          and not (
            p.prediction_status = 'verified'
            and p.verified_at is not null
            and jsonb_typeof(p.winning_numbers) = 'array'
            and jsonb_array_length(p.winning_numbers) = 20
            and jsonb_typeof(p.matched_numbers) = 'array'
            and jsonb_typeof(p.missed_numbers) = 'array'
          )
        """,
        sqlite_sql="""
        select count(*)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and o.numbers is not null
          and o.numbers not in ('', '[]')
          and not (
            p.prediction_status = 'verified'
            and p.verified_at is not null
            and p.winning_numbers is not null
            and p.winning_numbers not in ('', '[]')
            and p.matched_numbers is not null
            and p.missed_numbers is not null
          )
        """,
    )
    learning_rows = _query(
        """
        select count(*)
        from prediction_history
        where prediction_status = 'verified'
          and verified_at is not null
          and coalesce(learning_used, false) = false
        """,
        sqlite_sql="""
        select count(*)
        from prediction_history
        where prediction_status = 'verified'
          and verified_at is not null
          and coalesce(learning_used, 0) = 0
        """,
    )
    live_verification_rows = _query(
        """
        select count(*)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and p.prediction_status = 'waiting_draw'
          and jsonb_typeof(o.numbers) = 'array'
          and jsonb_array_length(o.numbers) = 20
        """,
        sqlite_sql="""
        select count(*)
        from prediction_history p
        join official_draw_history o on o.issue = p.prediction_issue
        where p.prediction_issue is not null
          and p.prediction_status = 'waiting_draw'
          and o.numbers is not null
          and o.numbers not in ('', '[]')
        """,
    )
    live_learning_rows = _query(
        """
        select count(*)
        from prediction_history
        where prediction_status = 'verified'
          and verified_at is not null
          and coalesce(learning_used, false) = false
          and coalesce(verification_version, '') <> 'prediction_recovery_v1'
        """,
        sqlite_sql="""
        select count(*)
        from prediction_history
        where prediction_status = 'verified'
          and verified_at is not null
          and coalesce(learning_used, 0) = 0
          and coalesce(verification_version, '') <> 'prediction_recovery_v1'
        """,
    )
    verification_pending = int(verification_rows[0][0] or 0) if verification_rows else 0
    learning_pending = int(learning_rows[0][0] or 0) if learning_rows else 0
    live_verification_pending = int(live_verification_rows[0][0] or 0) if live_verification_rows else 0
    live_learning_pending = int(live_learning_rows[0][0] or 0) if live_learning_rows else 0
    return {
        "verification_pending": verification_pending,
        "learning_pending": learning_pending,
        "live_verification_pending": live_verification_pending,
        "legacy_verification_incomplete": max(verification_pending - live_verification_pending, 0),
        "live_learning_pending": live_learning_pending,
        "legacy_learning_incomplete": max(learning_pending - live_learning_pending, 0),
    }


def target_unconfirmed_counts(today: str | None = None) -> dict:
    target_date = today or _today_taipei()
    null_rows = _query(
        """
        select count(*)
        from prediction_history
        where date(created_at at time zone 'Asia/Taipei') = %s
          and prediction_issue is null
        """,
        (target_date,),
        sqlite_sql="""
        select count(*)
        from prediction_history
        where date(created_at) = ?
          and prediction_issue is null
        """,
    )
    event_rows = _query(
        """
        select count(*)
        from operation_events
        where date(created_at at time zone 'Asia/Taipei') = %s
          and component = 'prediction'
          and (
            event_type in ('prediction_skipped', 'prediction_history_save_skipped')
            or message like '%%target_unconfirmed%%'
          )
        """,
        (target_date,),
        sqlite_sql="""
        select count(*)
        from operation_events
        where date(created_at) = ?
          and component = 'prediction'
          and (
            event_type in ('prediction_skipped', 'prediction_history_save_skipped')
            or message like '%target_unconfirmed%'
          )
        """,
    )
    return {
        "target_unconfirmed_today": int(event_rows[0][0] or 0) if event_rows else 0,
        "null_target_today": int(null_rows[0][0] or 0) if null_rows else 0,
    }


def verification_delay() -> dict:
    rows = _query(
        """
        select created_at, verified_at, prediction_issue
        from prediction_history
        where prediction_status = 'verified'
          and created_at is not null
          and verified_at is not null
        """,
    )
    durations = []
    for created_at, verified_at, _ in rows:
        duration = _duration_minutes(created_at, verified_at)
        if duration is not None:
            durations.append(duration)
    return {
        "sample_size": len(durations),
        "average_delay_minutes": round(mean(durations), 2) if durations else 0.0,
        "p95_delay_minutes": _p95(durations),
        "status": "ok" if not durations or _p95(durations) <= 30 else "warning",
    }


def learning_delay() -> dict:
    rows = _query(
        """
        select p.verified_at, min(l.learned_at), p.prediction_issue
        from prediction_history p
        join learning_history l on coalesce(l.target_issue, l.issue) = p.prediction_issue
        where p.prediction_status = 'verified'
          and p.verified_at is not null
          and l.prediction_type = 'live_prediction'
          and l.learned_status = 'learned'
          and l.learned_at is not null
        group by p.verified_at, p.prediction_issue
        """,
    )
    durations = []
    for verified_at, learned_at, _ in rows:
        duration = _duration_minutes(verified_at, learned_at)
        if duration is not None:
            durations.append(duration)
    return {
        "sample_size": len(durations),
        "average_delay_minutes": round(mean(durations), 2) if durations else 0.0,
        "p95_delay_minutes": _p95(durations),
        "status": "ok" if not durations or _p95(durations) <= 30 else "warning",
    }


def latest_pipeline_times() -> dict:
    rows = _query(
        """
        select max(created_at), max(verified_at)
        from prediction_history
        """
    )
    learning_rows = _query(
        """
        select max(learned_at)
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
        """
    )
    row = rows[0] if rows else [None, None]
    return {
        "prediction_last_created": str(row[0]) if row[0] is not None else None,
        "prediction_last_verified": str(row[1]) if row[1] is not None else None,
        "prediction_last_learning": str(learning_rows[0][0]) if learning_rows and learning_rows[0][0] is not None else None,
    }


def operation_event_health() -> dict:
    rows = _query(
        """
        select event_type, status, created_at
        from operation_events
        where component in ('prediction', 'recommendation', 'official_collector', 'learning')
        order by created_at desc
        limit 100
        """
    )
    count_rows = _query(
        """
        select
            sum(case when event_type in ('prediction_created', 'prediction_history_saved') then 1 else 0 end),
            sum(case when event_type in ('prediction_verified', 'prediction_lifecycle_recovery_apply') then 1 else 0 end),
            sum(case when event_type in ('learning_completed', 'learning_evaluation') then 1 else 0 end)
        from operation_events
        where component in ('prediction', 'recommendation', 'official_collector', 'learning')
        """
    )
    counts = count_rows[0] if count_rows else [0, 0, 0]
    expected = {
        "prediction_created": int(counts[0] or 0) > 0,
        "prediction_verified": int(counts[1] or 0) > 0,
        "learning_completed": int(counts[2] or 0) > 0,
    }
    return {
        "status": "ok" if all(expected.values()) else "warning",
        "expected_events": expected,
        "expected_event_counts": {
            "prediction_created": int(counts[0] or 0),
            "prediction_verified": int(counts[1] or 0),
            "learning_completed": int(counts[2] or 0),
        },
        "recent_event_count": len(rows),
        "recent_warning_count": sum(1 for row in rows if str(row[1]).lower() == "warning"),
        "recent_error_count": sum(1 for row in rows if str(row[1]).lower() == "error"),
    }


def scheduler_status(scheduler: Any | None = None) -> dict:
    runtime = collector_runtime_status()
    jobs = []
    if scheduler is not None:
        try:
            for job in scheduler.get_jobs():
                jobs.append(
                    {
                        "scheduler_name": getattr(job, "id", None),
                        "interval": str(getattr(job, "trigger", "")),
                        "cron": str(getattr(job, "trigger", "")) if "cron" in str(getattr(job, "trigger", "")).lower() else None,
                        "last_run": None,
                        "next_run": str(getattr(job, "next_run_time", None)) if getattr(job, "next_run_time", None) else None,
                        "average_duration_ms": None,
                        "success_count": runtime.get("scheduler_success_count", 0),
                        "failure_count": runtime.get("scheduler_error_count", 0),
                        "skipped_count": runtime.get("scheduler_skipped_count", 0),
                    }
                )
        except Exception:
            logger.exception("scheduler status inspection failed")
    prediction_jobs = [
        job for job in jobs
        if "prediction" in str(job.get("scheduler_name") or "").lower()
    ]
    return {
        "status": "running" if scheduler is not None and getattr(scheduler, "running", False) else "unknown",
        "jobs": jobs,
        "prediction_job_registered": bool(prediction_jobs),
        "prediction_job_id": prediction_jobs[0].get("scheduler_name") if prediction_jobs else None,
        "prediction_job_next_run": prediction_jobs[0].get("next_run") if prediction_jobs else None,
        "prediction_job_last_run": None,
        "prediction_job_last_status": (runtime.get("last_scheduler_event") or {}).get("type"),
        "prediction_job_last_error": runtime.get("last_scheduler_error"),
        "runtime": {
            "last_scheduler_event": runtime.get("last_scheduler_event"),
            "success_count": runtime.get("scheduler_success_count", 0),
            "failure_count": runtime.get("scheduler_error_count", 0),
            "skipped_count": runtime.get("scheduler_skipped_count", 0),
            "missed_count": runtime.get("scheduler_missed_count", 0),
        },
    }


def prediction_trigger_event_counts() -> dict:
    today_sql, today_sqlite, params = _today_clause("created_at")
    rows = _query(
        f"""
        select
            sum(case when event_type in (
                'next_prediction_trigger_started',
                'ensure_next_prediction_started',
                'refresh_next_prediction_started'
            ) then 1 else 0 end),
            sum(case when event_type = 'prediction_service_called' then 1 else 0 end),
            sum(case when event_type = 'prediction_create_started' then 1 else 0 end),
            sum(case when event_type = 'prediction_created' then 1 else 0 end),
            sum(case when event_type = 'prediction_skipped' then 1 else 0 end)
        from operation_events
        where {today_sql}
        """,
        params,
        sqlite_sql=f"""
        select
            sum(case when event_type in (
                'next_prediction_trigger_started',
                'ensure_next_prediction_started',
                'refresh_next_prediction_started'
            ) then 1 else 0 end),
            sum(case when event_type = 'prediction_service_called' then 1 else 0 end),
            sum(case when event_type = 'prediction_create_started' then 1 else 0 end),
            sum(case when event_type = 'prediction_created' then 1 else 0 end),
            sum(case when event_type = 'prediction_skipped' then 1 else 0 end)
        from operation_events
        where {today_sqlite}
        """,
    )
    row = rows[0] if rows else [0, 0, 0, 0, 0]
    return {
        "prediction_trigger_count_today": int(row[0] or 0),
        "prediction_service_call_count_today": int(row[1] or 0),
        "prediction_create_started_count_today": int(row[2] or 0),
        "prediction_created_count_today": int(row[3] or 0),
        "prediction_skipped_count_today": int(row[4] or 0),
    }


def recovery_dry_run_health() -> dict:
    cached = _RECOVERY_DRY_RUN_CACHE.get("payload")
    if isinstance(cached, dict) and time.monotonic() < float(_RECOVERY_DRY_RUN_CACHE.get("expires_at") or 0):
        payload = dict(cached)
        payload["cache_source"] = "memory"
        return payload
    if os.getenv("PIPELINE_HEALTH_INLINE_DRY_RUN", "").lower() not in ("1", "true", "yes"):
        return {
            "status": "skipped",
            "reason": "request_time_dry_run_disabled",
            "verification": {"would_verify": None},
            "learning_sync": {"would_sync": None},
            "cache_source": "not_run",
        }
    payload = dry_run_all()
    if isinstance(payload, dict):
        _RECOVERY_DRY_RUN_CACHE["payload"] = dict(payload)
        _RECOVERY_DRY_RUN_CACHE["expires_at"] = time.monotonic() + RECOVERY_DRY_RUN_HEALTH_TTL_SECONDS
    return payload


def pipeline_alerts(coverage: dict, pending: dict, target_counts: dict, recovery: dict) -> list[dict]:
    alerts = []
    schedule_coverage = float(coverage.get("schedule_coverage") or 0)
    if coverage.get("schedule_expected_count", 0) > 0 and schedule_coverage < 80:
        alerts.append({"type": "missing_prediction", "severity": "critical", "message": "schedule coverage below 80%"})
    elif coverage.get("schedule_expected_count", 0) > 0 and schedule_coverage < 95:
        alerts.append({"type": "missing_prediction", "severity": "warning", "message": "schedule coverage below 95%"})
    if target_counts.get("target_unconfirmed_today", 0) > 0:
        alerts.append({"type": "target_unconfirmed", "severity": "warning", "message": "target unconfirmed events detected today"})
    if target_counts.get("null_target_today", 0) > 0:
        alerts.append({"type": "null_target", "severity": "warning", "message": "null target predictions detected today"})
    if pending.get("verification_pending", 0) > 0:
        alerts.append({"type": "verification_pending", "severity": "warning", "message": "verified lifecycle has pending official results"})
    if pending.get("learning_pending", 0) > 0:
        alerts.append({"type": "learning_pending", "severity": "warning", "message": "verified predictions are waiting for learning sync"})
    verification_recovery = (recovery.get("verification") or {}).get("would_verify", 0)
    learning_recovery = (recovery.get("learning_sync") or {}).get("would_sync", 0)
    if verification_recovery or learning_recovery:
        alerts.append({"type": "recovery_pending", "severity": "critical", "message": "recovery dry-run is not clean"})
    return alerts


def _status_from_alerts(alerts: list[dict]) -> str:
    severities = {alert.get("severity") for alert in alerts}
    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "warning"
    return "healthy"


def _safe_error_code(name: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in str(name).lower()).strip("_")
    return f"{normalized or 'component'}_unavailable"


def _safe_component(name: str, loader, fallback: Any) -> tuple[Any, dict]:
    started = time.monotonic()
    try:
        value = loader()
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        return value, {"status": "ok", "duration_ms": duration_ms}
    except Exception as exc:
        logger.exception("pipeline health component failed: %s", name)
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        return fallback, {
            "status": "unavailable",
            "duration_ms": duration_ms,
            "error_code": _safe_error_code(name),
            "error_type": exc.__class__.__name__,
        }


def build_pipeline_health(scheduler: Any | None = None) -> dict:
    health_started = time.monotonic()
    component_status: dict[str, dict] = {}

    def component(name: str, loader, fallback: Any) -> Any:
        value, status = _safe_component(name, loader, fallback)
        component_status[name] = status
        return value

    coverage = component("coverage", prediction_coverage, {
        "date": _today_taipei(),
        "prediction_expected_today": EXPECTED_PREDICTIONS_PER_DAY,
        "schedule_expected_count": _expected_so_far(),
        "prediction_created_today": 0,
        "prediction_coverage": 0.0,
        "schedule_coverage": 0.0,
        "missing_prediction_count": 0,
        "missing_prediction_so_far": 0,
        "missing_target_issues": [],
    })
    pending = component("pending", lifecycle_pending_counts, {
        "verification_pending": 0,
        "learning_pending": 0,
        "live_verification_pending": 0,
        "legacy_verification_incomplete": 0,
        "live_learning_pending": 0,
        "legacy_learning_incomplete": 0,
    })
    target_counts = component("target_counts", target_unconfirmed_counts, {
        "target_unconfirmed_today": 0,
        "null_target_today": 0,
    })
    recovery = component("recovery_dry_run", recovery_dry_run_health, {
        "verification": {"would_verify": None},
        "learning_sync": {"would_sync": None},
        "status": "unavailable",
    })
    alerts = pipeline_alerts(coverage, pending, target_counts, recovery)
    aggregates = component("lifecycle_aggregates", get_prediction_lifecycle_aggregates, {})
    times = component("latest_pipeline_times", latest_pipeline_times, {})
    scheduler_payload = component("scheduler", lambda: scheduler_status(scheduler), {
        "status": "unavailable",
        "jobs": [],
        "prediction_job_registered": False,
        "runtime": {},
    })
    validation = component("latest_prediction", prediction_pipeline_validation, {
        "status": "warning",
        "message": "latest prediction validation unavailable",
    })
    event_health = component("operation_events", operation_event_health, {"status": "unavailable"})
    official_time = component("official_draw_time", official_draw_time_investigation, {"status": "unavailable"})
    trigger_counts = component("operation_counters", prediction_trigger_event_counts, {
        "prediction_trigger_count_today": 0,
        "prediction_service_call_count_today": 0,
        "prediction_create_started_count_today": 0,
        "prediction_created_count_today": 0,
        "prediction_skipped_count_today": 0,
    })
    verification_delay_payload = component("verification_delay", verification_delay, {
        "sample_size": 0,
        "average_delay_minutes": 0.0,
        "p95_delay_minutes": 0.0,
        "status": "unavailable",
    })
    learning_delay_payload = component("learning_delay", learning_delay, {
        "sample_size": 0,
        "average_delay_minutes": 0.0,
        "p95_delay_minutes": 0.0,
        "status": "unavailable",
    })
    learned_count = component("learning_sample_count", get_learned_live_target_count, None)
    failed_components = [
        name for name, status in component_status.items()
        if status.get("status") != "ok"
    ]
    if failed_components:
        alerts.append({
            "type": "health_component_unavailable",
            "severity": "warning",
            "message": "one or more health components are unavailable",
            "components": failed_components,
        })
    pipeline_status = _status_from_alerts(alerts)
    response_status = "partial" if failed_components else "ok"
    payload = {
        "status": response_status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_ms": None,
        "pipeline_status": "warning" if response_status == "partial" and pipeline_status == "healthy" else pipeline_status,
        **coverage,
        **times,
        **pending,
        **target_counts,
        "prediction_scheduler_status": scheduler_payload.get("status"),
        **trigger_counts,
        "prediction_validation": validation,
        "verification_delay": verification_delay_payload,
        "learning_delay": learning_delay_payload,
        "dashboard_statistics": {
            "prediction_count": aggregates.get("total_prediction_count"),
            "valid_prediction_count": aggregates.get("valid_prediction_count"),
            "verified_prediction_count": aggregates.get("completed_verified_count"),
            "learning_sample_count": learned_count,
            "null_target_count": aggregates.get("null_target_count"),
            "has_official_result_count": aggregates.get("has_official_result_count"),
        },
        "operation_events": event_health,
        "scheduler": scheduler_payload,
        "components": component_status,
        "alerts": alerts,
        "recovery_dry_run": {
            "verification_would_verify": (recovery.get("verification") or {}).get("would_verify", 0),
            "learning_would_sync": (recovery.get("learning_sync") or {}).get("would_sync", 0),
            "clean": (
                (recovery.get("verification") or {}).get("would_verify", 0) == 0
                and (recovery.get("learning_sync") or {}).get("would_sync", 0) == 0
            ),
        },
        "official_draw_time": official_time,
    }
    payload["total_duration_ms"] = round((time.monotonic() - health_started) * 1000, 2)
    json.dumps(payload, allow_nan=False, default=str)
    return payload
