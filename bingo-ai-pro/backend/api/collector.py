import os

from fastapi import APIRouter, Depends, Header, HTTPException

from database.collector_store import (
    get_collector_status,
    get_draw_history,
    get_kuaishou_history,
    get_kuaishou_summary_history,
    get_latest_draw_history,
    get_latest_kuaishou_snapshot,
)
from services.catch_up_service import catch_up_missing_issues, get_catch_up_status
from services.collector_gap_service import get_cached_collector_gaps
from services.collector_runtime import collector_runtime_status
from services.latest_sync import get_latest_sync_snapshot
from config.production_scope import production_scope_payload

router = APIRouter(prefix="/api", tags=["Collectors"])


def require_collector_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
    expected = os.getenv("COLLECTOR_ADMIN_TOKEN") or os.getenv("ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="collector admin token required")


@router.get("/collector/status")
def api_collector_status():
    runtime = collector_runtime_status()
    return {
        "status": "ok",
        "collector": get_collector_status(),
        "production_scope": production_scope_payload(),
        "test_data_ignored": True,
        **runtime,
    }


@router.get("/collector/catch-up")
def api_collector_catch_up():
    status = get_catch_up_status(fetch_source=False)
    return {
        **status,
        "read_only": True,
        "execution_triggered": False,
    }


@router.post("/collector/catch-up", dependencies=[Depends(require_collector_admin)])
def api_collector_catch_up_run(force: bool = False):
    return catch_up_missing_issues(force=force)


@router.get("/collector/gaps")
def api_collector_gaps():
    return get_cached_collector_gaps()


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
def api_kuaishou_history(limit: int | None = None, view: str = "full"):
    normalized_view = str(view or "full").strip().lower()
    if normalized_view == "summary":
        safe_limit = max(1, min(int(limit or 20), 100))
        data = get_kuaishou_summary_history(safe_limit)
    else:
        safe_limit = int(limit or 50)
        data = get_kuaishou_history(safe_limit)
    return {
        "status": "ok",
        "view": "summary" if normalized_view == "summary" else "full",
        "data": data,
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
