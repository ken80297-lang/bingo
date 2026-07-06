from fastapi import APIRouter, Request

from database.cloud_draws import get_cloud_history_draws
from db import get_latest_draw, get_statistics

router = APIRouter(
    prefix="/api/system",
    tags=["System Status"],
)


def _scheduler_status(request: Request) -> str:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return "unknown"

    return "running" if getattr(scheduler, "running", False) else "stopped"


def _sqlite_status() -> str:
    try:
        get_latest_draw()
        return "available"
    except Exception:
        return "unknown"


def _cloud_status() -> str:
    try:
        get_cloud_history_draws(1)
        return "available"
    except Exception:
        return "unknown"


def _latest_issue(stats: dict) -> str | None:
    issue = stats.get("latest_issue")
    if issue is not None:
        return str(issue)

    try:
        latest = get_latest_draw()
        return latest["issue"] if latest else None
    except Exception:
        return None


@router.get("/status")
def api_system_status(request: Request):
    try:
        stats = get_statistics()
    except Exception:
        stats = {}

    return {
        "status": "ok",
        "provider": "kuaishou",
        "scheduler": _scheduler_status(request),
        "latest_issue": _latest_issue(stats),
        "last_update": stats.get("last_update") or stats.get("updated_at"),
        "database": {
            "sqlite": _sqlite_status(),
            "cloud": _cloud_status(),
        },
    }
