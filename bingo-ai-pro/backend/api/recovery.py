from __future__ import annotations

from fastapi import APIRouter

from database.recovery_store import get_latest_recovery_report
from services.daily_recovery import build_health_report, get_recovery_status, run_daily_recovery

router = APIRouter(prefix="/api/recovery", tags=["Daily Recovery"])


@router.get("/status")
def api_recovery_status():
    return get_recovery_status()


@router.get("/report")
def api_recovery_report():
    return {"status": "ok", "data": get_latest_recovery_report(), "health": build_health_report()}


@router.post("/run")
def api_recovery_run(force: bool = True):
    return run_daily_recovery(force=force)

