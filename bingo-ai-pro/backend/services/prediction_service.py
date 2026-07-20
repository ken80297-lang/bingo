from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from database.prediction_history_store import get_prediction_history_records, save_prediction_history
from services.next_prediction_center import build_prediction_history_record
from services.recommendation_center import calculate_recommendation

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


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
    for item in get_prediction_history_records(500):
        if str(item.get("prediction_issue") or "") != str(target_issue):
            continue
        if str(item.get("issue") or "") == str(based_on_issue):
            return item
    return None


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
    source = str(source or "unknown")
    trigger = str(trigger or "unknown")
    based_on = _valid_issue(based_on_issue)
    target = _valid_issue(target_issue) if target_issue is not None else None
    if based_on and target is None:
        target = _next_issue(based_on)

    _record_event(
        event_type="prediction_create_started",
        status="ok",
        based_on_issue=based_on,
        target_issue=target,
        source=source,
        trigger=trigger,
        started_at=started_at,
    )

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
        }

    if not based_on:
        return skipped("based_on_missing")
    if not target:
        return skipped("target_unconfirmed")
    try:
        if int(target) != int(based_on) + 1:
            return skipped("target_unconfirmed")
    except Exception:
        return skipped("target_unconfirmed")

    existing = _existing_prediction(based_on, target)
    if existing and not force:
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
        }

    recommendation_result = calculate_recommendation(
        based_on,
        target,
        context={
            "source": source,
            "trigger": trigger,
            "collector_metadata": collector_metadata or {},
            "prediction_service": True,
            "ensure_simulation": False,
        },
    )
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
        }

    recommendation = recommendation_result.get("recommendation") or {}
    recommendation["issue"] = based_on
    recommendation["target_issue"] = target
    record = build_prediction_history_record(recommendation)
    recommended = _numbers((record or {}).get("recommend_numbers"))
    if len(recommended) != 20:
        return skipped("insufficient_recommendations", len(recommended))
    record["prediction_status"] = "waiting_draw"
    record["learning_used"] = False
    record["source"] = source
    record["trigger"] = trigger
    record["collector_metadata"] = collector_metadata or {}
    saved = save_prediction_history(record, caller_context="prediction_service")
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
    }
