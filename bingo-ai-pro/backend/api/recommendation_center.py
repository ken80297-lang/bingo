from fastapi import APIRouter

from database.recommendation_center_store import (
    get_latest_recommendation_run,
    get_recommendation_history,
    get_recommendation_run_by_issue,
    get_today_recommendation_run,
)
from services.recommendation_center import generate_recommendation_center
from services.simulation_model import get_production_latest_issue

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
    latest_issue = get_production_latest_issue()
    if latest_issue:
        recommendation = get_recommendation_run_by_issue(latest_issue)
        if not recommendation:
            generated = generate_recommendation_center()
            recommendation = generated.get("recommendation") or get_recommendation_run_by_issue(latest_issue)
        if recommendation:
            return {
                "status": "ok",
                "latest_issue": latest_issue,
                "data": recommendation,
            }
        fallback = get_latest_recommendation_run()
        return {
            "status": "outdated",
            "latest_issue": latest_issue,
            "recommendation_issue": fallback.get("issue") if fallback else None,
            "data": fallback,
        }

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
