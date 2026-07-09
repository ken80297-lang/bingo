import time

from fastapi import APIRouter

from services.operations_center import record_operation_event
from services.system_health import build_system_health

router = APIRouter(prefix="/api", tags=["System Health"])


@router.get("/system-health")
def api_system_health():
    start = time.perf_counter()
    payload = build_system_health(save=True)
    record_operation_event(
        component="system_health",
        event_type="api_check",
        status=payload.get("status", "unknown"),
        issue=payload.get("latest_issue"),
        message="system health checked",
        duration_ms=round((time.perf_counter() - start) * 1000, 2),
    )
    return payload
