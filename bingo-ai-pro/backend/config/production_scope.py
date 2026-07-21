from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

PRODUCTION_DATA_START_ISSUE = int(os.getenv("PRODUCTION_DATA_START_ISSUE", "115040780"))
PRODUCTION_GENERATION = int(os.getenv("PRODUCTION_GENERATION", "2"))
PRODUCTION_DATA_START_AT = os.getenv("PRODUCTION_DATA_START_AT", "2026-07-20T14:00:00Z")
ANALYSIS_HISTORY_WINDOW = int(os.getenv("ANALYSIS_HISTORY_WINDOW", "200"))
TEST_ISSUE_PREFIX = "99"
PENDING_PREFIX = "pending:"


def get_production_start_issue() -> int:
    return PRODUCTION_DATA_START_ISSUE


def get_production_generation() -> int:
    return PRODUCTION_GENERATION


def get_production_start_at() -> datetime:
    return datetime.fromisoformat(PRODUCTION_DATA_START_AT.replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize_issue(issue: Any) -> str | None:
    if issue is None:
        return None
    value = str(issue).strip()
    return value or None


def is_test_issue(issue: Any) -> bool:
    value = normalize_issue(issue)
    return bool(value and (value.startswith(TEST_ISSUE_PREFIX) or value.upper().startswith("TEST")))


def is_pending_issue(issue: Any) -> bool:
    value = normalize_issue(issue)
    return bool(value and value.lower().startswith(PENDING_PREFIX))


def is_issue_in_current_generation(issue: Any) -> bool:
    value = normalize_issue(issue)
    if not value or is_test_issue(value) or is_pending_issue(value):
        return False
    try:
        return int(value) >= PRODUCTION_DATA_START_ISSUE
    except Exception:
        return False


def is_valid_official_issue(issue: Any) -> bool:
    return is_issue_in_current_generation(issue)


def is_production_record(record: Mapping[str, Any] | None) -> bool:
    if not record:
        return False
    issue = (
        record.get("issue")
        or record.get("draw_issue")
        or record.get("based_on_issue")
        or record.get("prediction_issue")
        or record.get("target_issue")
    )
    if not is_valid_official_issue(issue):
        return False
    if str(record.get("source", "")).lower() == "test":
        return False
    if record.get("production_valid") is False:
        return False
    generation = record.get("production_generation")
    if generation is not None:
        try:
            if int(generation) != PRODUCTION_GENERATION:
                return False
        except Exception:
            return False
    return True


def production_scope_payload() -> dict[str, Any]:
    return {
        "production_generation": PRODUCTION_GENERATION,
        "production_start_issue": str(PRODUCTION_DATA_START_ISSUE),
        "production_start_at": PRODUCTION_DATA_START_AT,
        "analysis_history_window": ANALYSIS_HISTORY_WINDOW,
        "test_issue_prefix": TEST_ISSUE_PREFIX,
        "pending_issue_prefix": PENDING_PREFIX,
    }

