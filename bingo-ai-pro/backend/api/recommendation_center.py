from fastapi import APIRouter

from database.recommendation_center_store import (
    get_latest_recommendation_run,
    get_recommendation_history,
    get_today_recommendation_run,
)
from services.recommendation_center import generate_recommendation_center

router = APIRouter(prefix="/api/recommendation-center", tags=["Recommendation Center"])


@router.post("/generate")
def api_recommendation_center_generate():
    return generate_recommendation_center()


@router.get("/today")
def api_recommendation_center_today():
    return {
        "status": "ok",
        "data": get_today_recommendation_run(),
    }


@router.get("/latest")
def api_recommendation_center_latest():
    return {
        "status": "ok",
        "data": get_latest_recommendation_run(),
    }


@router.get("/history")
def api_recommendation_center_history(limit: int = 20):
    return {
        "status": "ok",
        "data": get_recommendation_history(limit),
    }

