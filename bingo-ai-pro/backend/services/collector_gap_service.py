from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from database.official_draw_store import get_official_draw_history
from services.collector_runtime import update_collector_runtime
from services.latest_sync import HISTORICAL_CATCHUP_ENABLED, LATEST_ISSUE_PRIORITY


def _issue_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


def _valid_numbers(values: Any) -> bool:
    try:
        numbers = [int(value) for value in values or []]
    except Exception:
        return False
    return len(numbers) == 20 and len(set(numbers)) == 20 and all(1 <= number <= 80 for number in numbers)


def scan_collector_gaps(limit: int = 200) -> dict:
    checked_at = datetime.now(timezone.utc).isoformat()
    records = get_official_draw_history(max(1, min(int(limit or 200), 200)))
    issues = [_issue_int(item.get("issue")) for item in records]
    issues = sorted(issue for issue in issues if issue is not None)
    if not issues:
        payload = {
            "status": "ok",
            "checked_at": checked_at,
            "range": {"start_issue": None, "end_issue": None},
            "database_count": 0,
            "expected_count": 0,
            "missing_count": 0,
            "missing_issues": [],
            "pending_verification": [],
            "duplicate_issues": [],
            "invalid_issues": [],
            "continuity_status": "unknown",
            "historical_catchup_enabled": HISTORICAL_CATCHUP_ENABLED,
            "historical_gaps_ignored": not HISTORICAL_CATCHUP_ENABLED,
            "latest_issue_priority": LATEST_ISSUE_PRIORITY,
        }
        update_collector_runtime(last_gap_scan_at=checked_at, missing_count=0, continuity_status="unknown")
        return payload

    start_issue, end_issue = min(issues), max(issues)
    issue_set = set(issues)
    expected = list(range(start_issue, end_issue + 1))
    missing = [str(issue) for issue in expected if issue not in issue_set]
    duplicates = [str(issue) for issue, count in Counter(issues).items() if count > 1]
    invalid = [
        str(item.get("issue"))
        for item in records
        if item.get("issue") and not _valid_numbers(item.get("numbers"))
    ]
    pending = [
        str(item.get("issue"))
        for item in records
        if item.get("issue") and not item.get("verified")
    ]
    continuity_status = "complete" if not missing and not invalid else "warning"
    effective_status = "ignored" if missing and not HISTORICAL_CATCHUP_ENABLED else continuity_status
    payload = {
        "status": "ok",
        "checked_at": checked_at,
        "range": {"start_issue": str(start_issue), "end_issue": str(end_issue)},
        "database_count": len(issue_set),
        "expected_count": len(expected),
        "missing_count": len(missing),
        "missing_issues": missing[:100],
        "pending_verification": pending[:100],
        "duplicate_issues": duplicates[:100],
        "invalid_issues": invalid[:100],
        "continuity_status": effective_status,
        "diagnostic_continuity_status": continuity_status,
        "historical_catchup_enabled": HISTORICAL_CATCHUP_ENABLED,
        "historical_gaps_ignored": not HISTORICAL_CATCHUP_ENABLED,
        "latest_issue_priority": LATEST_ISSUE_PRIORITY,
    }
    update_collector_runtime(
        last_gap_scan_at=checked_at,
        missing_count=len(missing),
        continuity_status=continuity_status,
    )
    return payload
