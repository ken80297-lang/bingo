from fastapi import APIRouter

from services.voting_engine import build_voting_result, model_status

router = APIRouter(prefix="/api/models", tags=["Models"])


@router.get("/status")
def api_models_status():
    return model_status()


@router.get("/latest")
def api_models_latest():
    return build_voting_result(100)
