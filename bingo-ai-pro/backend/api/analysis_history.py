from fastapi import APIRouter

from database.analysis_store import (
    get_analysis_history,
    get_analysis_statistics,
    get_latest_analysis_history,
)

router = APIRouter(prefix="/api/analysis", tags=["Analysis History"])


@router.get("/latest")
def api_analysis_latest():
    return {
        "status": "ok",
        "data": get_latest_analysis_history(),
    }


@router.get("/history")
def api_analysis_history(limit: int = 100):
    return {
        "status": "ok",
        "data": get_analysis_history(limit),
    }


@router.get("/statistics")
def api_analysis_statistics(limit: int = 100):
    return {
        "status": "ok",
        "data": get_analysis_statistics(limit),
    }
