from fastapi import APIRouter

from services.system_health import build_system_health

router = APIRouter(prefix="/api", tags=["System Health"])


@router.get("/system-health")
def api_system_health():
    return build_system_health(save=True)
