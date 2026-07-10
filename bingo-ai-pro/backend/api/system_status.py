from datetime import datetime, timezone

from fastapi import APIRouter, Request

from database.collector_store import get_collector_status
from database.cloud_draws import get_cloud_history_draws
from database.data_quality_store import get_data_quality_status
from database.prediction_history_store import get_prediction_history_count
from db import get_latest_draw, get_statistics
from services.catch_up_service import get_catch_up_status

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


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _collector_health(catch_up: dict) -> dict:
    status = str(catch_up.get("status") or "unknown")
    if status == "error":
        return {"status": "error", "reason": catch_up.get("error") or "collector_error"}
    last_success = _parse_time(catch_up.get("last_successful_collect_time"))
    if last_success:
        age_minutes = (datetime.now(timezone.utc) - last_success).total_seconds() / 60
        if age_minutes >= 30:
            return {"status": "error", "reason": "超過 30 分鐘沒有新資料"}
        if age_minutes >= 15:
            return {"status": "warning", "reason": "超過 15 分鐘沒有新資料"}
    lag = catch_up.get("lag_count")
    if isinstance(lag, int) and lag > 0:
        return {"status": "warning", "reason": f"落後 {lag} 期"}
    return {"status": "ok", "reason": "已同步"}


@router.get("/status")
def api_system_status(request: Request):
    try:
        stats = get_statistics()
    except Exception:
        stats = {}

    catch_up = get_catch_up_status(fetch_source=False)
    collector_health = _collector_health(catch_up)

    return {
        "status": "ok",
        "provider": "kuaishou",
        "scheduler": _scheduler_status(request),
        "latest_issue": _latest_issue(stats),
        "last_update": stats.get("last_update") or stats.get("updated_at"),
        "database_latest_issue": catch_up.get("database_latest_issue"),
        "source_latest_issue": catch_up.get("source_latest_issue"),
        "lag_count": catch_up.get("lag_count"),
        "collector_status": collector_health.get("status"),
        "collector_status_reason": collector_health.get("reason"),
        "last_successful_collect_time": catch_up.get("last_successful_collect_time"),
        "last_collect_duration": catch_up.get("last_collect_duration"),
        "catch_up_available": catch_up.get("catch_up_available"),
        "prediction_history_count": get_prediction_history_count(),
        "database": {
            "sqlite": _sqlite_status(),
            "cloud": _cloud_status(),
        },
        "collector": get_collector_status(),
        "data_quality": get_data_quality_status(),
    }
