from fastapi import APIRouter, Request

from services.collector_runtime import (
    get_system_status_cache,
    trigger_system_status_cache_refresh,
)
from services.latest_sync import get_latest_sync_snapshot

router = APIRouter(
    prefix="/api/system",
    tags=["System Status"],
)


def _scheduler_status(request: Request) -> str:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return "unknown"
    return "running" if getattr(scheduler, "running", False) else "stopped"


def _wake_monitor_status(request: Request) -> str:
    value = getattr(request.app.state, "last_health_request_at", None)
    if not value:
        return "unknown"
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    except Exception:
        return "unknown"
    if age_seconds <= 7 * 60:
        return "healthy"
    if age_seconds <= 12 * 60:
        return "delayed"
    return "at_risk"


@router.get("/status")
def api_system_status(request: Request):
    scheduler_status = _scheduler_status(request)
    payload = get_system_status_cache(scheduler_status=scheduler_status)
    if payload.get("stale") or payload.get("cache_source") == "minimal":
        trigger_system_status_cache_refresh(scheduler_status=scheduler_status)
    payload["scheduler"] = scheduler_status
    runtime = payload.get("collector_runtime") or {}
    latest_sync = get_latest_sync_snapshot()
    payload["web_service"] = "online"
    payload["wake_monitor"] = _wake_monitor_status(request)
    payload["collector"] = "running" if runtime.get("collector_running") else payload.get("collector_status", "unknown")
    payload["latest_draw_sync"] = latest_sync.get("sync_status")
    return payload
