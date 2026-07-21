from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config.production_scope import production_scope_payload
from database.recovery_store import get_latest_recovery_report, save_recovery_report
from database.release_store import get_current_release
from services.collector_runtime import collector_runtime_status

DAILY_RECOVERY_ENABLED = os.getenv("DAILY_RECOVERY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
DAILY_RECOVERY_HOUR = int(os.getenv("DAILY_RECOVERY_HOUR", "0"))
DAILY_RECOVERY_MINUTE = int(os.getenv("DAILY_RECOVERY_MINUTE", "30"))
DAILY_RECOVERY_TIMEZONE = os.getenv("DAILY_RECOVERY_TIMEZONE", "Asia/Taipei")
DAILY_RECOVERY_LOOKBACK_DAYS = int(os.getenv("DAILY_RECOVERY_LOOKBACK_DAYS", "1"))
DAILY_RECOVERY_MAX_BATCH_SIZE = int(os.getenv("DAILY_RECOVERY_MAX_BATCH_SIZE", "20"))
DAILY_RECOVERY_JOB_TIME_BUDGET_SECONDS = int(os.getenv("DAILY_RECOVERY_JOB_TIME_BUDGET_SECONDS", "600"))
DAILY_RECOVERY_PER_ISSUE_RETRY = int(os.getenv("DAILY_RECOVERY_PER_ISSUE_RETRY", "1"))

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False,
    "lock_owner": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_report_id": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _taipei_window(lookback_days: int) -> dict:
    tz = ZoneInfo(DAILY_RECOVERY_TIMEZONE)
    today = datetime.now(tz).date()
    target = today - timedelta(days=max(1, lookback_days))
    return {
        "timezone": DAILY_RECOVERY_TIMEZONE,
        "target_date": target.isoformat(),
        "started_after": datetime.combine(target, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).isoformat(),
        "finished_before": datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).isoformat(),
    }


def get_recovery_status() -> dict:
    latest = get_latest_recovery_report()
    return {
        "status": "ok",
        "enabled": DAILY_RECOVERY_ENABLED,
        "schedule": {
            "hour": DAILY_RECOVERY_HOUR,
            "minute": DAILY_RECOVERY_MINUTE,
            "timezone": DAILY_RECOVERY_TIMEZONE,
            "lookback_days": DAILY_RECOVERY_LOOKBACK_DAYS,
            "max_batch_size": DAILY_RECOVERY_MAX_BATCH_SIZE,
            "time_budget_seconds": DAILY_RECOVERY_JOB_TIME_BUDGET_SECONDS,
        },
        "lock": dict(_STATE),
        "latest_report": latest,
    }


def build_health_report(report: dict | None = None) -> dict:
    runtime = collector_runtime_status()
    active_release = get_current_release()
    issues = []
    if runtime.get("collector_deadline_exceeded_count"):
        issues.append("collector_deadline_exceeded")
    if runtime.get("scheduler_skipped_count"):
        issues.append("scheduler_skipped")
    if (report or {}).get("failed_issue_count"):
        issues.append("recovery_failures")
    status = "healthy"
    if issues:
        status = "warning"
    if runtime.get("last_error"):
        status = "critical"
        issues.append("collector_last_error")
    return {
        "status": status,
        "issues": issues,
        "collector": runtime,
        "release": active_release,
        "production_scope": production_scope_payload(),
        "recovery": report or get_latest_recovery_report(),
        "generated_at": _now(),
    }


def run_daily_recovery(*, force: bool = False, lookback_days: int | None = None) -> dict:
    if not DAILY_RECOVERY_ENABLED and not force:
        return {"status": "disabled", "enabled": False}
    if not _LOCK.acquire(blocking=False):
        return {"status": "skipped", "reason": "daily_recovery_lock", "lock": dict(_STATE)}
    start = time.perf_counter()
    started_at = _now()
    _STATE.update({"running": True, "lock_owner": "daily_recovery", "last_started_at": started_at, "last_error": None})
    lookback = int(lookback_days or DAILY_RECOVERY_LOOKBACK_DAYS)
    report: dict[str, Any] = {
        "status": "running",
        "started_at": started_at,
        "lookback_days": lookback,
        "window": _taipei_window(lookback),
        "checked_issue_count": 0,
        "repaired_issue_count": 0,
        "failed_issue_count": 0,
        "steps": {},
        "historical_reconstructed": False,
        "live_prediction_created": False,
    }
    try:
        from services.catch_up_service import catch_up_missing_issues
        from services.latest_sync import process_latest_official_draw
        from services.official_verification import run_official_verification
        from services.learning_engine import backfill_learning_records, get_learning_status
        from services.collector_runtime import refresh_system_status_cache

        catch_up = catch_up_missing_issues()
        report["steps"]["production_catch_up"] = catch_up
        report["checked_issue_count"] += int(catch_up.get("catch_count") or 0)
        report["repaired_issue_count"] += int(catch_up.get("success_count") or 0)
        report["failed_issue_count"] += int(catch_up.get("failed_count") or 0)
        report["catch_up_status"] = catch_up.get("status")

        latest = process_latest_official_draw()
        report["steps"]["latest_official_sync"] = latest
        report["checked_issue_count"] += 1 if latest.get("source_issue") or latest.get("database_latest_issue") else 0
        report["repaired_issue_count"] += 1 if latest.get("database_saved") else 0
        report["analysis_status"] = "ok" if latest.get("analysis_created") else "pending"
        report["prediction_lifecycle_status"] = "ok" if latest.get("prediction_created") else "pending"

        verification = run_official_verification(limit=min(10, DAILY_RECOVERY_MAX_BATCH_SIZE))
        report["steps"]["verification"] = verification
        report["verification_status"] = verification.get("status")

        learning = backfill_learning_records(limit=min(DAILY_RECOVERY_MAX_BATCH_SIZE, 20))
        report["steps"]["learning_backfill"] = learning
        report["learning_status"] = learning.get("status")
        report["steps"]["learning_status"] = get_learning_status()

        report["steps"]["system_status_refresh"] = refresh_system_status_cache(scheduler_status="running")
        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "error"
        report["failed_issue_count"] += 1
        report["error"] = str(exc)
        _STATE["last_error"] = str(exc)
    finally:
        report["finished_at"] = _now()
        report["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        report["health"] = build_health_report(report)
        report["health_status"] = report["health"].get("status")
        saved = save_recovery_report(report)
        report["saved"] = saved
        _STATE.update(
            {
                "running": False,
                "lock_owner": None,
                "last_finished_at": report["finished_at"],
                "last_report_id": saved.get("id"),
            }
        )
        _LOCK.release()
    return report
