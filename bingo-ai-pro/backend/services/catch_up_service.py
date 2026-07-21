from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results, get_last_official_fetch_diagnostics
from database.official_draw_store import (
    get_official_draw_history,
    get_latest_official_draw,
    save_draw_verification,
    save_official_draws,
)
from services.operations_center import record_operation_event
from services.official_verification import run_official_verification
from services.collector_runtime import (
    mark_error,
    mark_deadline_exceeded,
    mark_success,
    official_collection_lock,
)

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
LAST_CATCH_UP_RESULT: dict[str, Any] = {
    "status": "unknown",
    "database_latest_issue": None,
    "source_latest_issue": None,
    "lag_count": None,
    "catch_count": 0,
    "success_count": 0,
    "failed_count": 0,
    "elapsed_seconds": 0,
    "last_successful_collect_time": None,
    "last_collect_duration": None,
    "catch_up_available": True,
}
MAX_BATCH_SIZE = int(os.getenv("CATCH_UP_MAX_BATCH_SIZE", "120"))
MAX_SOURCE_PAGES = int(os.getenv("CATCH_UP_MAX_SOURCE_PAGES", "10"))
JOB_TIME_BUDGET_SECONDS = 75
PER_ISSUE_RETRY_LIMIT = 1


def _deadline_exceeded(start: float) -> bool:
    return (time.perf_counter() - start) >= JOB_TIME_BUDGET_SECONDS


def _elapsed_seconds(start: float) -> float:
    return round(time.perf_counter() - start, 3)


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def _issue_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


def _latest_issue_from_draws(draws: list[dict]) -> str | None:
    issues = [_issue_int(draw.get("issue")) for draw in draws]
    issues = [issue for issue in issues if issue is not None]
    return str(max(issues)) if issues else None


def _valid_draw(draw: dict) -> bool:
    issue = _issue_int(draw.get("issue"))
    numbers = draw.get("numbers") or []
    super_number = draw.get("super_number")
    if issue is None or len(numbers) != 20:
        return False
    try:
        normalized = [int(value) for value in numbers]
    except Exception:
        return False
    if len(set(normalized)) != 20:
        return False
    if any(number < 1 or number > 80 for number in normalized):
        return False
    if super_number is not None:
        try:
            super_value = int(super_number)
        except Exception:
            return False
        if super_value < 1 or super_value > 80:
            return False
    return True


def _mark_pending_verification(issue: Any, reason: str) -> None:
    issue_text = str(issue or "").strip()
    if not issue_text:
        return
    try:
        save_draw_verification(
            {
                "issue": issue_text,
                "kuaishou_numbers": [],
                "official_numbers": [],
                "kuaishou_super": None,
                "official_super": None,
                "numbers_match": False,
                "super_match": False,
                "verified": False,
                "status": "pending_verification",
                "verified_at": None,
                "error_message": reason,
            }
        )
    except Exception:
        logger.exception("failed to mark pending verification for issue %s", issue_text)


def get_database_latest_issue() -> str | None:
    try:
        latest = get_latest_official_draw()
        return str(latest.get("issue")) if latest and latest.get("issue") else None
    except Exception:
        logger.exception("failed to load database latest official issue")
        return None


def fetch_source_today_draws(max_pages: int = 3, page_size: int = 100) -> list[dict]:
    collected: dict[str, dict] = {}
    query_date = _today_taipei()
    page_size = max(1, min(int(page_size or 20), 100))
    for page in range(1, max(1, int(max_pages or 1)) + 1):
        draws = fetch_official_bingo_results(query_date, page_num=page, page_size=page_size)
        if not draws:
            break
        for draw in draws:
            issue = str(draw.get("issue") or "")
            if issue:
                collected[issue] = draw
        if len(draws) < page_size:
            break
    return sorted(collected.values(), key=lambda item: int(item.get("issue") or 0))


def _source_fetch_diagnostics() -> list[dict]:
    try:
        return get_last_official_fetch_diagnostics()[-10:]
    except Exception:
        logger.exception("failed to load official source diagnostics")
        return []


def get_source_latest_issue() -> str | None:
    try:
        draws = fetch_official_bingo_results(_today_taipei(), page_num=1, page_size=20)
        return _latest_issue_from_draws(draws)
    except Exception:
        logger.exception("failed to load source latest official issue")
        return None


