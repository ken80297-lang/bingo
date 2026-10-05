from __future__ import annotations

import json
import logging
import os
import atexit
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from services.prediction_lifecycle import verify_prediction
from services.prediction_refresh import refresh_next_prediction_for_draw

logger = logging.getLogger(__name__)
_LEARNING_EXECUTOR: ThreadPoolExecutor | None = None
_LEARNING_EXECUTOR_LOCK = threading.Lock()
_LEARNING_BACKGROUND_ACCEPTING = True


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _get_learning_executor() -> ThreadPoolExecutor | None:
    global _LEARNING_EXECUTOR
    if not _LEARNING_BACKGROUND_ACCEPTING or sys.is_finalizing():
        return None
    with _LEARNING_EXECUTOR_LOCK:
        if not _LEARNING_BACKGROUND_ACCEPTING or sys.is_finalizing():
            return None
        if _LEARNING_EXECUTOR is None:
            _LEARNING_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prediction-learning")
        return _LEARNING_EXECUTOR


def shutdown_lifecycle_background_tasks(*, wait: bool = False) -> dict:
    global _LEARNING_EXECUTOR
    global _LEARNING_BACKGROUND_ACCEPTING
    _LEARNING_BACKGROUND_ACCEPTING = False
    with _LEARNING_EXECUTOR_LOCK:
        executor = _LEARNING_EXECUTOR
        _LEARNING_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)
    return {"status": "stopped", "executor_shutdown": executor is not None}


def reset_lifecycle_background_tasks_for_tests() -> dict:
    global _LEARNING_BACKGROUND_ACCEPTING
    _LEARNING_BACKGROUND_ACCEPTING = True
    return {"status": "running", "accepting": True}


atexit.register(shutdown_lifecycle_background_tasks)


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


def _run_learning_evaluation(issue: str) -> dict:
    from services.learning_engine import evaluate_verified_issue

    return evaluate_verified_issue(issue)


def _submit_learning_evaluation(issue: str) -> dict:
    executor = _get_learning_executor()
    if executor is None:
        return {"status": "skipped", "reason": "background_stopped", "issue": issue, "async": True}
    start = time.perf_counter()
    try:
        future = executor.submit(_run_learning_evaluation, issue)
    except RuntimeError as exc:
        if "shutdown" not in str(exc).lower():
            raise
        return {"status": "skipped", "reason": "background_stopped", "issue": issue, "async": True}

    def _done(completed) -> None:
        try:
            result = completed.result()
            status = "ok" if result.get("status") in {"ok", "pending_official", "missing_snapshot"} else "warning"
            _record_event(
                "learning_evaluation_background_completed",
                status=status,
                issue=issue,
                source="learning",
                trigger="background",
                caller="prediction_lifecycle",
                start=start,
                reason=result.get("status"),
            )
        except Exception as exc:
            logger.exception("background learning evaluation failed issue=%s", issue)
            _record_event(
                "learning_evaluation_background_failed",
                status="error",
                issue=issue,
                source="learning",
                trigger="background",
                caller="prediction_lifecycle",
                start=start,
                reason=str(exc),
            )

    future.add_done_callback(_done)
    return {"status": "queued", "issue": issue, "async": True}


def process_official_draw_lifecycle(
    official_draw: dict | None,
    *,
    source: str = "official_collector",
    trigger: str = "official_draw_saved",
    caller: str = "official_draw_lifecycle",
    create_next_prediction: bool = True,
    analysis_result: dict | None = None,
    learning_synchronous: bool = False,
) -> dict:
    start = time.perf_counter()
    timings: dict[str, float] = {}
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

    mark = time.perf_counter()
    verification = verify_prediction(
        {
            "issue": issue,
            "numbers": numbers,
            "super_number": official_draw.get("super_number"),
        }
    )
    timings["verification_ms"] = _duration_ms(mark)
    mark = time.perf_counter()
    cron_core_only = os.getenv("AI_LIFECYCLE_CRON_CORE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
    if cron_core_only:
        shadow_dynamic = {"status": "deferred", "reason": "cron_core_only"}
        timings["shadow_dynamic_ms"] = _duration_ms(mark)
    else:
        try:
            from services.shadow_dynamic_observer import verify_for_official_draw

            shadow_dynamic = verify_for_official_draw({**official_draw, "issue": issue, "numbers": numbers})
            timings["shadow_dynamic_ms"] = _duration_ms(mark)
        except Exception as exc:
            logger.exception("shadow dynamic observer verification failed")
            shadow_dynamic = {"status": "error", "message": str(exc)}
            timings["shadow_dynamic_ms"] = _duration_ms(mark)

    mark = time.perf_counter()
    if analysis_result and analysis_result.get("status") == "ok":
        analysis = {**analysis_result, "reused": True}
    else:
        try:
            from database.analysis_store import save_analysis_history

            analysis = save_analysis_history({**official_draw, "issue": issue, "numbers": numbers})
        except Exception as exc:
            logger.exception("lifecycle analysis save failed")
            analysis = {"status": "error", "message": str(exc)}
    timings["analysis_ms"] = _duration_ms(mark)

    # Create the next prediction before learning. The official collector can run
    # close to the next five-minute draw boundary, and learning is not required
    # to build the prediction for this already-saved official issue. Keeping
    # learning ahead of prediction can therefore turn an otherwise valid
    # pre-draw prediction into a post-draw one.
    mark = time.perf_counter()
    if create_next_prediction:
        prediction_draw = {**official_draw, "issue": issue, "numbers": numbers}
        if analysis.get("record"):
            prediction_draw["analysis_record"] = analysis.get("record")
        prediction = refresh_next_prediction_for_draw(prediction_draw)
    else:
        prediction = {"status": "skipped", "reason": "create_next_prediction_disabled"}
    timings["prediction_ms"] = _duration_ms(mark)

    mark = time.perf_counter()
    if learning_synchronous:
        try:
            learning = _run_learning_evaluation(issue)
        except Exception as exc:
            logger.exception("lifecycle learning evaluation failed")
            learning = {"status": "error", "message": str(exc)}
    else:
        learning = _submit_learning_evaluation(issue)
    timings["learning_ms"] = _duration_ms(mark)

    status = "ok"
    if (
        verification.get("status") == "failed"
        or analysis.get("status") == "error"
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
        "shadow_dynamic": shadow_dynamic,
        "analysis": analysis,
        "learning": learning,
        "prediction": prediction,
        "timings_ms": {**timings, "total_ms": _duration_ms(start)},
        "elapsed_ms": _duration_ms(start),
    }
