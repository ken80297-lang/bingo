from __future__ import annotations

import logging
import time
from typing import Any

from database.analysis_store import (
    get_analysis_statistics,
    get_latest_analysis_history,
    save_analysis_history,
)
from database.official_draw_store import get_official_draw_history
from services.operations_center import record_operation_event

logger = logging.getLogger(__name__)


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _record_event(status: str, issue: str | None, start: float, message: str, error: Exception | None = None) -> None:
    try:
        record_operation_event(
            component="analysis_engine",
            event_type="analysis",
            status=status,
            issue=issue,
            message=message,
            duration_ms=_duration_ms(start),
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
    except Exception:
        logger.exception("failed to record analysis engine event")


def _official_to_draw(official: dict[str, Any]) -> dict:
    return {
        "issue": official.get("issue"),
        "draw_time": official.get("draw_time") or official.get("draw_date"),
        "numbers": official.get("numbers") or [],
        "super_number": official.get("super_number"),
        "source": "taiwan_lottery",
    }


def _is_test_issue(issue: Any) -> bool:
    text = str(issue or "").strip().upper()
    return not text or text.startswith("99") or text.startswith("TEST")


def analyze_official_draw(official: dict[str, Any]) -> dict:
    start = time.perf_counter()
    issue = str(official.get("issue")) if official and official.get("issue") is not None else None
    try:
        if not official or not issue:
            return {"status": "error", "issue": issue, "error": "missing official draw"}
        if _is_test_issue(issue):
            return {"status": "skipped", "issue": issue, "reason": "test_issue"}
        result = save_analysis_history(_official_to_draw(official))
        _record_event(
            "ok" if result.get("status") == "ok" else "warning",
            issue,
            start,
            f"analysis saved for issue {issue}",
        )
        return {"status": result.get("status"), "issue": issue, "saved": result}
    except Exception as exc:
        logger.exception("analysis engine failed for official draw")
        _record_event("error", issue, start, "analysis engine failed", exc)
        return {"status": "error", "issue": issue, "error": str(exc)}


def analyze_latest_official_draws(limit: int = 20) -> dict:
    start = time.perf_counter()
    saved = []
    failed = []
    try:
        for official in get_official_draw_history(limit):
            result = analyze_official_draw(official)
            if result.get("status") == "ok":
                saved.append(result)
            elif result.get("status") == "skipped":
                continue
            else:
                failed.append(result)
        latest = get_latest_analysis_history()
        status = "ok" if not failed else "warning"
        _record_event(status, latest.get("issue") if latest else None, start, f"analysis batch saved {len(saved)} draws")
        return {
            "status": status,
            "success_count": len(saved),
            "failed_count": len(failed),
            "latest": latest,
            "failed": failed,
            "elapsed_seconds": round(time.perf_counter() - start, 3),
        }
    except Exception as exc:
        logger.exception("analysis engine batch failed")
        _record_event("error", None, start, "analysis engine batch failed", exc)
        return {"status": "error", "success_count": 0, "failed_count": 1, "error": str(exc)}


def analysis_engine_status() -> dict:
    stats = get_analysis_statistics(100)
    return {
        "status": "ok" if stats.get("analysis_count", 0) else "warning",
        "latest_issue": stats.get("latest_issue"),
        "analysis_count": stats.get("analysis_count", 0),
        "last_analysis_time": stats.get("last_analysis_time"),
        "statistics": stats,
    }
