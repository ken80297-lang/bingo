from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.analysis_store import get_analysis_history, save_analysis_history
from database.official_draw_store import (
    get_latest_official_draw_sync_status,
    get_latest_official_draw,
    get_official_draw_by_issue,
    save_official_draws,
)
from database.prediction_history_store import get_prediction_for_source_target
from services.collector_runtime import update_collector_runtime

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
HISTORICAL_CATCHUP_ENABLED = os.getenv("HISTORICAL_CATCHUP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
LATEST_ISSUE_PRIORITY = os.getenv("LATEST_ISSUE_PRIORITY", "true").lower() not in {"0", "false", "no", "off"}
LATEST_SYNC_STALE_SECONDS = 12 * 60

_STATE_LOCK = threading.RLock()
_RECONCILE_LOCK = threading.RLock()
_RECONCILE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="latest-sync-reconcile")
_RECONCILE_IN_FLIGHT: set[str] = set()
_LATEST_SYNC_CACHE_TTL_SECONDS = 10
_LATEST_SYNC_CACHE: dict[str, Any] = {"snapshot": None, "expires_at": 0.0}
_LATEST_SYNC_STATE: dict[str, Any] = {
    "official_detected_issue": None,
    "source_issue": None,
    "database_latest_issue": None,
    "dashboard_latest_issue": None,
    "latest_saved_at": None,
    "draw_time": None,
    "numbers_count": 0,
    "database_saved": False,
    "analysis_created": False,
    "prediction_created": False,
    "issues_behind": None,
    "sync_status": "unknown",
    "historical_catchup_enabled": HISTORICAL_CATCHUP_ENABLED,
    "latest_issue_priority": LATEST_ISSUE_PRIORITY,
    "target_issue": None,
    "detected_at": None,
    "last_attempt_at": None,
    "attempt_count": 0,
    "failure_stage": None,
    "failure_reason": None,
    "next_retry_expected_at": None,
    "stages": {},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def _issue_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


def _next_issue(issue: Any) -> str | None:
    source_issue = str(issue or "").strip()
    if not source_issue:
        return None
    try:
        from services.prediction_refresh import _next_issue as resolve_next_issue

        return resolve_next_issue(source_issue)
    except Exception:
        issue_number = _issue_int(source_issue)
        if issue_number is None:
            return None
        return str(issue_number + 1)


def _valid_numbers(values: Any) -> list[int]:
    numbers: list[int] = []
    try:
        source = list(values or [])
    except Exception:
        return []
    for value in source:
        try:
            number = int(value)
        except Exception:
            return []
        if not 1 <= number <= 80 or number in numbers:
            return []
        numbers.append(number)
    return sorted(numbers) if len(numbers) == 20 else []


def is_complete_official_draw(draw: dict | None) -> bool:
    if not draw or not str(draw.get("issue") or "").strip().isdigit():
        return False
    return len(_valid_numbers(draw.get("numbers"))) == 20


def _latest_draw_from_source() -> dict | None:
    draws = fetch_official_bingo_results(_today_taipei(), page_num=1, page_size=10)
    valid = [draw for draw in draws if is_complete_official_draw(draw)]
    if not valid:
        return None
    return max(valid, key=lambda item: _issue_int(item.get("issue")) or 0)


def _analysis_exists(issue: str) -> bool:
    try:
        return any(str(item.get("issue")) == str(issue) for item in get_analysis_history(10))
    except Exception:
        logger.exception("latest sync analysis lookup failed")
        return False


def _latest_prediction_for_issue(issue: str) -> dict | None:
    issue_number = _issue_int(issue)
    target_issue = _next_issue(issue)
    if issue_number is None or not target_issue:
        return None
    return get_prediction_for_source_target(str(issue), target_issue)


def _prediction_exists_for_latest(issue: str) -> bool:
    return _latest_prediction_for_issue(issue) is not None


def _sync_status(payload: dict[str, Any]) -> str:
    if payload.get("failure_stage") and not payload.get("database_saved"):
        return "error"
    official = _issue_int(payload.get("official_detected_issue"))
    database = _issue_int(payload.get("database_latest_issue"))
    if official is not None and database is None:
        return "detected_not_saved"
    if official is not None and database is not None and database < official:
        return "database_behind"
    if payload.get("database_saved") and not payload.get("analysis_created"):
        return "analysis_pending"
    if payload.get("database_saved") and not payload.get("prediction_created"):
        return "prediction_pending"
    saved_at = payload.get("latest_saved_at")
    if saved_at:
        try:
            parsed = datetime.fromisoformat(str(saved_at).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - parsed).total_seconds() > LATEST_SYNC_STALE_SECONDS:
                return "stale"
        except Exception:
            pass
    if payload.get("database_saved"):
        return "synced"
    return "unknown"


def _update_state(**kwargs: Any) -> dict[str, Any]:
    with _STATE_LOCK:
        _LATEST_SYNC_STATE.update(kwargs)
        official = _issue_int(_LATEST_SYNC_STATE.get("official_detected_issue"))
        database = _issue_int(_LATEST_SYNC_STATE.get("database_latest_issue"))
        _LATEST_SYNC_STATE["issues_behind"] = (
            max(0, official - database) if official is not None and database is not None else None
        )
        _LATEST_SYNC_STATE["sync_status"] = _sync_status(_LATEST_SYNC_STATE)
        snapshot = deepcopy(_LATEST_SYNC_STATE)
    update_collector_runtime(
        latest_sync_status=snapshot.get("sync_status"),
        latest_success_issue=snapshot.get("database_latest_issue") if snapshot.get("database_saved") else None,
        latest_official_detected_issue=snapshot.get("official_detected_issue"),
    )
    return snapshot


def _stage(status: str, completed_at: str | None = None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "completed_at": completed_at, "error": error}


def _snapshot_stages(
    *,
    database_saved: bool,
    analysis_created: bool,
    prediction_created: bool,
    dashboard_ready: bool,
    failure_stage: str | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    def status(stage: str, ready: bool, pending_after: bool) -> str:
        if failure_stage == stage:
            return "failed"
        if ready:
            return "completed"
        if pending_after:
            return "pending"
        return "waiting"

    return {
        "collector": _stage("completed" if database_saved else "pending"),
        "database": _stage(status("database", database_saved, True)),
        "analysis": _stage(status("analysis", analysis_created, database_saved)),
        "prediction": _stage(status("prediction", prediction_created, database_saved and analysis_created)),
        "dashboard": _stage(status("dashboard", dashboard_ready, database_saved and analysis_created and prediction_created)),
        "failure": {
            "stage": failure_stage,
            "reason": failure_reason,
        },
    }


def _reconcile_missing_prediction(latest: dict, source_issue: str, target_issue: str) -> dict[str, Any]:
    with _STATE_LOCK:
        attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0) + 1
    try:
        from services.prediction_refresh import ensure_next_prediction

        result = ensure_next_prediction(latest)
        return {
            "attempt_count": attempt_count,
            "last_attempt_at": _now(),
            "prediction_reconcile": result,
            "prediction_created": result.get("refresh_status") in {"ready", "existing"}
            or result.get("status") in {"created", "already_exists", "existing"},
            "failure_stage": None,
            "failure_reason": None,
            "next_retry_expected_at": None,
        }
    except Exception as exc:
        logger.exception(
            "latest sync prediction reconcile failed source_issue=%s target_issue=%s",
            source_issue,
            target_issue,
        )
        return {
            "attempt_count": attempt_count,
            "last_attempt_at": _now(),
            "prediction_reconcile": {"status": "failed", "error": str(exc)},
            "prediction_created": False,
            "failure_stage": "prediction",
            "failure_reason": str(exc),
            "next_retry_expected_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        }


def _run_prediction_reconcile(latest: dict, source_issue: str, target_issue: str) -> None:
    try:
        result = _reconcile_missing_prediction(latest, source_issue, target_issue)
        prediction_created = bool(result.get("prediction_created")) or _prediction_exists_for_latest(source_issue)
        if prediction_created:
            _update_state(
                source_issue=source_issue,
                target_issue=target_issue,
                prediction_created=True,
                dashboard_ready=bool(_LATEST_SYNC_STATE.get("database_saved") and _LATEST_SYNC_STATE.get("analysis_created")),
                failure_stage=None,
                failure_reason=None,
                next_retry_expected_at=None,
                last_attempt_at=result.get("last_attempt_at"),
                attempt_count=result.get("attempt_count", _LATEST_SYNC_STATE.get("attempt_count")),
                prediction_reconcile=result.get("prediction_reconcile"),
            )
    finally:
        with _RECONCILE_LOCK:
            _RECONCILE_IN_FLIGHT.discard(source_issue)


def _queue_prediction_reconcile(latest: dict, source_issue: str, target_issue: str) -> dict[str, Any]:
    with _RECONCILE_LOCK:
        if source_issue in _RECONCILE_IN_FLIGHT:
            with _STATE_LOCK:
                attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0)
            return {
                "status": "queued",
                "refresh_status": "queued",
                "based_on_issue": source_issue,
                "target_issue": target_issue,
                "reason": "reconcile_already_running",
                "attempt_count": attempt_count,
            }
        _RECONCILE_IN_FLIGHT.add(source_issue)
    with _STATE_LOCK:
        attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0) + 1
    queued_at = _now()
    _update_state(
        source_issue=source_issue,
        target_issue=target_issue,
        last_attempt_at=queued_at,
        attempt_count=attempt_count,
        failure_stage="prediction",
        failure_reason="latest_prediction_missing",
        next_retry_expected_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        prediction_reconcile={
            "status": "queued",
            "refresh_status": "queued",
            "based_on_issue": source_issue,
            "target_issue": target_issue,
            "queued_at": queued_at,
        },
    )
    _RECONCILE_EXECUTOR.submit(_run_prediction_reconcile, dict(latest), source_issue, target_issue)
    return {
        "status": "queued",
        "refresh_status": "queued",
        "based_on_issue": source_issue,
        "target_issue": target_issue,
        "queued_at": queued_at,
        "attempt_count": attempt_count,
    }


