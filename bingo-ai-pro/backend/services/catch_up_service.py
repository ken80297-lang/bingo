from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.official_draw_store import (
    get_latest_official_draw,
    save_official_draws,
)
from services.operations_center import record_operation_event
from services.official_verification import run_official_verification

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
    database_issue = get_database_latest_issue()
    try:
        source_draws = fetch_source_today_draws()
        source_issue = _latest_issue_from_draws(source_draws)
        database_number = _issue_int(database_issue)
        source_number = _issue_int(source_issue)

        if source_number is None:
            result = _empty_result(start, database_issue, source_issue, status="warning")
            result["reason"] = "source_latest_issue_unavailable"
            LAST_CATCH_UP_RESULT.update(result)
            _record_event("warning", database_issue, start, "official catch-up skipped: source unavailable")
            return result

        if database_number is not None and source_number <= database_number:
            result = _empty_result(start, database_issue, source_issue)
            LAST_CATCH_UP_RESULT.update(result)
            _record_event("ok", source_issue, start, "official catch-up already synced")
            return result

        missing = []
        for draw in source_draws:
            issue_number = _issue_int(draw.get("issue"))
            if issue_number is None:
                continue
            if database_number is None or database_number < issue_number <= source_number:
                missing.append(draw)

        saved = save_official_draws(missing)
        success_count = int(saved.get("saved") or 0) if saved.get("status") == "ok" else 0
        failed_count = max(0, len(missing) - success_count)
        verification = run_official_verification(limit=10)

        prediction = {"status": "unknown"}
        try:
            from services.prediction_tracker import evaluate_pending_predictions

            prediction = evaluate_pending_predictions(max_runs=3)
        except Exception as exc:
            logger.exception("catch-up prediction evaluation failed")
            prediction = {"status": "error", "message": str(exc)}

        elapsed = round(time.perf_counter() - start, 3)
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
            "elapsed_seconds": elapsed,
            "saved": saved,
            "verification": verification,
            "prediction": prediction,
            "last_successful_collect_time": datetime.utcnow().isoformat() if success_count else LAST_CATCH_UP_RESULT.get("last_successful_collect_time"),
            "last_collect_duration": elapsed,
            "catch_up_available": True,
        }
        LAST_CATCH_UP_RESULT.update(result)
        _record_event(result["status"], source_issue, start, f"official catch-up saved {success_count}/{len(missing)} draws")
        return result
    except Exception as exc:
        logger.exception("official catch-up failed")
        elapsed = round(time.perf_counter() - start, 3)
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
        }
        LAST_CATCH_UP_RESULT.update(result)
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
