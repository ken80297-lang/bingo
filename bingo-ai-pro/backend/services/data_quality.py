from __future__ import annotations

import logging
from collections import Counter
from datetime import date

from database.collector_store import get_kuaishou_history
from database.data_quality_store import save_data_quality_report

logger = logging.getLogger(__name__)


def _as_issue_number(issue) -> int | None:
    try:
        return int(issue)
    except Exception:
        return None


def _numbers_from_snapshot(snapshot: dict) -> list[int]:
    parsed = snapshot.get("parsed_json") or {}
    api_data = parsed.get("api_get_data") if isinstance(parsed, dict) else None
    latest = (api_data.get("data") or [{}])[0] if isinstance(api_data, dict) else {}
    raw_numbers = latest.get("\u4e00\u822c\u734e\u865f") or snapshot.get("numbers") or []

    numbers = []
    for value in raw_numbers:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            numbers.append(number)
    return numbers


def build_kuaishou_quality_report(limit: int = 1000) -> dict:
    snapshots = get_kuaishou_history(limit)
    issue_values = [snapshot.get("issue") for snapshot in snapshots if snapshot.get("issue")]
    issue_numbers = sorted(
        issue for issue in (_as_issue_number(value) for value in issue_values)
        if issue is not None
    )

    duplicate_issues = [
        issue for issue, count in Counter(issue_values).items()
        if issue is not None and count > 1
    ]

    missing_issues = []
    if issue_numbers:
        existing = set(issue_numbers)
        for issue in range(issue_numbers[0], issue_numbers[-1] + 1):
            if issue not in existing:
                missing_issues.append(str(issue))

    invalid_records = []
    for snapshot in snapshots:
        issue = snapshot.get("issue")
        numbers = _numbers_from_snapshot(snapshot)
        reasons = []

        if issue in (None, ""):
            reasons.append("missing_issue")
        if len(numbers) != 20:
            reasons.append("invalid_numbers_count")
        if len(set(numbers)) != len(numbers):
            reasons.append("duplicate_numbers")

        if reasons:
            invalid_records.append(
                {
                    "id": snapshot.get("id"),
                    "issue": issue,
                    "reasons": reasons,
                    "numbers_count": len(numbers),
                }
            )

    status = "ok"
    if missing_issues or duplicate_issues or invalid_records:
        status = "warning"

    return {
        "report_date": date.today().isoformat(),
        "source": "kuaishou",
        "total_records": len(snapshots),
        "missing_issues": missing_issues,
        "duplicate_issues": duplicate_issues,
        "invalid_records": invalid_records,
        "latest_issue": str(issue_numbers[-1]) if issue_numbers else None,
        "earliest_issue": str(issue_numbers[0]) if issue_numbers else None,
        "status": status,
    }


def run_kuaishou_data_quality_check(limit: int = 1000) -> dict:
    try:
        report = build_kuaishou_quality_report(limit)
        saved = save_data_quality_report(report)
        return {
            "status": report.get("status", "unknown"),
            "report": report,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("kuaishou data quality check failed")
        report = {
            "report_date": date.today().isoformat(),
            "source": "kuaishou",
            "total_records": 0,
            "missing_issues": [],
            "duplicate_issues": [],
            "invalid_records": [{"error": str(exc)}],
            "latest_issue": None,
            "earliest_issue": None,
            "status": "error",
        }
        saved = save_data_quality_report(report)
        return {"status": "error", "report": report, "saved": saved}