def get_latest_sync_snapshot() -> dict[str, Any]:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    with _STATE_LOCK:
        cached = _LATEST_SYNC_CACHE.get("snapshot")
        if (
            isinstance(cached, dict)
            and cached.get("sync_status") == "synced"
            and float(_LATEST_SYNC_CACHE.get("expires_at") or 0) > started
        ):
            snapshot = deepcopy(cached)
            snapshot["timings_ms"] = {
                **(snapshot.get("timings_ms") or {}),
                "cache_hit_ms": 0.0,
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            }
            return snapshot

    mark = time.perf_counter()
    sync_status = get_latest_official_draw_sync_status()
    latest = (sync_status or {}).get("draw")
    timings["read_latest_draw_ms"] = round((time.perf_counter() - mark) * 1000, 2)
    source_issue = str(latest["issue"]) if latest and latest.get("issue") else None
    target_issue = (sync_status or {}).get("target_issue") or (_next_issue(source_issue) if source_issue else None)
    prediction_created = False
    analysis_created = False
    reconcile: dict[str, Any] = {}
    if source_issue:
        analysis_created = bool((sync_status or {}).get("analysis_exists"))
        timings["analysis_lookup_ms"] = 0.0
        prediction_created = bool((sync_status or {}).get("prediction_exists"))
        timings["prediction_lookup_ms"] = 0.0
        if is_complete_official_draw(latest) and analysis_created and not prediction_created and target_issue:
            reconcile = _queue_prediction_reconcile(latest, source_issue, target_issue)
    with _STATE_LOCK:
        detected = source_issue or _LATEST_SYNC_STATE.get("official_detected_issue")
        existing_attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0)
        existing_detected_at = _LATEST_SYNC_STATE.get("detected_at")
        existing_last_attempt_at = _LATEST_SYNC_STATE.get("last_attempt_at")
    database_saved = is_complete_official_draw(latest)
    dashboard_ready = database_saved and analysis_created and prediction_created
    failure_stage = reconcile.get("failure_stage")
    failure_reason = reconcile.get("failure_reason")
    if database_saved and analysis_created and not prediction_created and not failure_stage:
        failure_stage = None if reconcile else "prediction"
        failure_reason = "latest_prediction_queued" if reconcile else "latest_prediction_missing"
    elif database_saved and not analysis_created:
        failure_stage = "analysis"
        failure_reason = "latest_analysis_missing"
    elif not database_saved and detected:
        failure_stage = "database"
        failure_reason = "latest_official_draw_missing_or_incomplete"
    stages = _snapshot_stages(
        database_saved=database_saved,
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        dashboard_ready=dashboard_ready,
        failure_stage=failure_stage,
        failure_reason=failure_reason,
    )
    timings["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    snapshot = _update_state(
        official_detected_issue=detected,
        source_issue=source_issue,
        database_latest_issue=(latest or {}).get("issue"),
        dashboard_latest_issue=(latest or {}).get("issue"),
        latest_saved_at=(latest or {}).get("updated_at") or (latest or {}).get("created_at"),
        draw_time=(latest or {}).get("draw_time"),
        numbers_count=len(_valid_numbers((latest or {}).get("numbers"))),
        database_saved=database_saved,
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        dashboard_ready=dashboard_ready,
        target_issue=target_issue,
        detected_at=existing_detected_at or (_now() if detected else None),
        last_attempt_at=reconcile.get("last_attempt_at") or existing_last_attempt_at,
        attempt_count=reconcile.get("attempt_count", existing_attempt_count),
        failure_stage=failure_stage,
        failure_reason=failure_reason,
        next_retry_expected_at=reconcile.get("next_retry_expected_at"),
        stages=stages,
        prediction_reconcile=reconcile or None,
        timings_ms=timings,
        historical_catchup_enabled=HISTORICAL_CATCHUP_ENABLED,
        latest_issue_priority=LATEST_ISSUE_PRIORITY,
    )
    if snapshot.get("sync_status") == "synced":
        with _STATE_LOCK:
            _LATEST_SYNC_CACHE["snapshot"] = deepcopy(snapshot)
            _LATEST_SYNC_CACHE["expires_at"] = time.perf_counter() + _LATEST_SYNC_CACHE_TTL_SECONDS
    return snapshot


