from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone
from typing import Any

from database.prediction_history_store import get_prediction_for_source_target

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


def _next_issue(source_issue: str) -> str:
    return str(int(source_issue) + 1)


def _numbers(draw: dict) -> list[int]:
    result: list[int] = []
    for value in draw.get("numbers") or draw.get("official_numbers") or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return result


def _existing_prediction(source_issue: str, target_issue: str) -> dict | None:
    return get_prediction_for_source_target(source_issue, target_issue)


def _lag_issues(source_issue: str, target_issue: str | None) -> int | None:
    try:
        if not target_issue:
            return None
        return max(int(source_issue) + 1 - int(target_issue), 0)
    except Exception:
        return None


def _record_refresh_event(payload: dict, start: float) -> None:
    try:
        from services.operations_center import record_operation_event

        refresh_status = payload.get("refresh_status") or payload.get("status") or "unknown"
        event_status = "ok" if refresh_status in ("ready", "existing") else "warning"
        if refresh_status == "failed":
            event_status = "error"
        record_operation_event(
            component="recommendation",
            event_type="next_prediction_refresh",
            status=event_status,
            issue=payload.get("based_on_issue"),
            message=f"next prediction refresh {refresh_status}",
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
    except Exception:
        logger.exception("failed to record next prediction refresh event")


def _record_trigger_event(
    event_type: str,
    *,
    status: str,
    based_on_issue: str | None,
    proposed_target_issue: str | None,
    source: str = "official_collector",
    trigger: str = "official_draw_saved",
    skip_reason: str | None = None,
    caller: str = "prediction_refresh",
    start: float | None = None,
) -> None:
    try:
        from services.operations_center import record_operation_event

        payload = {
            "event_type": event_type,
            "based_on_issue": based_on_issue,
            "proposed_target_issue": proposed_target_issue,
            "source": source,
            "trigger": trigger,
            "status": status,
            "skip_reason": skip_reason,
            "caller": caller,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2) if start else None,
        }
        record_operation_event(
            component="prediction",
            event_type=event_type,
            status=status,
            issue=based_on_issue,
            message=json.dumps(payload, ensure_ascii=False),
            duration_ms=payload["duration_ms"],
            error_type=skip_reason if status in ("warning", "error") else None,
        )
    except Exception:
        logger.exception("failed to record prediction trigger event")


def refresh_next_prediction_for_draw(draw: dict) -> dict:
    start = time.perf_counter()
    source_issue = _valid_issue((draw or {}).get("issue"))
    proposed_target = _next_issue(source_issue) if source_issue else None
    _record_trigger_event(
        "refresh_next_prediction_started",
        status="ok",
        based_on_issue=source_issue,
        proposed_target_issue=proposed_target,
        start=start,
    )
    if not source_issue:
        payload = {
            "status": "skipped_incomplete_draw",
            "refresh_status": "skipped_incomplete_draw",
            "refresh_reason": "missing production issue",
            "last_refresh_attempt": _now(),
            "last_refresh_success": None,
            "based_on_issue": None,
            "target_issue": None,
            "is_stale": True,
            "lag_issues": None,
            "elapsed_ms": 0,
        }
        _record_trigger_event(
            "prediction_trigger_skipped",
            status="warning",
            based_on_issue=None,
            proposed_target_issue=None,
            skip_reason=payload["refresh_reason"],
            start=start,
        )
        _record_refresh_event(payload, start)
        return payload

    draw_numbers = _numbers(draw)
    target_issue = proposed_target
    attempt = _now()
    base_payload = {
        "last_refresh_attempt": attempt,
        "based_on_issue": source_issue,
        "target_issue": target_issue,
        "is_stale": False,
        "lag_issues": 0,
    }

    if len(draw_numbers) != 20:
        payload = {
            "status": "skipped_incomplete_draw",
            "refresh_status": "skipped_incomplete_draw",
            "refresh_reason": f"incomplete draw numbers: {len(draw_numbers)}",
            "last_refresh_success": None,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            **base_payload,
        }
        _record_trigger_event(
            "prediction_trigger_skipped",
            status="warning",
            based_on_issue=source_issue,
            proposed_target_issue=target_issue,
            skip_reason=payload["refresh_reason"],
            start=start,
        )
        _record_refresh_event(payload, start)
        return payload

    try:
        existing = _existing_prediction(source_issue, target_issue)
        if existing:
            payload = {
                "status": "existing",
                "refresh_status": "existing",
                "refresh_reason": None,
                "last_refresh_success": existing.get("predict_time") or existing.get("created_at"),
                "prediction_id": existing.get("id"),
                "prediction_status": existing.get("prediction_status"),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                **base_payload,
            }
            _record_trigger_event(
                "prediction_trigger_skipped",
                status="ok",
                based_on_issue=source_issue,
                proposed_target_issue=target_issue,
                skip_reason="existing_prediction",
                start=start,
            )
            _record_refresh_event(payload, start)
            return payload

        from services.prediction_service import create_for_official_draw, recover_prediction_lock_for_target

        _record_trigger_event(
            "prediction_service_called",
            status="ok",
            based_on_issue=source_issue,
            proposed_target_issue=target_issue,
            start=start,
        )
        service_result = create_for_official_draw(
            source_issue,
            source="official_collector",
            trigger="official_draw_saved",
            target_issue=target_issue,
            collector_metadata={"draw_issue": source_issue, "draw_number_count": len(draw_numbers)},
        )
        if service_result.get("status") == "already_running":
            recovery = recover_prediction_lock_for_target(
                source_issue,
                target_issue,
                reason="prediction_refresh_already_running_recovery",
            )
            existing_after_recovery = _existing_prediction(source_issue, target_issue)
            if existing_after_recovery:
                service_result = {
                    "status": "already_exists",
                    "based_on_issue": source_issue,
                    "target_issue": target_issue,
                    "prediction_id": existing_after_recovery.get("id"),
                    "recommended_count": len(existing_after_recovery.get("recommend_numbers") or []),
                    "prediction_status": existing_after_recovery.get("prediction_status"),
                    "lock_recovery": recovery,
                }
            elif recovery.get("status") == "recovered":
                service_result = create_for_official_draw(
                    source_issue,
                    source="official_collector",
                    trigger="official_draw_saved",
                    target_issue=target_issue,
                    collector_metadata={
                        "draw_issue": source_issue,
                        "draw_number_count": len(draw_numbers),
                        "lock_recovery": recovery.get("status"),
                    },
                )
                service_result["lock_recovery"] = recovery
        status = service_result.get("status")
        refresh_ready = status in ("created", "already_exists")
        payload = {
            "status": status,
            "refresh_status": "ready" if refresh_ready else "failed",
            "refresh_reason": None if refresh_ready else service_result.get("skip_reason") or service_result.get("error"),
            "last_refresh_success": _now() if refresh_ready else None,
            "prediction_history": service_result,
            "recommendation_status": "single_entry_prediction_service",
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            **base_payload,
        }
        if not refresh_ready:
            _record_trigger_event(
                "prediction_trigger_skipped",
                status="warning",
                based_on_issue=source_issue,
                proposed_target_issue=target_issue,
                skip_reason=payload["refresh_reason"],
                start=start,
            )
        _record_refresh_event(payload, start)
        return payload
    except Exception as exc:
        logger.exception("next prediction refresh failed")
        payload = {
            "status": "failed",
            "refresh_status": "failed",
            "refresh_reason": str(exc),
            "last_refresh_success": None,
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            **base_payload,
        }
        _record_trigger_event(
            "prediction_trigger_failed",
            status="error",
            based_on_issue=source_issue,
            proposed_target_issue=target_issue,
            skip_reason=str(exc),
            start=start,
        )
        _record_refresh_event(payload, start)
        return payload


