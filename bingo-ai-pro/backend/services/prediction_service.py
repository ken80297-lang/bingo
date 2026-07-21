from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from typing import Any

from config.production_scope import get_production_generation, is_issue_in_current_generation
from config.release import FEATURE_VERSION, GIT_COMMIT_HASH, MODEL_VERSION, RELEASE_VERSION
from database.prediction_history_store import get_prediction_for_source_target, save_prediction_history
from services.next_prediction_center import build_prediction_history_record
from services.recommendation_center import (
    FAST_PATH_STRATEGY_VERSION,
    calculate_fast_recommendation,
    calculate_recommendation,
    fast_path_strategy_version_from_prediction,
)

logger = logging.getLogger(__name__)
PREDICTION_TIMEOUT_SECONDS = float(os.getenv("PREDICTION_TIMEOUT_SECONDS", "45"))
PREDICTION_RECOMMENDATION_TIMEOUT_SECONDS = float(os.getenv("PREDICTION_RECOMMENDATION_TIMEOUT_SECONDS", "12"))
PREDICTION_STALE_LOCK_SECONDS = 90
_PREDICTION_LOCK = threading.Lock()
_PREDICTION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prediction-service")
_LOCK_STATE: dict[str, Any] = {
    "prediction_running": False,
    "prediction_lock_owner": None,
    "prediction_last_started_at": None,
    "prediction_last_finished_at": None,
    "prediction_last_success_issue": None,
    "prediction_last_error": None,
    "prediction_recovery_count": 0,
    "prediction_lock_token": 0,
    "prediction_last_timings": [],
    "prediction_current_stage": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _stage_done(stages: list[dict], name: str, start: float, **payload: Any) -> None:
    stages.append({"stage": name, "duration_ms": _duration_ms(start), **payload})
    _LOCK_STATE["prediction_current_stage"] = name
    _LOCK_STATE["prediction_last_timings"] = list(stages)


def _remaining_seconds(start: float, reserve_seconds: float = 3.0) -> float:
    elapsed = time.perf_counter() - start
    return max(0.1, PREDICTION_TIMEOUT_SECONDS - elapsed - reserve_seconds)


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    if not is_issue_in_current_generation(text):
        return None
    return text


def prediction_lock_status() -> dict:
    started = _LOCK_STATE.get("prediction_last_started_at")
    running_seconds = None
    if started and _LOCK_STATE.get("prediction_running"):
        try:
            parsed = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            running_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
        except Exception:
            running_seconds = None
    return {
        **_LOCK_STATE,
        "prediction_running_seconds": running_seconds,
        "prediction_timeout_seconds": PREDICTION_TIMEOUT_SECONDS,
        "prediction_recommendation_timeout_seconds": PREDICTION_RECOMMENDATION_TIMEOUT_SECONDS,
        "prediction_stale_lock_seconds": PREDICTION_STALE_LOCK_SECONDS,
    }


def _lock_is_stale() -> bool:
    status = prediction_lock_status()
    seconds = status.get("prediction_running_seconds")
    stale_after = min(PREDICTION_STALE_LOCK_SECONDS, PREDICTION_TIMEOUT_SECONDS)
    return bool(_LOCK_STATE.get("prediction_running") and seconds is not None and seconds >= stale_after)


def _owner_target_issue(owner: str | None) -> int | None:
    try:
        return int(str(owner or "").rsplit(":", 1)[-1])
    except Exception:
        return None


def _lock_should_be_superseded(owner: str) -> bool:
    status = prediction_lock_status()
    seconds = status.get("prediction_running_seconds")
    current_target = _owner_target_issue(owner)
    locked_target = _owner_target_issue(status.get("prediction_lock_owner"))
    return bool(
        _LOCK_STATE.get("prediction_running")
        and seconds is not None
        and seconds >= 10
        and current_target is not None
        and locked_target is not None
        and current_target > locked_target
    )


def _recover_prediction_lock(reason: str) -> None:
    _LOCK_STATE["prediction_recovery_count"] = int(_LOCK_STATE.get("prediction_recovery_count") or 0) + 1
    _LOCK_STATE["prediction_running"] = False
    _LOCK_STATE["prediction_lock_owner"] = None
    _LOCK_STATE["prediction_lock_token"] = int(_LOCK_STATE.get("prediction_lock_token") or 0) + 1
    _LOCK_STATE["prediction_last_error"] = reason
    try:
        _PREDICTION_LOCK.release()
    except RuntimeError:
        pass


def recover_prediction_lock_for_target(based_on_issue: str | None, target_issue: str | None, *, reason: str = "manual_recovery") -> dict:
    based_on = _valid_issue(based_on_issue)
    target = _valid_issue(target_issue)
    requested_owner = f"official_collector:official_draw_saved:{based_on or 'unknown'}:{target or 'unknown'}"
    status = prediction_lock_status()
    if not status.get("prediction_running"):
        return {"status": "not_running", **status}
    current_target = _owner_target_issue(requested_owner)
    locked_target = _owner_target_issue(status.get("prediction_lock_owner"))
    running_seconds = status.get("prediction_running_seconds")
    if (
        current_target is not None
        and locked_target is not None
        and current_target >= locked_target
        and (running_seconds is None or running_seconds >= 5)
    ):
        _recover_prediction_lock(reason)
        return {"status": "recovered", "requested_owner": requested_owner, "previous_lock": status}
    return {"status": "active_lock_retained", "requested_owner": requested_owner, **status}


def _acquire_prediction_lock(owner: str) -> tuple[bool, dict]:
    if _PREDICTION_LOCK.acquire(blocking=False):
        token = int(_LOCK_STATE.get("prediction_lock_token") or 0) + 1
        _LOCK_STATE.update(
            {
                "prediction_running": True,
                "prediction_lock_owner": owner,
                "prediction_lock_token": token,
                "prediction_last_started_at": _now(),
                "prediction_last_error": None,
                "prediction_last_timings": [],
                "prediction_current_stage": "lock_acquire",
            }
        )
        return True, {"status": "locked", "lock_owner": owner, "lock_token": token}
    if not _LOCK_STATE.get("prediction_running") or _lock_is_stale() or _lock_should_be_superseded(owner):
        _recover_prediction_lock("stale_or_superseded_prediction_lock")
        return _acquire_prediction_lock(owner)
    return False, {"status": "already_running", **prediction_lock_status()}


def _release_prediction_lock(owner: str, *, lock_token: int | None = None, success_issue: str | None = None, error: str | None = None) -> None:
    current_owner = _LOCK_STATE.get("prediction_lock_owner")
    current_token = int(_LOCK_STATE.get("prediction_lock_token") or 0)
    if current_owner != owner or (lock_token is not None and lock_token != current_token):
        logger.warning(
            "prediction lock release skipped owner=%s current_owner=%s token=%s current_token=%s",
            owner,
            current_owner,
            lock_token,
            current_token,
        )
        return
    _LOCK_STATE.update(
        {
            "prediction_running": False,
            "prediction_lock_owner": None,
            "prediction_last_finished_at": _now(),
            "prediction_last_success_issue": success_issue or _LOCK_STATE.get("prediction_last_success_issue"),
            "prediction_last_error": error,
            "prediction_current_stage": None,
        }
    )
    try:
        _PREDICTION_LOCK.release()
    except RuntimeError:
        logger.warning("prediction lock release skipped owner=%s", owner)


def _next_issue(issue: str) -> str:
    return str(int(issue) + 1)


def _numbers(values: Any) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return sorted(result)


def _record_event(
    *,
    event_type: str,
    status: str,
    based_on_issue: str | None,
    target_issue: str | None,
    source: str,
    trigger: str,
    reason: str | None = None,
    recommended_count: int = 0,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> None:
    try:
        from services.operations_center import record_operation_event
        from database.prediction_history_store import _json_dumps

        payload = {
            "event_type": event_type,
            "based_on_issue": based_on_issue,
            "target_issue": target_issue,
            "source": source,
            "trigger": trigger,
            "status": status,
            "reason": reason,
            "recommended_count": recommended_count,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "error_type": error_type,
            "error_message": error_message,
        }
        record_operation_event(
            component="prediction",
            event_type=event_type,
            status=status,
            issue=based_on_issue,
            message=_json_dumps(payload),
            duration_ms=duration_ms,
            error_type=error_type or reason,
            error_message=error_message,
        )
    except Exception:
        logger.exception("prediction service event recording failed")


def _existing_prediction(based_on_issue: str, target_issue: str) -> dict | None:
    return get_prediction_for_source_target(based_on_issue, target_issue)


def _existing_fast_path_status(existing: dict | None) -> dict:
    previous_version = fast_path_strategy_version_from_prediction(existing)
    is_current = previous_version == FAST_PATH_STRATEGY_VERSION
    return {
        "is_current": is_current,
        "fast_path_strategy_version": FAST_PATH_STRATEGY_VERSION,
        "previous_strategy_version": previous_version,
        "regenerated_reason": None if is_current else "fast_path_strategy_version_changed",
    }


def create_for_official_draw(
    based_on_issue: str,
    *,
    source: str,
    trigger: str,
    target_issue: str | None = None,
    collector_metadata: dict | None = None,
    force: bool = False,
) -> dict:
    start = time.perf_counter()
    started_at = _now()
    stages: list[dict] = []
    source = str(source or "unknown")
    trigger = str(trigger or "unknown")
    based_on = _valid_issue(based_on_issue)
    target = _valid_issue(target_issue) if target_issue is not None else None
    if based_on and target is None:
        target = _next_issue(based_on)
    lock_owner = f"{source}:{trigger}:{based_on or 'unknown'}:{target or 'unknown'}"
    locked, lock_payload = _acquire_prediction_lock(lock_owner)
    lock_token = lock_payload.get("lock_token")
    _stage_done(stages, "lock_acquire", start, status=lock_payload.get("status"))
    if not locked:
        _record_event(
            event_type="prediction_skipped",
            status="warning",
            based_on_issue=based_on,
            target_issue=target,
            source=source,
            trigger=trigger,
            reason=lock_payload.get("status") or "already_running",
            started_at=started_at,
            completed_at=_now(),
            duration_ms=_duration_ms(start),
        )
        return {
            "status": "already_running",
            "based_on_issue": based_on,
            "target_issue": target,
            "prediction_id": None,
            "recommended_count": 0,
            "skip_reason": "already_running",
            "duration_ms": _duration_ms(start),
            "persisted": False,
            "lock": lock_payload,
            "timings": stages,
        }

    def skipped(reason: str, recommended_count: int = 0) -> dict:
        completed_at = _now()
        duration = _duration_ms(start)
        _record_event(
            event_type="prediction_skipped",
            status="warning",
            based_on_issue=based_on,
            target_issue=target,
            source=source,
            trigger=trigger,
            reason=reason,
            recommended_count=recommended_count,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration,
        )
        return {
            "status": "skipped",
            "based_on_issue": based_on,
            "target_issue": target,
            "prediction_id": None,
            "recommended_count": recommended_count,
            "skip_reason": reason,
            "duration_ms": duration,
            "persisted": False,
            "timings": stages,
        }

    try:
        _record_event(
            event_type="prediction_create_started",
            status="ok",
            based_on_issue=based_on,
            target_issue=target,
            source=source,
            trigger=trigger,
            started_at=started_at,
        )

        if not based_on:
            _stage_done(stages, "validation", start, status="skipped", reason="based_on_missing")
            return skipped("based_on_missing")
        if not target:
            _stage_done(stages, "validation", start, status="skipped", reason="target_unconfirmed")
            return skipped("target_unconfirmed")
        try:
            if int(target) != int(based_on) + 1:
                _stage_done(stages, "validation", start, status="skipped", reason="target_unconfirmed")
                return skipped("target_unconfirmed")
        except Exception:
            _stage_done(stages, "validation", start, status="skipped", reason="target_unconfirmed")
            return skipped("target_unconfirmed")

        mark = time.perf_counter()
        existing = _existing_prediction(based_on, target)
        existing_strategy = _existing_fast_path_status(existing)
        should_regenerate_existing = bool(existing and not existing_strategy["is_current"])
        _stage_done(
            stages,
            "existing_prediction_lookup",
            mark,
            found=bool(existing),
            fast_path_strategy_version=FAST_PATH_STRATEGY_VERSION,
            previous_strategy_version=existing_strategy["previous_strategy_version"],
            regenerated_reason=existing_strategy["regenerated_reason"] if should_regenerate_existing else None,
        )
        if existing and not force and not should_regenerate_existing:
            completed_at = _now()
            duration = _duration_ms(start)
            _record_event(
                event_type="prediction_already_exists",
                status="ok",
                based_on_issue=based_on,
                target_issue=target,
                source=source,
                trigger=trigger,
                recommended_count=len(existing.get("recommend_numbers") or []),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration,
            )
            return {
                "status": "already_exists",
                "based_on_issue": based_on,
                "target_issue": target,
                "prediction_id": existing.get("id"),
                "recommended_count": len(existing.get("recommend_numbers") or []),
                "skip_reason": None,
                "duration_ms": duration,
                "persisted": False,
                "prediction_status": existing.get("prediction_status"),
                "fast_path_strategy_version": existing_strategy["previous_strategy_version"],
                "timings": stages,
            }
        regenerated_reason = None
        previous_strategy_version = None
        if should_regenerate_existing:
            regenerated_reason = existing_strategy["regenerated_reason"]
            previous_strategy_version = existing_strategy["previous_strategy_version"]
            force = True

        recommendation_context = {
            "source": source,
            "trigger": trigger,
            "collector_metadata": collector_metadata or {},
            "prediction_service": True,
            "ensure_simulation": False,
            "fast_path_strategy_version": FAST_PATH_STRATEGY_VERSION,
            "regenerated_reason": regenerated_reason,
            "previous_strategy_version": previous_strategy_version,
        }
        mark = time.perf_counter()
        recommendation_result = calculate_fast_recommendation(
            based_on,
            target,
            context={**recommendation_context, "path": "prediction_service_fast_path"},
        )
        _stage_done(
            stages,
            "fast_recommendation_build",
            mark,
            status=(recommendation_result or {}).get("status"),
            reason=(recommendation_result or {}).get("reason") or (recommendation_result or {}).get("message"),
        )
        if recommendation_result.get("status") == "ok":
            recommendation_result.setdefault("recommendation_status", "production_fast_path")
        else:
            recommendation_result = None

        mark = time.perf_counter()
        if recommendation_result is None:
            recommendation_timeout = min(PREDICTION_RECOMMENDATION_TIMEOUT_SECONDS, _remaining_seconds(start))
            future = _PREDICTION_EXECUTOR.submit(
                calculate_recommendation,
                based_on,
                target,
                context=recommendation_context,
            )
            try:
                recommendation_result = future.result(timeout=recommendation_timeout)
                _stage_done(
                    stages,
                    "recommendation_build",
                    mark,
                    status=(recommendation_result or {}).get("status"),
                    timeout_seconds=round(recommendation_timeout, 3),
                    timed_out=False,
                )
            except TimeoutError:
                future.cancel()
                _stage_done(
                    stages,
                    "recommendation_build",
                    mark,
                    status="timed_out",
                    timeout_seconds=round(recommendation_timeout, 3),
                    timed_out=True,
                )
                _record_event(
                    event_type="prediction_skipped",
                    status="warning",
                    based_on_issue=based_on,
                    target_issue=target,
                    source=source,
                    trigger=trigger,
                    reason="timed_out",
                    started_at=started_at,
                    completed_at=_now(),
                    duration_ms=_duration_ms(start),
                    error_type="timed_out",
                    error_message=f"recommendation_build exceeded {recommendation_timeout:.2f}s",
                )
                return {
                    "status": "skipped",
                    "based_on_issue": based_on,
                    "target_issue": target,
                    "prediction_id": None,
                    "recommended_count": 0,
                    "skip_reason": "timed_out",
                    "duration_ms": _duration_ms(start),
                    "persisted": False,
                    "timings": stages,
                    "timeout_stage": "recommendation_build",
                    "pending_usable": False,
                }
        if _duration_ms(start) / 1000 >= PREDICTION_TIMEOUT_SECONDS:
            _stage_done(stages, "timeout_guard", start, status="timed_out")
            return skipped("timed_out")
        if recommendation_result.get("status") != "ok":
            completed_at = _now()
            duration = _duration_ms(start)
            error = recommendation_result.get("message") or "recommendation_failed"
            _record_event(
                event_type="prediction_failed",
                status="error",
                based_on_issue=based_on,
                target_issue=target,
                source=source,
                trigger=trigger,
                reason="recommendation_failed",
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration,
                error_type="recommendation_failed",
                error_message=error,
            )
            return {
                "status": "failed",
                "based_on_issue": based_on,
                "target_issue": target,
                "prediction_id": None,
                "recommended_count": 0,
                "skip_reason": "recommendation_failed",
                "duration_ms": duration,
                "error": error,
                "persisted": False,
                "timings": stages,
            }

        recommendation = recommendation_result.get("recommendation") or {}
        recommendation["issue"] = based_on
        recommendation["target_issue"] = target
        mark = time.perf_counter()
        record = build_prediction_history_record(recommendation)
        _stage_done(stages, "history_record_build", mark, record_built=bool(record))
        recommended = _numbers((record or {}).get("recommend_numbers"))
        if len(recommended) != 20:
            _stage_done(stages, "recommendation_validation", start, status="skipped", recommended_count=len(recommended))
            return skipped("insufficient_recommendations", len(recommended))
        record["prediction_status"] = "waiting_draw"
        record["learning_used"] = False
        record["source"] = source
        record["trigger"] = trigger
        record["collector_metadata"] = collector_metadata or {}
        record["production_generation"] = get_production_generation()
        record["production_valid"] = True
        record["release_version"] = RELEASE_VERSION
        record["git_commit_hash"] = GIT_COMMIT_HASH
        record["model_version"] = MODEL_VERSION
        record["feature_version"] = FEATURE_VERSION
        model_scores = record.get("model_scores") if isinstance(record.get("model_scores"), dict) else {}
        fast_path_scores = model_scores.get("production_fast_path") if isinstance(model_scores.get("production_fast_path"), dict) else {}
        fast_path_scores.update(
            {
                "fast_path_strategy_version": FAST_PATH_STRATEGY_VERSION,
                "regenerated_reason": regenerated_reason,
                "previous_strategy_version": previous_strategy_version,
            }
        )
        model_scores["production_fast_path"] = fast_path_scores
        record["model_scores"] = model_scores
        mark = time.perf_counter()
        saved = save_prediction_history(record, caller_context="prediction_service")
        _stage_done(stages, "prediction_history_save", mark, status=saved.get("status"), storage=saved.get("storage"))
        completed_at = _now()
        duration = _duration_ms(start)
        if saved.get("status") == "ok":
            prediction_id = saved.get("id")
            _record_event(
                event_type="prediction_created",
                status="ok",
                based_on_issue=based_on,
                target_issue=target,
                source=source,
                trigger=trigger,
                recommended_count=len(recommended),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=duration,
            )
            return {
                "status": "created",
                "based_on_issue": based_on,
                "target_issue": target,
                "prediction_id": prediction_id,
                "recommended_count": len(recommended),
                "skip_reason": None,
                "duration_ms": duration,
                "persisted": True,
                "storage": saved.get("storage"),
                "fast_path_strategy_version": FAST_PATH_STRATEGY_VERSION,
                "regenerated_reason": regenerated_reason,
                "previous_strategy_version": previous_strategy_version,
                "timings": stages,
            }
        status = "failed" if saved.get("status") in ("error", "rejected") else "skipped"
        event_type = "prediction_failed" if status == "failed" else "prediction_skipped"
        reason = saved.get("skip_reason") or saved.get("error") or saved.get("message")
        _record_event(
            event_type=event_type,
            status="error" if status == "failed" else "warning",
            based_on_issue=based_on,
            target_issue=target,
            source=source,
            trigger=trigger,
            reason=reason,
            recommended_count=len(recommended),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration,
            error_type=reason if status == "failed" else None,
        )
        return {
            "status": status,
            "based_on_issue": based_on,
            "target_issue": target,
            "prediction_id": saved.get("id"),
            "recommended_count": len(recommended),
            "skip_reason": reason,
            "duration_ms": duration,
            "persisted": False,
            "storage_result": saved,
            "timings": stages,
        }
    except Exception as exc:
        _LOCK_STATE["prediction_last_error"] = str(exc)
        raise
    finally:
        _release_prediction_lock(
            lock_owner,
            lock_token=lock_token,
            success_issue=target if based_on and target else None,
            error=_LOCK_STATE.get("prediction_last_error"),
        )
