from __future__ import annotations

import logging
from typing import Any

from database.operations_store import (
    get_database_health,
    get_operation_error_summary,
    get_operation_errors,
    get_operation_metrics,
    get_operation_timeline,
    resolve_stale_operation_errors,
    save_operation_event,
)

logger = logging.getLogger(__name__)


def record_operation_event(
    component: str,
    event_type: str = "pipeline_stage",
    status: str = "ok",
    issue: str | None = None,
    message: str | None = None,
    duration_ms: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> dict:
    try:
        return save_operation_event(
            {
                "issue": issue,
                "component": component,
                "event_type": event_type,
                "status": status,
                "message": message,
                "duration_ms": duration_ms,
                "error_type": error_type,
                "error_message": error_message,
            }
        )
    except Exception as exc:
        logger.exception("operation event recording failed")
        return {"status": "error", "error": str(exc)}


def operation_timeline(limit: int = 50) -> dict:
    try:
        return {"status": "ok", "data": get_operation_timeline(limit)}
    except Exception as exc:
        logger.exception("operation timeline failed")
        return {"status": "error", "data": [], "error": str(exc)}


def operation_errors(limit: int = 50) -> dict:
    try:
        return {"status": "ok", "data": get_operation_errors(limit)}
    except Exception as exc:
        logger.exception("operation errors failed")
        return {"status": "error", "data": [], "error": str(exc)}


def operation_error_summary() -> dict:
    try:
        return get_operation_error_summary()
    except Exception as exc:
        logger.exception("operation error summary failed")
        return {"total": 0, "unresolved": 0, "resolved": 0, "unresolved_by_component": {}, "error": str(exc)}


def resolve_stale_errors() -> dict:
    try:
        return resolve_stale_operation_errors()
    except Exception as exc:
        logger.exception("resolve stale operation errors failed")
        return {"status": "error", "checked": 0, "resolved": 0, "remaining_unresolved": 0, "error": str(exc)}


def operation_metrics() -> dict:
    try:
        return get_operation_metrics()
    except Exception as exc:
        logger.exception("operation metrics failed")
        return {"status": "error", "components": [], "error": str(exc)}


def operation_database_health() -> dict:
    try:
        return get_database_health()
    except Exception as exc:
        logger.exception("operation database health failed")
        return {"status": "error", "tables": {}, "error": str(exc)}


def _deferred_database_health() -> dict:
    return {
        "status": "deferred",
        "tables": {},
        "message": "Use /api/operations/database-health for a full table scan.",
    }


def _latest_issue_from_health(database_health: dict[str, Any]) -> str | None:
    tables = database_health.get("tables") or {}
    kuaishou = tables.get("kuaishou_snapshots") or {}
    return kuaishou.get("latest_issue")


def operation_summary(limit: int = 20) -> dict:
    timeline_payload = operation_timeline(limit)
    errors_payload = operation_errors(limit)
    metrics_payload = operation_metrics()
    database_health = _deferred_database_health()
    error_summary = operation_error_summary()

    timeline = timeline_payload.get("data") or []
    errors = errors_payload.get("data") or []
    unresolved_errors = [item for item in errors if not item.get("resolved")]
    suggestions: list[str] = []

    status = "ok"
    if error_summary.get("unresolved", 0) > 0:
        status = "error"
        suggestions.append("Resolve outstanding pipeline errors before trusting production output.")
    elif database_health.get("status") == "error":
        status = "warning"
        suggestions.append("Check database table health and fallback storage status.")
    else:
        recent_statuses = {str(item.get("status") or "").lower() for item in timeline[:10]}
        if "warning" in recent_statuses:
            status = "warning"
            suggestions.append("Review recent warning events in the pipeline timeline.")

    latest_issue = _latest_issue_from_health(database_health)
    if not latest_issue and timeline:
        latest_issue = timeline[0].get("issue")

    return {
        "status": status,
        "latest_issue": latest_issue,
        "timeline": timeline,
        "errors": errors,
        "error_summary": error_summary,
        "metrics": metrics_payload,
        "database_health": database_health,
        "suggestions": suggestions,
    }
