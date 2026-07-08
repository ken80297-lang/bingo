from fastapi import APIRouter, Request

from database.adaptive_weight_store import get_latest_adaptive_weights
from database.collector_store import get_collector_status
from database.data_quality_store import get_data_quality_status
from database.simulation_store import get_latest_simulation_run
from database.strategy_ranking_store import (
    get_latest_strategy_rankings,
    get_strategy_ranking_history,
)
from db import get_latest_draw, get_statistics
from services.strategy_ranking import build_strategy_rankings

router = APIRouter(prefix="/api", tags=["Strategy Ranking"])


def _scheduler_status(request: Request) -> str:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return "unknown"
    return "running" if getattr(scheduler, "running", False) else "stopped"


def _system_summary(request: Request) -> dict:
    try:
        stats = get_statistics()
    except Exception:
        stats = {}

    latest_issue = stats.get("latest_issue")
    if latest_issue is None:
        try:
            latest = get_latest_draw()
            latest_issue = latest.get("issue") if latest else None
        except Exception:
            latest_issue = None

    return {
        "status": "ok",
        "provider": "kuaishou",
        "scheduler": _scheduler_status(request),
        "latest_issue": str(latest_issue) if latest_issue is not None else None,
        "last_update": stats.get("last_update") or stats.get("updated_at"),
    }


@router.post("/strategy-ranking/update")
def api_strategy_ranking_update():
    return build_strategy_rankings()


@router.get("/strategy-ranking/latest")
def api_strategy_ranking_latest():
    return {
        "status": "ok",
        "data": get_latest_strategy_rankings(),
    }


@router.get("/strategy-ranking/history")
def api_strategy_ranking_history(limit: int = 20):
    return {
        "status": "ok",
        "data": get_strategy_ranking_history(limit),
    }


@router.get("/dashboard")
def api_dashboard(request: Request):
    try:
        return {
            "status": "ok",
            "system": _system_summary(request),
            "collector": get_collector_status(),
            "data_quality": get_data_quality_status(),
            "adaptive_weight": get_latest_adaptive_weights(),
            "strategy_ranking": get_latest_strategy_rankings(),
            "latest_simulation": get_latest_simulation_run(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
            "system": _system_summary(request),
            "collector": {},
            "data_quality": {},
            "adaptive_weight": None,
            "strategy_ranking": [],
            "latest_simulation": None,
        }

