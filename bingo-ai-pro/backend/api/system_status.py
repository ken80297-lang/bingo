from fastapi import APIRouter, Request

from services.collector_runtime import (
    get_system_status_cache,
    trigger_system_status_cache_refresh,
)

router = APIRouter(
    prefix="/api/system",
    tags=["System Status"],
)


def _scheduler_status(request: Request) -> str:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return "unknown"
    return "running" if getattr(scheduler, "running", False) else "stopped"


@router.get("/status")
def api_system_status(request: Request):
    scheduler_status = _scheduler_status(request)
    payload = get_system_status_cache(scheduler_status=scheduler_status)
    if payload.get("stale") or payload.get("cache_source") == "minimal":
        trigger_system_status_cache_refresh(scheduler_status=scheduler_status)
    payload["scheduler"] = scheduler_status
    return payload
