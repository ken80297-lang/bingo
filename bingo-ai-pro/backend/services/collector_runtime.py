from __future__ import annotations

import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SYSTEM_STATUS_CACHE_TTL_SECONDS = int(os.getenv("SYSTEM_STATUS_CACHE_TTL_SECONDS", "75"))
SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS = min(float(os.getenv("SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS", "9")), 9.0)
SYSTEM_STATUS_PRODUCTION_SCOPE_TIMEOUT_SECONDS = float(os.getenv("SYSTEM_STATUS_PRODUCTION_SCOPE_TIMEOUT_SECONDS", "3"))
SYSTEM_STATUS_STEP_MIN_REMAINING_SECONDS = float(os.getenv("SYSTEM_STATUS_STEP_MIN_REMAINING_SECONDS", "1.5"))
SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS = float(os.getenv("SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS", "1.5"))
OFFICIAL_LOCK_STALE_SECONDS = 180

_OFFICIAL_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_SYSTEM_STATUS_CACHE_LOCK = threading.RLock()
_SYSTEM_STATUS_REFRESH_LOCK = threading.Lock()
_SYSTEM_STATUS_STEP_WORKER_LOCK = threading.Lock()

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


class _StatusCacheDeadlineExceeded(Exception):
    pass


class _StatusCacheStepTimeout(Exception):
    pass


class _StatusCacheStepWorkerBusy(Exception):
    pass


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


def _status_cache_step_log(step: str, start: float, success: bool, timeout: bool = False) -> None:
    logger.info(
        "status_cache_step step=%s duration_ms=%s success=%s timeout=%s",
        step,
        round((time.perf_counter() - start) * 1000, 2),
        str(success).lower(),
        str(timeout).lower(),
    )


def _status_cache_deadline_exceeded(start: float) -> bool:
    return (time.perf_counter() - start) >= SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS


def _cached_status_value(key: str, default):
    with _SYSTEM_STATUS_CACHE_LOCK:
        cached = deepcopy(_SYSTEM_STATUS_CACHE)
    if isinstance(cached, dict) and key in cached:
        return cached.get(key)
    return default


def _run_bounded_step(func, timeout_seconds: float):
    if not _SYSTEM_STATUS_STEP_WORKER_LOCK.acquire(blocking=False):
        raise _StatusCacheStepWorkerBusy()
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def target() -> None:
        try:
            result_queue.put(("ok", func()), block=False)
        except Exception as exc:
            result_queue.put(("error", exc), block=False)
        finally:
            _SYSTEM_STATUS_STEP_WORKER_LOCK.release()

    threading.Thread(target=target, daemon=True).start()
    try:
        status, value = result_queue.get(timeout=max(0.001, timeout_seconds))
    except queue.Empty as exc:
        raise _StatusCacheStepTimeout() from exc
    if status == "error":
        raise value
    return value


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


