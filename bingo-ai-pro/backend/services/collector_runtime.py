from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SYSTEM_STATUS_CACHE_TTL_SECONDS = 30

_OFFICIAL_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_SYSTEM_STATUS_CACHE_LOCK = threading.RLock()
_SYSTEM_STATUS_REFRESH_LOCK = threading.Lock()

_STATE: dict[str, Any] = {
    "collector_running": False,
    "catch_up_running": False,
    "official_lock_owner": None,
    "last_collector_started_at": None,
    "last_collector_finished_at": None,
    "last_collector_duration_ms": None,
    "last_catch_up_started_at": None,
    "last_catch_up_finished_at": None,
    "last_catch_up_duration_ms": None,
    "last_catch_up_recovered_count": 0,
    "last_catch_up_failed_count": 0,
    "last_catch_up_pending_count": 0,
    "catch_up_scheduler_enabled": None,
    "catch_up_startup_job_registered": False,
    "catch_up_interval_job_registered": False,
    "last_error": None,
    "consecutive_failures": 0,
    "scheduler_skipped_count": 0,
    "skipped_due_to_lock_count": 0,
    "collector_deadline_exceeded_count": 0,
    "catch_up_deadline_exceeded_count": 0,
    "last_job_exit_reason": None,
    "last_collector_exit_reason": None,
    "last_catch_up_exit_reason": None,
    "scheduler_missed_count": 0,
    "scheduler_error_count": 0,
    "scheduler_success_count": 0,
    "last_scheduler_event": None,
    "last_scheduler_error": None,
    "last_gap_scan_at": None,
    "missing_count": 0,
    "continuity_status": "unknown",
}

_SYSTEM_STATUS_CACHE: dict[str, Any] | None = None
_SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
_SYSTEM_STATUS_LAST_REFRESH_ERROR: str | None = None
_SYSTEM_STATUS_LAST_REFRESH_DURATION_MS: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cache_age_seconds(payload: dict | None) -> float | None:
    if not payload:
        return None
    refreshed_at = _parse_time(payload.get("cache_refreshed_at") or payload.get("last_refresh_at"))
    if not refreshed_at:
        return None
    return max(0.0, (datetime.now(timezone.utc) - refreshed_at).total_seconds())


def _cache_metadata(payload: dict, *, source: str) -> dict:
    result = deepcopy(payload)
    age = _cache_age_seconds(result)
    stale = age is None or age > SYSTEM_STATUS_CACHE_TTL_SECONDS or source in {"minimal", "stale"}
    if source == "minimal":
        cache_state = "unavailable"
    elif stale:
        cache_state = "stale"
    else:
        cache_state = "fresh"
    result["cache_state"] = cache_state
    result["cache_age_seconds"] = round(age, 3) if age is not None else None
    result["stale"] = stale
    result["cache_source"] = source
    result["cache_ttl_seconds"] = SYSTEM_STATUS_CACHE_TTL_SECONDS
    result["cache_refresh_duration_ms"] = _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS
    result["last_refresh_error"] = _SYSTEM_STATUS_LAST_REFRESH_ERROR
    result["refresh_in_progress"] = _SYSTEM_STATUS_REFRESH_IN_PROGRESS
    return result


def _collector_health_from_runtime(payload: dict) -> dict:
    last_finished = payload.get("last_collector_finished_at") or payload.get("last_catch_up_finished_at")
    minutes_since_last_collect = None
    health_status = "unknown"
    reason = "尚未有收集紀錄"
    if last_finished:
        parsed = _parse_time(last_finished)
        if parsed:
            minutes_since_last_collect = round((datetime.now(timezone.utc) - parsed).total_seconds() / 60, 2)
            if minutes_since_last_collect >= 30:
                health_status = "error"
                reason = "超過 30 分鐘沒有成功收集"
            elif minutes_since_last_collect >= 15:
                health_status = "warning"
                reason = "超過 15 分鐘沒有成功收集"
            else:
                health_status = "ok"
                reason = "收集器最近有成功執行"
        else:
            reason = "無法解析最後收集時間"
    if payload.get("last_error"):
        health_status = "error"
        reason = str(payload.get("last_error"))
    return {
        "collector_health_status": health_status,
        "collector_health_reason": reason,
        "minutes_since_last_collect": minutes_since_last_collect,
    }


