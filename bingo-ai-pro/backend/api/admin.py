from __future__ import annotations

from fastapi import APIRouter, Request

from database.collector_store import get_collector_status
from database.prediction_history_store import (
    get_prediction_history_count,
)
from services.catch_up_service import get_catch_up_status

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _scheduler_status(request: Request) -> str:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return "unknown"
    return "ok" if getattr(scheduler, "running", False) else "error"


def _status_from_lag(lag: int | None) -> str:
    if lag is None:
        return "unknown"
    if lag == 0:
        return "ok"
    if lag <= 2:
        return "warning"
    return "error"


def _learning_lazy_placeholder() -> dict:
    return {
        "status": "lazy",
        "readiness_status": "loading",
        "ready_for_phase_22_2": False,
        "lazy_endpoint": "/api/learning/observation",
        "readiness": {
            "status": "loading",
            "ready": False,
            "reasons": ["learning observation loads separately"],
        },
        "records": {},
        "targets": {},
        "quality": {},
        "models": [],
    }


@router.get("/status")
def api_admin_status(request: Request):
    catch_up = get_catch_up_status(fetch_source=False)
    collector = get_collector_status()
    learning = _learning_lazy_placeholder()
    history_count = get_prediction_history_count()
    lag_count = catch_up.get("lag_count")

    database_status = "ok" if catch_up.get("database_latest_issue") else "warning"
    prediction_history_status = "ok" if history_count > 0 else "warning"
    collector_status = _status_from_lag(lag_count)

    return {
        "status": "ok",
        "system": {
            "service": "ok",
            "collector": collector_status,
            "scheduler": _scheduler_status(request),
            "database": database_status,
            "prediction_history": prediction_history_status,
            "ai_engine": "unknown",
        },
        "sync": {
            "database_latest_issue": catch_up.get("database_latest_issue"),
            "source_latest_issue": catch_up.get("source_latest_issue"),
            "lag": lag_count,
            "last_successful_collect_time": catch_up.get("last_successful_collect_time"),
            "last_collect_duration": catch_up.get("last_collect_duration"),
            "status": collector_status,
            "summary": "已同步" if lag_count == 0 else f"落後 {lag_count or 0} 期",
        },
        "prediction_history": {
            "total_count": history_count,
            "today_count": 0,
            "latest_prediction": None,
            "last_prediction_issue": None,
            "last_verified_at": None,
            "lazy_endpoint": "/api/prediction-history/statistics?limit=100",
        },
        "analysis_engine": {"status": "lazy", "lazy_endpoint": "/api/analysis/statistics"},
        "learning_engine": learning,
        "model_engine": {"status": "lazy", "lazy_endpoint": "/api/models/status"},
        "system_health": {"status": "lazy", "lazy_endpoint": "/api/system-health"},
        "operations": {"status": "lazy", "lazy_endpoint": "/api/operations/summary"},
        "hit_rate": {"status": "lazy", "lazy_endpoint": "/api/prediction-history/statistics?limit=100"},
        "collector": {
            "official_source": "Taiwan Lottery Official API",
            "last_fetch": catch_up.get("last_successful_collect_time"),
            "success_rate": 0,
            "recent_error": None,
            "raw_status": collector,
        },
        "next_prediction": {"status": "lazy", "lazy_endpoint": "/api/next-prediction"},
    }
