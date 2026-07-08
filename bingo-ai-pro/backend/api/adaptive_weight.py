from fastapi import APIRouter

from database.adaptive_weight_store import (
    get_adaptive_weight_history,
    get_latest_adaptive_weights,
)
from services.adaptive_weight import update_adaptive_weights

router = APIRouter(prefix="/api/adaptive-weight", tags=["Adaptive Weight"])


@router.post("/update")
def api_adaptive_weight_update():
    return update_adaptive_weights()


@router.get("/latest")
def api_adaptive_weight_latest():
    return {
        "status": "ok",
        "data": get_latest_adaptive_weights(),
    }


@router.get("/history")
def api_adaptive_weight_history(limit: int = 20):
    return {
        "status": "ok",
        "data": get_adaptive_weight_history(limit),
    }

