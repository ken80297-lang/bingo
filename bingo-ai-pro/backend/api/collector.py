from fastapi import APIRouter

from database.collector_store import (
    get_collector_status,
    get_draw_history,
    get_kuaishou_history,
    get_latest_draw_history,
    get_latest_kuaishou_snapshot,
)

router = APIRouter(prefix="/api", tags=["Collectors"])


@router.get("/collector/status")
def api_collector_status():
    return {
        "status": "ok",
        "collector": get_collector_status(),
    }


@router.get("/kuaishou/latest")
def api_kuaishou_latest():
    return {
        "status": "ok",
        "data": get_latest_kuaishou_snapshot(),
    }


@router.get("/kuaishou/history")
def api_kuaishou_history(limit: int = 50):
    return {
        "status": "ok",
        "data": get_kuaishou_history(limit),
    }


@router.get("/draws/latest")
def api_draws_latest():
    return {
        "status": "ok",
        "data": get_latest_draw_history(),
    }


@router.get("/draws/history")
def api_draws_history(limit: int = 50):
    return {
        "status": "ok",
        "data": get_draw_history(limit),
    }
