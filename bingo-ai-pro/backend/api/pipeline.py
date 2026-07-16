from fastapi import APIRouter, Request

from services.pipeline_health import build_pipeline_health

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline Health"])


@router.get("/health")
def api_pipeline_health(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    return build_pipeline_health(scheduler=scheduler)
