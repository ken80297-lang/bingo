from __future__ import annotations

import logging
from typing import Any

from database.prediction_history_store import (
    get_prediction_history_records,
    get_prediction_history_statistics,
    update_prediction_history_result,
)

logger = logging.getLogger(__name__)


def verify_prediction(draw: dict) -> dict:
    """Verify saved predictions against an official draw without re-running AI."""
    try:
        issue = str(draw.get("issue") or "")
        if not issue:
            return {"status": "waiting_draw", "updated": 0, "message": "missing issue"}
        numbers = draw.get("numbers") or []
        if len(numbers) != 20:
            return {"status": "waiting_draw", "updated": 0, "issue": issue}
        return update_prediction_history_result(
            {
                "issue": issue,
                "numbers": numbers,
                "super_number": draw.get("super_number"),
            }
        )
    except Exception as exc:
        logger.exception("prediction lifecycle verification failed")
        return {"status": "failed", "updated": 0, "error": str(exc)}


def prediction_lifecycle_history(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    records = get_prediction_history_records(limit)
    return {
        "status": "ok",
        "data": [_history_item(item) for item in records],
    }


def prediction_lifecycle_statistics(limit: int = 100) -> dict:
    stats = get_prediction_history_statistics(limit)
    return {
        "status": "ok",
        **stats,
    }


def _history_item(item: dict[str, Any]) -> dict:
    return {
        "id": item.get("id"),
        "issue": item.get("issue"),
        "prediction_issue": item.get("prediction_issue"),
        "target_issue": item.get("prediction_issue"),
        "prediction_status": item.get("prediction_status"),
        "verified_issue": item.get("verified_issue"),
        "hit_count": item.get("hit_count"),
        "matched_numbers": item.get("matched_numbers") or [],
        "missed_numbers": item.get("missed_numbers") or [],
        "learning_used": bool(item.get("learning_used")),
        "created_at": item.get("created_at"),
        "verified_at": item.get("verified_at"),
    }
