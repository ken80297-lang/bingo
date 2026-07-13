from fastapi import APIRouter

from services.health_cache_engine import get_cached_health

router = APIRouter(prefix="/api", tags=["System Health"])


@router.get("/system-health")
def api_system_health():
    return get_cached_health()