def collector_runtime_status() -> dict:
    with _STATE_LOCK:
        payload = deepcopy(_STATE)
    payload.update(_collector_health_from_runtime(payload))
    payload["scheduler_max_instances_skipped_count"] = payload.get("scheduler_skipped_count", 0)
    return payload


def update_collector_runtime(**kwargs: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(kwargs)


def mark_success(owner: str, duration_ms: float | None = None, **kwargs: Any) -> None:
    exit_reason = kwargs.pop("exit_reason", "completed")
    with _STATE_LOCK:
        if owner == "catch_up":
            _STATE["last_catch_up_finished_at"] = _now()
            _STATE["last_catch_up_duration_ms"] = duration_ms
            _STATE["catch_up_running"] = False
            _STATE["last_catch_up_exit_reason"] = exit_reason
        else:
            _STATE["last_collector_finished_at"] = _now()
            _STATE["last_collector_duration_ms"] = duration_ms
            _STATE["collector_running"] = False
            _STATE["last_collector_exit_reason"] = exit_reason
        _STATE["last_error"] = None
        _STATE["consecutive_failures"] = 0
        _STATE["last_job_exit_reason"] = exit_reason
        _STATE.update(kwargs)


def mark_error(owner: str, error: Exception | str, duration_ms: float | None = None) -> None:
    exit_reason = "exception"
    with _STATE_LOCK:
        if owner == "catch_up":
            _STATE["last_catch_up_finished_at"] = _now()
            _STATE["last_catch_up_duration_ms"] = duration_ms
            _STATE["catch_up_running"] = False
            _STATE["last_catch_up_exit_reason"] = exit_reason
        else:
            _STATE["last_collector_finished_at"] = _now()
            _STATE["last_collector_duration_ms"] = duration_ms
            _STATE["collector_running"] = False
            _STATE["last_collector_exit_reason"] = exit_reason
        _STATE["last_error"] = str(error)
        _STATE["consecutive_failures"] = int(_STATE.get("consecutive_failures") or 0) + 1
        _STATE["last_job_exit_reason"] = exit_reason


def mark_deadline_exceeded(owner: str) -> None:
    with _STATE_LOCK:
        if owner == "catch_up":
            _STATE["catch_up_deadline_exceeded_count"] = int(_STATE.get("catch_up_deadline_exceeded_count") or 0) + 1
        else:
            _STATE["collector_deadline_exceeded_count"] = int(_STATE.get("collector_deadline_exceeded_count") or 0) + 1


def mark_scheduler_event(event_type: str, job_id: str | None = None, error: Exception | str | None = None) -> None:
    with _STATE_LOCK:
        _STATE["last_scheduler_event"] = {"type": event_type, "job_id": job_id, "at": _now()}
        if event_type == "max_instances":
            _STATE["scheduler_skipped_count"] = int(_STATE.get("scheduler_skipped_count") or 0) + 1
        elif event_type == "missed":
            _STATE["scheduler_missed_count"] = int(_STATE.get("scheduler_missed_count") or 0) + 1
        elif event_type == "error":
            _STATE["scheduler_error_count"] = int(_STATE.get("scheduler_error_count") or 0) + 1
            _STATE["last_scheduler_error"] = str(error) if error else None
        elif event_type == "success":
            _STATE["scheduler_success_count"] = int(_STATE.get("scheduler_success_count") or 0) + 1


@contextmanager
def official_collection_lock(owner: str) -> Iterator[tuple[bool, dict]]:
    acquired = _OFFICIAL_LOCK.acquire(blocking=False)
    start = time.perf_counter()
    if not acquired:
        with _STATE_LOCK:
            _STATE["scheduler_skipped_count"] = int(_STATE.get("scheduler_skipped_count") or 0) + 1
            _STATE["skipped_due_to_lock_count"] = int(_STATE.get("skipped_due_to_lock_count") or 0) + 1
            _STATE["last_job_exit_reason"] = "skipped_due_to_lock"
            lock_owner = _STATE.get("official_lock_owner")
        yield False, {
            "status": "skipped_due_to_lock",
            "official_lock_owner": lock_owner,
            "elapsed_ms": 0,
        }
        return

    with _STATE_LOCK:
        _STATE["official_lock_owner"] = owner
        if owner == "catch_up":
            _STATE["catch_up_running"] = True
            _STATE["last_catch_up_started_at"] = _now()
        else:
            _STATE["collector_running"] = True
            _STATE["last_collector_started_at"] = _now()
    try:
        yield True, {"status": "locked", "official_lock_owner": owner}
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _STATE_LOCK:
            if owner == "catch_up":
                _STATE["catch_up_running"] = False
                _STATE["last_catch_up_duration_ms"] = duration_ms
            else:
                _STATE["collector_running"] = False
                _STATE["last_collector_duration_ms"] = duration_ms
            _STATE["official_lock_owner"] = None
        _OFFICIAL_LOCK.release()


def _sqlite_status() -> str:
    try:
        from db import get_latest_draw

        get_latest_draw()
        return "available"
    except Exception:
        return "unknown"


def _cloud_status() -> str:
    try:
        from database.cloud_draws import get_cloud_history_draws

        get_cloud_history_draws(1)
        return "available"
    except Exception:
        return "unknown"


def _latest_issue(stats: dict) -> str | None:
    issue = stats.get("latest_issue")
    if issue is not None:
        return str(issue)
    try:
        from db import get_latest_draw

        latest = get_latest_draw()
        return latest["issue"] if latest else None
    except Exception:
        return None


def _collector_health(catch_up: dict, runtime: dict) -> dict:
    runtime_status = runtime.get("collector_health_status")
    if runtime_status in {"ok", "warning", "error"}:
        return {
            "status": runtime_status,
            "reason": runtime.get("collector_health_reason") or "runtime_status",
        }

    status = str(catch_up.get("status") or "unknown")
    if status == "error":
        return {"status": "error", "reason": catch_up.get("error") or "collector_error"}

    last_success = _parse_time(catch_up.get("last_successful_collect_time"))
    if last_success:
        age_minutes = (datetime.now(timezone.utc) - last_success).total_seconds() / 60
        if age_minutes >= 30:
            return {"status": "error", "reason": "超過 30 分鐘沒有成功收集"}
        if age_minutes >= 15:
            return {"status": "warning", "reason": "超過 15 分鐘沒有成功收集"}

    lag = catch_up.get("lag_count")
    if isinstance(lag, int) and lag > 0:
        return {"status": "warning", "reason": f"落後 {lag} 期"}
    return {"status": "ok", "reason": "已同步"}


def _learning_status() -> dict:
    try:
        from services.learning_engine import get_learning_status

        return get_learning_status()
    except Exception as exc:
        return {
            "status": "unknown",
            "engine_version": "22.1",
            "error": str(exc),
        }


def _minimal_system_status_payload(scheduler_status: str = "unknown") -> dict:
    runtime = collector_runtime_status()
    now = _now()
    from config.production_scope import production_scope_payload
    from database.release_store import get_current_release
    from services.daily_recovery import get_recovery_status
    from services.prediction_service import prediction_lock_status

    return {
        "status": "ok",
        "provider": "kuaishou",
        "scheduler": scheduler_status,
        "latest_issue": None,
        "last_update": None,
        "database_latest_issue": None,
        "source_latest_issue": None,
        "lag_count": None,
        "collector_status": runtime.get("collector_health_status", "unknown"),
        "collector_status_reason": runtime.get("collector_health_reason", "cache unavailable"),
        "last_successful_collect_time": None,
        "last_collect_duration": None,
        "catch_up_available": True,
        "prediction_history_count": None,
        "collector_runtime": runtime,
        "scheduler_skipped_count": runtime.get("scheduler_skipped_count"),
        "continuity_status": runtime.get("continuity_status"),
        "missing_count": runtime.get("missing_count"),
        "database": {"sqlite": "unknown", "cloud": "unknown"},
        "collector": {"status": "unknown"},
        "data_quality": {"status": "unknown"},
        "learning": {"status": "unknown"},
        "production_scope": production_scope_payload(),
        "release": get_current_release(),
        "daily_recovery": get_recovery_status(),
        "prediction_lock": prediction_lock_status(),
        "cache_refreshed_at": now,
    }


def refresh_system_status_cache(scheduler_status: str = "unknown") -> dict:
    global _SYSTEM_STATUS_CACHE
    global _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS
    global _SYSTEM_STATUS_LAST_REFRESH_ERROR
    global _SYSTEM_STATUS_REFRESH_IN_PROGRESS

    if not _SYSTEM_STATUS_REFRESH_LOCK.acquire(blocking=False):
        with _SYSTEM_STATUS_CACHE_LOCK:
            cached = deepcopy(_SYSTEM_STATUS_CACHE)
        if cached:
            return _cache_metadata(cached, source="memory")
        return _cache_metadata(_minimal_system_status_payload(scheduler_status), source="minimal")

    start = time.perf_counter()
    _SYSTEM_STATUS_REFRESH_IN_PROGRESS = True
    try:
        from database.collector_store import get_collector_status
        from database.data_quality_store import get_data_quality_status
        from database.prediction_history_store import get_prediction_history_count
        from db import get_statistics
        from services.catch_up_service import get_catch_up_status
        from config.production_scope import production_scope_payload
        from database.release_store import get_current_release
        from services.daily_recovery import get_recovery_status, build_health_report
        from services.prediction_service import prediction_lock_status

        try:
            stats = get_statistics()
        except Exception:
            stats = {}

        catch_up = get_catch_up_status(fetch_source=False)
        runtime = collector_runtime_status()
        collector_health = _collector_health(catch_up, runtime)
        payload = {
            "status": "ok",
            "provider": "kuaishou",
            "scheduler": scheduler_status,
            "latest_issue": _latest_issue(stats),
            "last_update": stats.get("last_update") or stats.get("updated_at"),
            "database_latest_issue": catch_up.get("database_latest_issue"),
            "source_latest_issue": catch_up.get("source_latest_issue"),
            "lag_count": catch_up.get("lag_count"),
            "collector_status": collector_health.get("status"),
            "collector_status_reason": collector_health.get("reason"),
            "last_successful_collect_time": catch_up.get("last_successful_collect_time"),
            "last_collect_duration": catch_up.get("last_collect_duration"),
            "catch_up_available": catch_up.get("catch_up_available"),
            "prediction_history_count": get_prediction_history_count(),
            "collector_runtime": runtime,
            "scheduler_skipped_count": runtime.get("scheduler_skipped_count"),
            "continuity_status": runtime.get("continuity_status"),
            "missing_count": runtime.get("missing_count"),
            "database": {
                "sqlite": _sqlite_status(),
                "cloud": _cloud_status(),
            },
            "collector": get_collector_status(),
            "data_quality": get_data_quality_status(),
            "learning": _learning_status(),
            "production_scope": production_scope_payload(),
            "release": get_current_release(),
            "daily_recovery": get_recovery_status(),
            "prediction_lock": prediction_lock_status(),
            "ai_daily_health_report": build_health_report(),
            "cache_refreshed_at": _now(),
        }
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE = deepcopy(payload)
            _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = duration_ms
            _SYSTEM_STATUS_LAST_REFRESH_ERROR = None
        return _cache_metadata(payload, source="refresh")
    except Exception as exc:
        logger.exception("system status cache refresh failed")
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = duration_ms
            _SYSTEM_STATUS_LAST_REFRESH_ERROR = str(exc)
            cached = deepcopy(_SYSTEM_STATUS_CACHE)
        if cached:
            return _cache_metadata(cached, source="stale")
        return _cache_metadata(_minimal_system_status_payload(scheduler_status), source="minimal")
    finally:
        _SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
        _SYSTEM_STATUS_REFRESH_LOCK.release()


def get_system_status_cache(scheduler_status: str = "unknown") -> dict:
    with _SYSTEM_STATUS_CACHE_LOCK:
        cached = deepcopy(_SYSTEM_STATUS_CACHE)
    if cached:
        payload = _cache_metadata(cached, source="memory")
    else:
        payload = _cache_metadata(_minimal_system_status_payload(scheduler_status), source="minimal")
    payload["scheduler"] = scheduler_status
    return payload


def is_system_status_cache_fresh() -> bool:
    with _SYSTEM_STATUS_CACHE_LOCK:
        cached = deepcopy(_SYSTEM_STATUS_CACHE)
    age = _cache_age_seconds(cached)
    return age is not None and age <= SYSTEM_STATUS_CACHE_TTL_SECONDS


def trigger_system_status_cache_refresh(scheduler_status: str = "unknown") -> bool:
    if is_system_status_cache_fresh():
        return False
    if _SYSTEM_STATUS_REFRESH_IN_PROGRESS:
        return False

    def _refresh() -> None:
        try:
            refresh_system_status_cache(scheduler_status=scheduler_status)
        except Exception:
            logger.exception("background system status cache refresh failed")

    thread = threading.Thread(target=_refresh, name="system-status-cache-refresh", daemon=True)
    thread.start()
    return True
