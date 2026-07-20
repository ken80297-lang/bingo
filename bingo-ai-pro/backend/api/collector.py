from fastapi import APIRouter

from database.collector_store import (
    get_collector_status,
    get_draw_history,
    get_kuaishou_history,
    get_latest_draw_history,
    get_latest_kuaishou_snapshot,
)
from services.catch_up_service import catch_up_missing_issues
from services.collector_gap_service import scan_collector_gaps
from services.collector_runtime import collector_runtime_status
from services.latest_sync import get_latest_sync_snapshot

router = APIRouter(prefix="/api", tags=["Collectors"])


@router.get("/collector/status")
def api_collector_status():
    runtime = collector_runtime_status()
    return {
        "status": "ok",
        "collector": get_collector_status(),
        **runtime,
    }


@router.get("/collector/catch-up")
def api_collector_catch_up():
    return catch_up_missing_issues()


@router.get("/collector/gaps")
def api_collector_gaps():
    return scan_collector_gaps()


@router.get("/collector/latest-sync")
def api_collector_latest_sync():
    return get_latest_sync_snapshot()


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