def _record_event(status: str, issue: str | None, start: float, message: str, error: Exception | None = None) -> None:
    try:
        record_operation_event(
            component="official_catch_up",
            event_type="catch_up",
            status=status,
            issue=issue,
            message=message,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
    except Exception:
        logger.exception("failed to record catch-up operation event")


def _record_structured_event(
    event_type: str,
    *,
    status: str,
    issue: str | None,
    start: float,
    message: str,
    payload: dict | None = None,
    error: Exception | None = None,
) -> None:
    try:
        import json

        record_operation_event(
            component="official_catch_up",
            event_type=event_type,
            status=status,
            issue=issue,
            message=json.dumps(
                {
                    "event_type": event_type,
                    "issue": issue,
                    "status": status,
                    "message": message,
                    **(payload or {}),
                },
                ensure_ascii=False,
            ),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
    except Exception:
        logger.exception("failed to record structured catch-up operation event")


def _known_database_issues(limit: int = 200) -> set[int]:
    try:
        return {
            issue
            for issue in (_issue_int(item.get("issue")) for item in get_official_draw_history(limit))
            if issue is not None
        }
    except Exception:
        logger.exception("failed to load known official issue set for catch-up")
        return set()


def _run_live_downstream_for_draw(draw: dict | None, start: float, caller: str) -> dict:
    issue = str((draw or {}).get("issue") or "").strip() or None
    if not draw or not issue:
        return {
            "verification": {"status": "skipped", "reason": "missing_draw"},
            "prediction": {"status": "skipped", "reason": "missing_draw"},
        }

    _record_structured_event(
        "official_draw_saved",
        status="ok",
        issue=issue,
        start=start,
        message="official draw available for live downstream",
        payload={"caller": caller},
    )

    _record_structured_event(
        "next_prediction_trigger_started",
        status="ok",
        issue=issue,
        start=start,
        message="official catch-up triggering next prediction",
        payload={
            "based_on_issue": issue,
            "proposed_target_issue": str(int(issue) + 1) if issue.isdigit() else None,
            "source": "official_collector",
            "trigger": "official_draw_saved",
            "caller": caller,
        },
    )
    try:
        from services.prediction_lifecycle_orchestrator import process_official_draw_lifecycle

        lifecycle = process_official_draw_lifecycle(
            draw,
            source="official_collector",
            trigger="official_draw_saved",
            caller=caller,
            create_next_prediction=True,
        )
    except Exception as exc:
        logger.exception("catch-up downstream lifecycle failed")
        lifecycle = {"status": "error", "message": str(exc)}

    verification_scan = {"status": "skipped", "reason": "not_started"}
    try:
        verification_scan = run_official_verification(limit=10)
    except Exception as exc:
        logger.exception("catch-up downstream verification scan failed")
        verification_scan = {"status": "error", "message": str(exc)}

    return {
        "lifecycle": lifecycle,
        "verification": lifecycle.get("verification") if isinstance(lifecycle, dict) else {"status": "error"},
        "analysis": lifecycle.get("analysis") if isinstance(lifecycle, dict) else {"status": "error"},
        "verification_scan": verification_scan,
        "learning": lifecycle.get("learning") if isinstance(lifecycle, dict) else {"status": "error"},
        "prediction": lifecycle.get("prediction") if isinstance(lifecycle, dict) else {"status": "error"},
    }


def _log_job_finished(result: dict) -> None:
    logger.info(
        "catch_up_job_finished duration_ms=%s processed_count=%s recovered_count=%s failed_count=%s pending_count=%s exit_reason=%s lock_released=true",
        round(float(result.get("elapsed_seconds") or 0) * 1000, 2),
        result.get("catch_count", 0),
        result.get("success_count", 0),
        result.get("failed_count", 0),
        result.get("pending_verification_count", 0),
        result.get("exit_reason"),
    )


def _empty_result(start: float, database_issue: str | None, source_issue: str | None, status: str = "ok") -> dict:
    database_number = _issue_int(database_issue)
    source_number = _issue_int(source_issue)
    lag = max(0, source_number - database_number) if database_number is not None and source_number is not None else 0
    elapsed = round(time.perf_counter() - start, 3)
    successful_time = datetime.utcnow().isoformat() if status == "ok" else LAST_CATCH_UP_RESULT.get("last_successful_collect_time")
    return {
        "status": status,
        "database_latest_issue": database_issue,
        "source_latest_issue": source_issue,
        "lag_count": lag,
        "start_issue": None,
        "end_issue": None,
        "catch_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "elapsed_seconds": elapsed,
        "last_successful_collect_time": successful_time,
        "last_collect_duration": elapsed,
        "catch_up_available": True,
    }


def catch_up_missing_issues() -> dict:
    start = time.perf_counter()
    logger.info("catch_up_job_started")
    with official_collection_lock("catch_up") as (locked, lock_payload):
        if not locked:
            result = {
                **LAST_CATCH_UP_RESULT,
                **lock_payload,
                "catch_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "exit_reason": "skipped_due_to_lock",
            }
            LAST_CATCH_UP_RESULT.update(result)
            _log_job_finished(result)
            return result
        result = _catch_up_missing_issues_locked(start)
    result = _run_deferred_downstream(result, start)
    _log_job_finished(result)
    return result


def _defer_downstream(result: dict, draw: dict | None, caller: str) -> dict:
    if draw:
        result["_downstream_draw"] = draw
        result["_downstream_caller"] = caller
        result.setdefault("verification", {"status": "queued", "reason": "downstream_after_lock_release"})
        result.setdefault("analysis", {"status": "queued", "reason": "downstream_after_lock_release"})
        result.setdefault("prediction", {"status": "queued", "reason": "downstream_after_lock_release"})
        LAST_CATCH_UP_RESULT.update({key: value for key, value in result.items() if not key.startswith("_")})
    return result


def _run_deferred_downstream(result: dict, start: float) -> dict:
    draw = result.pop("_downstream_draw", None)
    caller = result.pop("_downstream_caller", "catch_up_downstream")
    if not draw:
        return result
    downstream = _run_live_downstream_for_draw(draw, start, caller)
    result["verification"] = downstream.get("verification")
    result["analysis"] = downstream.get("analysis")
    result["prediction"] = downstream.get("prediction")
    result["verification_scan"] = downstream.get("verification_scan")
    result["learning"] = downstream.get("learning")
    LAST_CATCH_UP_RESULT.update(result)
    return result


def _catch_up_missing_issues_locked(start: float) -> dict:
    database_issue = get_database_latest_issue()
    try:
        source_draws = fetch_source_today_draws(max_pages=MAX_SOURCE_PAGES, page_size=100)
        source_issue = _latest_issue_from_draws(source_draws)
        database_number = _issue_int(database_issue)
        source_number = _issue_int(source_issue)
        known_database_issues = _known_database_issues(max(MAX_BATCH_SIZE * 2, 200))

        if source_number is None:
            result = _empty_result(start, database_issue, source_issue, status="warning")
            result["reason"] = "source_latest_issue_unavailable"
            result["source_fetch_diagnostics"] = _source_fetch_diagnostics()
            LAST_CATCH_UP_RESULT.update(result)
            result["exit_reason"] = "source_error"
            mark_success("catch_up", result.get("elapsed_seconds", 0) * 1000, exit_reason="source_error")
            _record_event("warning", database_issue, start, "official catch-up skipped: source unavailable")
            return result

        missing = []
        pending_verification = []
        for draw in source_draws:
            if _deadline_exceeded(start):
                break
            issue_number = _issue_int(draw.get("issue"))
            if issue_number is None:
                continue
            issue_missing_from_database = issue_number not in known_database_issues
            issue_newer_than_database = database_number is None or database_number < issue_number <= source_number
            if issue_newer_than_database or issue_missing_from_database:
                if _valid_draw(draw):
                    missing.append(draw)
                else:
                    pending_issue = draw.get("issue")
                    pending_verification.append(pending_issue)
                    _mark_pending_verification(pending_issue, "invalid_or_incomplete_official_draw")
            if len(missing) >= MAX_BATCH_SIZE:
                break

        if database_number is not None and source_number <= database_number and not missing:
            result = _empty_result(start, database_issue, source_issue)
            result["exit_reason"] = "completed"
            mark_success("catch_up", result.get("elapsed_seconds", 0) * 1000, exit_reason="completed")
            _record_event("ok", source_issue, start, "official catch-up already synced")
            result = _defer_downstream(result, get_latest_official_draw(), "catch_up_already_synced")
            return result

        saved = save_official_draws(missing[:MAX_BATCH_SIZE])
        success_count = int(saved.get("saved") or 0) if saved.get("status") == "ok" else 0
        failed_count = max(0, len(missing) - success_count)
        deadline_hit = _deadline_exceeded(start)
        verification = {"status": "skipped", "reason": "deadline_exceeded"}
        analysis = {"status": "skipped", "reason": "deadline_exceeded"}
        prediction = {"status": "skipped", "reason": "deadline_exceeded"}
        if not deadline_hit and success_count:
            latest_saved_draw = missing[min(success_count, len(missing)) - 1]
            verification = {"status": "queued", "reason": "downstream_after_lock_release"}
            analysis = {"status": "queued", "reason": "downstream_after_lock_release"}
            prediction = {"status": "queued", "reason": "downstream_after_lock_release"}

        elapsed = _elapsed_seconds(start)
        exit_reason = "deadline_exceeded" if deadline_hit else "completed"
        result = {
            "status": "ok" if saved.get("status") == "ok" else "warning",
            "database_latest_issue": database_issue,
            "source_latest_issue": source_issue,
            "lag_count": max(0, source_number - database_number) if database_number is not None else len(missing),
            "start_issue": missing[0].get("issue") if missing else None,
            "end_issue": missing[-1].get("issue") if missing else None,
            "catch_count": len(missing),
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_verification_count": len(pending_verification),
            "pending_verification": pending_verification[:20],
            "max_batch_size": MAX_BATCH_SIZE,
            "max_source_pages": MAX_SOURCE_PAGES,
            "job_time_budget_seconds": JOB_TIME_BUDGET_SECONDS,
            "per_issue_retry_limit": PER_ISSUE_RETRY_LIMIT,
            "elapsed_seconds": elapsed,
            "saved": saved,
            "verification": verification,
            "analysis": analysis,
            "prediction": prediction,
            "last_successful_collect_time": datetime.utcnow().isoformat() if success_count else LAST_CATCH_UP_RESULT.get("last_successful_collect_time"),
            "last_collect_duration": elapsed,
            "catch_up_available": True,
            "deadline_exceeded": deadline_hit,
            "exit_reason": exit_reason,
            "source_fetch_diagnostics": _source_fetch_diagnostics(),
        }
        LAST_CATCH_UP_RESULT.update(result)
        if deadline_hit:
            mark_deadline_exceeded("catch_up")
        mark_success(
            "catch_up",
            elapsed * 1000,
            exit_reason=exit_reason,
            last_catch_up_recovered_count=success_count,
            last_catch_up_failed_count=failed_count,
            last_catch_up_pending_count=len(pending_verification),
        )
        _record_event(result["status"], source_issue, start, f"official catch-up saved {success_count}/{len(missing)} draws")
        if not deadline_hit and success_count:
            result = _defer_downstream(result, latest_saved_draw, "catch_up_saved_draw")
        return result
    except Exception as exc:
        logger.exception("official catch-up failed")
        elapsed = _elapsed_seconds(start)
        result = {
            "status": "error",
            "database_latest_issue": database_issue,
            "source_latest_issue": None,
            "lag_count": None,
            "start_issue": None,
            "end_issue": None,
            "catch_count": 0,
            "success_count": 0,
            "failed_count": 1,
            "elapsed_seconds": elapsed,
            "error": str(exc),
            "last_successful_collect_time": LAST_CATCH_UP_RESULT.get("last_successful_collect_time"),
            "last_collect_duration": elapsed,
            "catch_up_available": True,
            "exit_reason": "exception",
            "source_fetch_diagnostics": _source_fetch_diagnostics(),
        }
        LAST_CATCH_UP_RESULT.update(result)
        mark_error("catch_up", exc, elapsed * 1000)
        _record_event("error", database_issue, start, "official catch-up failed", exc)
        return result


def get_catch_up_status(fetch_source: bool = False) -> dict:
    database_issue = get_database_latest_issue()
    source_issue = get_source_latest_issue() if fetch_source else LAST_CATCH_UP_RESULT.get("source_latest_issue")
    database_number = _issue_int(database_issue)
    source_number = _issue_int(source_issue)
    lag = max(0, source_number - database_number) if database_number is not None and source_number is not None else None
    return {
        **LAST_CATCH_UP_RESULT,
        "database_latest_issue": database_issue,
        "source_latest_issue": source_issue,
        "lag_count": lag,
        "catch_up_available": True,
    }
