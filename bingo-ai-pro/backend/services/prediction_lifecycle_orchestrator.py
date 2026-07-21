from __future__ import annotations

import json
import logging
import time
from typing import Any

from services.prediction_lifecycle import verify_prediction
from services.prediction_refresh import refresh_next_prediction_for_draw

logger = logging.getLogger(__name__)


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() else None


def _numbers(draw: dict) -> list[int]:
    result: list[int] = []
    for value in draw.get("numbers") or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return sorted(result)


def _record_event(
    event_type: str,
    *,
    status: str,
    issue: str | None,
    source: str,
    trigger: str,
    caller: str,
    start: float,
    reason: str | None = None,
) -> None:
    try:
        from services.operations_center import record_operation_event

        record_operation_event(
            component="prediction_lifecycle",
            event_type=event_type,
            status=status,
            issue=issue,
            message=json.dumps(
                {
                    "event_type": event_type,
                    "issue": issue,
                    "source": source,
                    "trigger": trigger,
                    "caller": caller,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
            duration_ms=_duration_ms(start),
            error_type=reason if status in ("warning", "error") else None,
        )
    except Exception:
        logger.exception("failed to record prediction lifecycle event")


def process_official_draw_lifecycle(
    official_draw: dict | None,
    *,
    source: str = "official_collector",
    trigger: str = "official_draw_saved",
    caller: str = "official_draw_lifecycle",
    create_next_prediction: bool = True,
) -> dict:
    start = time.perf_counter()
    issue = _valid_issue((official_draw or {}).get("issue")) if official_draw else None
    numbers = _numbers(official_draw or {})
    if not issue or len(numbers) != 20:
        reason = "missing_or_incomplete_official_draw"
        _record_event(
            "official_draw_lifecycle_skipped",
            status="warning",
            issue=issue,
            source=source,
            trigger=trigger,
            caller=caller,
            start=start,
            reason=reason,
        )
        return {
            "status": "skipped",
            "reason": reason,
            "issue": issue,
            "verification": {"status": "skipped", "reason": reason},
            "learning": {"status": "skipped", "reason": reason},
            "prediction": {"status": "skipped", "reason": reason},
            "elapsed_ms": _duration_ms(start),
        }

    _record_event(
        "official_draw_lifecycle_started",
        status="ok",
        issue=issue,
        source=source,
        trigger=trigger,
        caller=caller,
        start=start,
    )

    verification = verify_prediction(
        {
            "issue": issue,
            "numbers": numbers,
            "super_number": official_draw.get("super_number"),
        }
    )

    try:
        from database.analysis_store import save_analysis_history

        analysis = save_analysis_history({**official_draw, "issue": issue, "numbers": numbers})
    except Exception as exc:
        logger.exception("lifecycle analysis save failed")
        analysis = {"status": "error", "message": str(exc)}

    try:
        from services.learning_engine import evaluate_verified_issue

        learning = evaluate_verified_issue(issue)
    except Exception as exc:
        logger.exception("lifecycle learning evaluation failed")
        learning = {"status": "error", "message": str(exc)}

    if create_next_prediction:
        prediction = refresh_next_prediction_for_draw({**official_draw, "issue": issue, "numbers": numbers})
    else:
        prediction = {"status": "skipped", "reason": "create_next_prediction_disabled"}

    status = "ok"
    if (
        verification.get("status") == "failed"
        or analysis.get("status") == "error"
        or learning.get("status") == "error"
        or prediction.get("status") == "failed"
    ):
        status = "error"
    _record_event(
        "official_draw_lifecycle_completed",
        status=status,
        issue=issue,
        source=source,
        trigger=trigger,
        caller=caller,
        start=start,
    )
    return {
        "status": status,
        "issue": issue,
        "verification": verification,
        "analysis": analysis,
        "learning": learning,
        "prediction": prediction,
        "elapsed_ms": _duration_ms(start),
    }