def _failure(source_issue: str | None, stage: str, reason: str, detected_at: str | None, attempt_count: int) -> dict[str, Any]:
    prediction_target_issue = _next_issue(source_issue)
    return _update_state(
        source_issue=source_issue,
        target_issue=prediction_target_issue,
        detected_at=detected_at,
        last_attempt_at=_now(),
        attempt_count=attempt_count,
        failure_stage=stage,
        failure_reason=reason,
        next_retry_expected_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        database_saved=False,
        stages=_snapshot_stages(
            database_saved=False,
            analysis_created=False,
            prediction_created=False,
            dashboard_ready=False,
            failure_stage=stage,
            failure_reason=reason,
        ),
    )


def process_latest_official_draw() -> dict[str, Any]:
    start = time.perf_counter()
    detected_at = _now()
    with _STATE_LOCK:
        attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0) + 1

    source_draw = _latest_draw_from_source()
    if not source_draw:
        return _failure(None, "detect", "official_latest_issue_unavailable", detected_at, attempt_count)

    source_issue = str(source_draw.get("issue"))
    prediction_target_issue = _next_issue(source_issue)
    existing = get_official_draw_by_issue(source_issue)
    if is_complete_official_draw(existing):
        saved_draw = existing
        save_result = {"status": "ok", "saved": 0, "storage": "existing"}
    elif is_complete_official_draw(source_draw):
        source_draw["verification_status"] = "validated"
        source_draw["fetched_at"] = detected_at
        save_result = save_official_draws([source_draw])
        if save_result.get("status") != "ok" or int(save_result.get("saved") or 0) < 1:
            return _failure(source_issue, "database_saved", str(save_result.get("error") or save_result), detected_at, attempt_count)
        saved_draw = get_official_draw_by_issue(source_issue)
        if not is_complete_official_draw(saved_draw):
            return _failure(source_issue, "database_confirmed", "saved_draw_not_confirmed", detected_at, attempt_count)
    else:
        return _failure(source_issue, "validated", "invalid_or_incomplete_official_draw", detected_at, attempt_count)

    analysis_result: dict[str, Any]
    try:
        analysis_result = save_analysis_history(saved_draw)
    except Exception as exc:
        logger.exception("latest sync analysis failed")
        analysis_result = {"status": "error", "error": str(exc)}

    lifecycle: dict[str, Any]
    try:
        from services.prediction_lifecycle_orchestrator import process_official_draw_lifecycle

        lifecycle = process_official_draw_lifecycle(
            saved_draw,
            source="official_collector",
            trigger="official_draw_saved",
            caller="process_latest_official_draw",
            create_next_prediction=True,
        )
    except Exception as exc:
        logger.exception("latest sync downstream lifecycle failed")
        lifecycle = {"status": "error", "message": str(exc)}

    analysis_created = analysis_result.get("status") == "ok" or _analysis_exists(source_issue)
    prediction_payload = lifecycle.get("prediction") if isinstance(lifecycle, dict) else {}
    prediction_created = (
        (prediction_payload or {}).get("status") in {"created", "already_exists", "ok"}
        or _prediction_exists_for_latest(source_issue)
    )
    completed = analysis_created and prediction_created
    stages = _snapshot_stages(
        database_saved=True,
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        dashboard_ready=completed,
        failure_stage=None if completed else "downstream",
        failure_reason=None if completed else "analysis_or_prediction_pending",
    )
    snapshot = _update_state(
        official_detected_issue=source_issue,
        source_issue=source_issue,
        database_latest_issue=source_issue,
        dashboard_latest_issue=source_issue,
        latest_saved_at=(saved_draw or {}).get("updated_at") or (saved_draw or {}).get("created_at") or _now(),
        draw_time=(saved_draw or {}).get("draw_time"),
        numbers_count=len(_valid_numbers((saved_draw or {}).get("numbers"))),
        database_saved=True,
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        dashboard_ready=completed,
        target_issue=prediction_target_issue,
        detected_at=detected_at,
        last_attempt_at=_now(),
        attempt_count=attempt_count,
        failure_stage=None if completed else "downstream",
        failure_reason=None if completed else "analysis_or_prediction_pending",
        next_retry_expected_at=None if completed else (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        stages=stages,
    )
    snapshot.update(
        {
            "status": "ok" if completed else "partial",
            "saved": save_result,
            "analysis": analysis_result,
            "lifecycle": lifecycle,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "exit_reason": "completed" if completed else "partial",
        }
    )
    return snapshot
