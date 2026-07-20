from __future__ import annotations

import logging
import os
import threading
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.analysis_store import get_analysis_history, save_analysis_history
from database.official_draw_store import (
    get_latest_official_draw,
    get_official_draw_by_issue,
    save_official_draws,
)
from database.prediction_history_store import get_latest_prediction_history
from services.collector_runtime import update_collector_runtime

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
HISTORICAL_CATCHUP_ENABLED = os.getenv("HISTORICAL_CATCHUP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
LATEST_ISSUE_PRIORITY = os.getenv("LATEST_ISSUE_PRIORITY", "true").lower() not in {"0", "false", "no", "off"}
LATEST_SYNC_STALE_SECONDS = 12 * 60

_STATE_LOCK = threading.RLock()
_LATEST_SYNC_STATE: dict[str, Any] = {
    "official_detected_issue": None,
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


def _prediction_exists_for_latest(issue: str) -> bool:
    latest = get_latest_prediction_history()
    if not latest:
        return False
    target_issue = _issue_int(latest.get("prediction_issue"))
    based_on = _issue_int(latest.get("issue"))
    issue_number = _issue_int(issue)
    return bool(issue_number is not None and based_on == issue_number and target_issue == issue_number + 1)


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


def get_latest_sync_snapshot() -> dict[str, Any]:
    latest = get_latest_official_draw()
    prediction_created = False
    analysis_created = False
    if latest and latest.get("issue"):
        analysis_created = _analysis_exists(str(latest["issue"]))
        prediction_created = _prediction_exists_for_latest(str(latest["issue"]))
    with _STATE_LOCK:
        detected = _LATEST_SYNC_STATE.get("official_detected_issue") or (latest or {}).get("issue")
    return _update_state(
        official_detected_issue=detected,
        database_latest_issue=(latest or {}).get("issue"),
        dashboard_latest_issue=(latest or {}).get("issue"),
        latest_saved_at=(latest or {}).get("updated_at") or (latest or {}).get("created_at"),
        draw_time=(latest or {}).get("draw_time"),
        numbers_count=len(_valid_numbers((latest or {}).get("numbers"))),
        database_saved=is_complete_official_draw(latest),
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        historical_catchup_enabled=HISTORICAL_CATCHUP_ENABLED,
        latest_issue_priority=LATEST_ISSUE_PRIORITY,
    )


def _failure(target_issue: str | None, stage: str, reason: str, detected_at: str | None, attempt_count: int) -> dict[str, Any]:
    return _update_state(
        target_issue=target_issue,
        detected_at=detected_at,
        last_attempt_at=_now(),
        attempt_count=attempt_count,
        failure_stage=stage,
        failure_reason=reason,
        next_retry_expected_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        database_saved=False,
        stages={
            "detected": bool(target_issue),
            "fetched": stage not in {"detect", "fetch"},
            "validated": False,
            "database_saved": False,
            "analysis_created": False,
            "prediction_created": False,
            "dashboard_available": False,
            "completed": False,
            "partial": False,
            "failed": True,
        },
    )


def process_latest_official_draw() -> dict[str, Any]:
    start = time.perf_counter()
    detected_at = _now()
    with _STATE_LOCK:
        attempt_count = int(_LATEST_SYNC_STATE.get("attempt_count") or 0) + 1

    source_draw = _latest_draw_from_source()
    if not source_draw:
        return _failure(None, "detect", "official_latest_issue_unavailable", detected_at, attempt_count)

    target_issue = str(source_draw.get("issue"))
    existing = get_official_draw_by_issue(target_issue)
    if is_complete_official_draw(existing):
        saved_draw = existing
        save_result = {"status": "ok", "saved": 0, "storage": "existing"}
    elif is_complete_official_draw(source_draw):
        source_draw["verification_status"] = "validated"
        source_draw["fetched_at"] = detected_at
        save_result = save_official_draws([source_draw])
        if save_result.get("status") != "ok" or int(save_result.get("saved") or 0) < 1:
            return _failure(target_issue, "database_saved", str(save_result.get("error") or save_result), detected_at, attempt_count)
        saved_draw = get_official_draw_by_issue(target_issue)
        if not is_complete_official_draw(saved_draw):
            return _failure(target_issue, "database_confirmed", "saved_draw_not_confirmed", detected_at, attempt_count)
    else:
        return _failure(target_issue, "validated", "invalid_or_incomplete_official_draw", detected_at, attempt_count)

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

    analysis_created = analysis_result.get("status") == "ok" or _analysis_exists(target_issue)
    prediction_payload = lifecycle.get("prediction") if isinstance(lifecycle, dict) else {}
    prediction_created = (
        (prediction_payload or {}).get("status") in {"created", "already_exists", "ok"}
        or _prediction_exists_for_latest(target_issue)
    )
    completed = analysis_created and prediction_created
    snapshot = _update_state(
        official_detected_issue=target_issue,
        database_latest_issue=target_issue,
        dashboard_latest_issue=target_issue,
        latest_saved_at=(saved_draw or {}).get("updated_at") or (saved_draw or {}).get("created_at") or _now(),
        draw_time=(saved_draw or {}).get("draw_time"),
        numbers_count=len(_valid_numbers((saved_draw or {}).get("numbers"))),
        database_saved=True,
        analysis_created=analysis_created,
        prediction_created=prediction_created,
        target_issue=target_issue,
        detected_at=detected_at,
        last_attempt_at=_now(),
        attempt_count=attempt_count,
        failure_stage=None if completed else "downstream",
        failure_reason=None if completed else "analysis_or_prediction_pending",
        next_retry_expected_at=None if completed else (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        stages={
            "detected": True,
            "fetched": True,
            "validated": True,
            "database_saved": True,
            "analysis_created": analysis_created,
            "prediction_created": prediction_created,
            "dashboard_available": True,
            "completed": completed,
            "partial": not completed,
            "failed": False,
        },
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