def _runtime_seconds_since(value: Any) -> float | None:
    parsed = _parse_time(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _official_lock_is_stale() -> bool:
    with _STATE_LOCK:
        owner = _STATE.get("official_lock_owner")
        started = _STATE.get("last_catch_up_started_at") if owner == "catch_up" else _STATE.get("last_collector_started_at")
        finished = _STATE.get("last_catch_up_finished_at") if owner == "catch_up" else _STATE.get("last_collector_finished_at")
        running = bool(_STATE.get("catch_up_running")) if owner == "catch_up" else bool(_STATE.get("collector_running"))
        exit_reason = _STATE.get("last_catch_up_exit_reason") if owner == "catch_up" else _STATE.get("last_collector_exit_reason")
    if not owner or not running:
        return False
    started_at = _parse_time(started)
    finished_at = _parse_time(finished)
    if started_at and finished_at and finished_at >= started_at and exit_reason in {"completed", "deadline_exceeded", "exception"}:
        return True
    age = _runtime_seconds_since(started)
    return bool(age is not None and age >= OFFICIAL_LOCK_STALE_SECONDS)


def _release_stale_official_lock() -> bool:
    if not _official_lock_is_stale():
        return False
    with _STATE_LOCK:
        stale_owner = _STATE.get("official_lock_owner")
        _STATE["official_lock_owner"] = None
        _STATE["collector_running"] = False
        _STATE["catch_up_running"] = False
        _STATE["last_job_exit_reason"] = "stale_lock_recovered"
        if stale_owner == "catch_up":
            _STATE["last_catch_up_exit_reason"] = _STATE.get("last_catch_up_exit_reason") or "stale_lock_recovered"
        else:
            _STATE["last_collector_exit_reason"] = _STATE.get("last_collector_exit_reason") or "stale_lock_recovered"
    try:
        _OFFICIAL_LOCK.release()
        return True
    except RuntimeError:
        return False


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
    if not acquired and _release_stale_official_lock():
        acquired = _OFFICIAL_LOCK.acquire(blocking=False)
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
        from services.learning_engine import get_learning_status_snapshot

        return get_learning_status_snapshot()
    except Exception as exc:
        return {
            "status": "unknown",
            "engine_version": "22.1",
            "stale": True,
            "partial": True,
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
        steps: list[dict[str, Any]] = []
        timeout_steps: list[str] = []
        partial = False

        def run_step(
            step: str,
            func,
            default,
            *,
            min_remaining_seconds: float | None = None,
            step_timeout_seconds: float | None = None,
        ):
            nonlocal partial
            step_start = time.perf_counter()
            step_started = _now()
            min_remaining = (
                SYSTEM_STATUS_STEP_MIN_REMAINING_SECONDS
                if min_remaining_seconds is None
                else max(0.0, float(min_remaining_seconds))
            )
            remaining = SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS - (step_start - start)
            if remaining <= 0:
                timeout_steps.append(step)
                _status_cache_step_log(step, step_start, success=False, timeout=True)
                raise _StatusCacheDeadlineExceeded(step)
            if remaining <= min_remaining:
                partial = True
                steps.append(
                    {
                        "step": step,
                        "step_started": step_started,
                        "step_completed": _now(),
                        "duration_ms": round((time.perf_counter() - step_start) * 1000, 2),
                        "success": False,
                        "timeout": False,
                        "result": "stale",
                        "reason": "insufficient_refresh_budget",
                    }
                )
                _status_cache_step_log(step, step_start, success=False)
                return default
            try:
                if step_timeout_seconds is not None:
                    timeout_seconds = min(max(0.001, step_timeout_seconds), max(0.001, remaining - min_remaining))
                    value = _run_bounded_step(func, timeout_seconds)
                else:
                    value = func()
                elapsed_ms = round((time.perf_counter() - step_start) * 1000, 2)
                if _status_cache_deadline_exceeded(start):
                    timeout_steps.append(step)
                    steps.append(
                        {
                            "step": step,
                            "step_started": step_started,
                            "step_completed": _now(),
                            "duration_ms": elapsed_ms,
                            "success": False,
                            "timeout": True,
                            "result": "timeout",
                        }
                    )
                    _status_cache_step_log(step, step_start, success=False, timeout=True)
                    raise _StatusCacheDeadlineExceeded(step)
                steps.append(
                    {
                        "step": step,
                        "step_started": step_started,
                        "step_completed": _now(),
                        "duration_ms": elapsed_ms,
                        "success": True,
                        "timeout": False,
                        "result": "ok",
                    }
                )
                _status_cache_step_log(step, step_start, success=True)
                return value
            except _StatusCacheStepWorkerBusy:
                partial = True
                steps.append(
                    {
                        "step": step,
                        "step_started": step_started,
                        "step_completed": _now(),
                        "duration_ms": round((time.perf_counter() - step_start) * 1000, 2),
                        "success": False,
                        "timeout": False,
                        "result": "stale",
                        "reason": "status_step_worker_busy",
                    }
                )
                _status_cache_step_log(step, step_start, success=False)
                return default
            except _StatusCacheStepTimeout:
                timeout_steps.append(step)
                partial = True
                steps.append(
                    {
                        "step": step,
                        "step_started": step_started,
                        "step_completed": _now(),
                        "duration_ms": round((time.perf_counter() - step_start) * 1000, 2),
                        "success": False,
                        "timeout": True,
                        "result": "timeout",
                    }
                )
                _status_cache_step_log(step, step_start, success=False, timeout=True)
                return default
            except _StatusCacheDeadlineExceeded:
                raise
            except Exception as exc:
                logger.warning("system status cache step failed step=%s error=%s", step, exc)
                steps.append(
                    {
                        "step": step,
                        "step_started": step_started,
                        "step_completed": _now(),
                        "duration_ms": round((time.perf_counter() - step_start) * 1000, 2),
                        "success": False,
                        "timeout": False,
                        "result": "error",
                    }
                )
                _status_cache_step_log(step, step_start, success=False)
                return default

        from database.collector_store import get_collector_status
        from database.data_quality_store import get_data_quality_status
        from database.prediction_history_store import get_prediction_history_count
        from db import get_statistics
        from services.catch_up_service import get_catch_up_status
        from config.production_scope import production_scope_payload
        from database.release_store import get_current_release
        from services.daily_recovery import get_recovery_status, build_health_report
        from services.prediction_service import prediction_lock_status

        stats = run_step("db_statistics", get_statistics, {}, step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS)
        catch_up = run_step(
            "catch_up_status",
            lambda: get_catch_up_status(fetch_source=False),
            _cached_status_value("catch_up_status", {}),
            step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
        )
        runtime = run_step("collector_runtime", collector_runtime_status, collector_runtime_status())
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
            "prediction_history_count": run_step(
                "prediction_history_count",
                get_prediction_history_count,
                _cached_status_value("prediction_history_count", None),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "collector_runtime": runtime,
            "scheduler_skipped_count": runtime.get("scheduler_skipped_count"),
            "continuity_status": runtime.get("continuity_status"),
            "missing_count": runtime.get("missing_count"),
            "database": {
                "sqlite": run_step("sqlite_status", _sqlite_status, "unknown", step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS),
                "cloud": run_step("cloud_status", _cloud_status, "unknown", step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS),
            },
            "collector": run_step(
                "collector_status",
                get_collector_status,
                _cached_status_value("collector", {"status": "unknown"}),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "data_quality": run_step(
                "data_quality_status",
                get_data_quality_status,
                _cached_status_value("data_quality", {"status": "unknown"}),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "learning": run_step(
                "learning_status",
                _learning_status,
                _cached_status_value("learning", {"status": "unknown", "stale": True, "partial": True}),
                min_remaining_seconds=0.05,
            ),
            "production_scope": run_step(
                "production_scope",
                production_scope_payload,
                _cached_status_value("production_scope", {}),
                min_remaining_seconds=min(0.05, SYSTEM_STATUS_PRODUCTION_SCOPE_TIMEOUT_SECONDS),
            ),
            "release": run_step(
                "release",
                get_current_release,
                _cached_status_value("release", {}),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "daily_recovery": run_step(
                "daily_recovery",
                get_recovery_status,
                _cached_status_value("daily_recovery", {"status": "unknown"}),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "prediction_lock": run_step(
                "prediction_lock",
                prediction_lock_status,
                _cached_status_value("prediction_lock", {"status": "unknown"}),
                min_remaining_seconds=0.05,
            ),
            "ai_daily_health_report": run_step(
                "health_report",
                build_health_report,
                _cached_status_value("ai_daily_health_report", {"status": "unknown"}),
                step_timeout_seconds=SYSTEM_STATUS_DB_STEP_TIMEOUT_SECONDS,
            ),
            "cache_refreshed_at": _now(),
            "status_cache_steps": steps,
            "timeout_steps": timeout_steps,
            "timeout": False,
            "partial": partial,
        }
    except _StatusCacheDeadlineExceeded as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _SYSTEM_STATUS_CACHE_LOCK:
            cached = deepcopy(_SYSTEM_STATUS_CACHE)
        payload = cached or _minimal_system_status_payload(scheduler_status)
        payload.update(
            {
                "status": payload.get("status", "ok"),
                "scheduler": scheduler_status,
                "cache_refreshed_at": _now(),
                "status_cache_steps": steps,
                "timeout_steps": timeout_steps or [str(exc)],
                "timeout": True,
                "partial": True,
            }
        )
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE = deepcopy(payload)
            _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = duration_ms
            _SYSTEM_STATUS_LAST_REFRESH_ERROR = "deadline_exceeded"
        logger.warning(
            "system status cache refresh timed out duration_ms=%s timeout_steps=%s",
            duration_ms,
            payload.get("timeout_steps"),
        )
        logger.info(
            "status_cache_refresh_completed duration_ms=%s cache_state=%s stale=%s partial=%s timeout_steps=%s success=false",
            duration_ms,
            "stale" if cached else "minimal",
            str(True).lower(),
            str(True).lower(),
            payload.get("timeout_steps"),
        )
        _SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
        return _cache_metadata(payload, source="stale" if cached else "minimal")
    except Exception as exc:
        logger.exception("system status cache refresh failed")
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = duration_ms
            _SYSTEM_STATUS_LAST_REFRESH_ERROR = str(exc)
            cached = deepcopy(_SYSTEM_STATUS_CACHE)
        if cached:
            _SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
            return _cache_metadata(cached, source="stale")
        _SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
        return _cache_metadata(_minimal_system_status_payload(scheduler_status), source="minimal")
    else:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        with _SYSTEM_STATUS_CACHE_LOCK:
            _SYSTEM_STATUS_CACHE = deepcopy(payload)
            _SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = duration_ms
            _SYSTEM_STATUS_LAST_REFRESH_ERROR = None
        logger.info(
            "status_cache_refresh_completed duration_ms=%s cache_state=refresh stale=false partial=false timeout_steps=[] success=true",
            duration_ms,
        )
        return _cache_metadata(payload, source="refresh")
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