def ensure_next_prediction(latest_draw: dict | None) -> dict:
    start = time.perf_counter()
    based_on_issue = _valid_issue((latest_draw or {}).get("issue")) if latest_draw else None
    _record_trigger_event(
        "ensure_next_prediction_started",
        status="ok" if latest_draw else "warning",
        based_on_issue=based_on_issue,
        proposed_target_issue=_next_issue(based_on_issue) if based_on_issue else None,
        skip_reason=None if latest_draw else "missing_latest_draw",
        caller="ensure_next_prediction",
        start=start,
    )
    if not latest_draw:
        return {
            "status": "skipped_incomplete_draw",
            "refresh_status": "skipped_incomplete_draw",
            "refresh_reason": "missing latest draw",
            "last_refresh_attempt": _now(),
            "last_refresh_success": None,
            "based_on_issue": None,
            "target_issue": None,
            "is_stale": True,
            "lag_issues": None,
        }
    return refresh_next_prediction_for_draw(latest_draw)


def recover_latest_prediction() -> dict:
    try:
        from database.official_draw_store import get_latest_official_draw

        return ensure_next_prediction(get_latest_official_draw())
    except Exception as exc:
        logger.exception("latest prediction recovery failed")
        return {
            "status": "failed",
            "refresh_status": "failed",
            "refresh_reason": str(exc),
            "last_refresh_attempt": _now(),
            "last_refresh_success": None,
            "based_on_issue": None,
            "target_issue": None,
            "is_stale": True,
            "lag_issues": None,
        }


def prediction_refresh_status(latest_draw: dict | None, latest_prediction: dict | None) -> dict:
    source_issue = _valid_issue((latest_draw or {}).get("issue"))
    target_issue = _valid_issue((latest_prediction or {}).get("prediction_issue"))
    based_on_issue = _valid_issue((latest_prediction or {}).get("issue"))
    expected_target = _next_issue(source_issue) if source_issue else None
    is_stale = bool(source_issue and target_issue and int(target_issue) <= int(source_issue))
    if source_issue and target_issue and target_issue == expected_target and based_on_issue == source_issue:
        status = "ready"
        reason = None
    elif not source_issue:
        status = "skipped_incomplete_draw"
        reason = "missing latest draw"
    elif not target_issue:
        status = "stale"
        reason = "missing latest prediction"
    elif target_issue != expected_target:
        status = "stale"
        reason = "prediction target is not latest draw + 1"
    else:
        status = "stale"
        reason = "prediction based_on_issue is not latest draw"
    return {
        "refresh_status": status,
        "refresh_reason": reason,
        "last_refresh_attempt": None,
        "last_refresh_success": latest_prediction.get("predict_time") if latest_prediction else None,
        "based_on_issue": based_on_issue,
        "target_issue": target_issue,
        "expected_target_issue": expected_target,
        "is_stale": is_stale or status == "stale",
        "lag_issues": _lag_issues(source_issue, target_issue) if source_issue else None,
    }
