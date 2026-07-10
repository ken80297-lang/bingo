from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from database.collector_store import get_collector_status
from database.prediction_history_store import (
    get_latest_prediction_history,
    get_prediction_history_count,
    get_prediction_history_records,
    get_prediction_history_statistics,
)
from services.catch_up_service import get_catch_up_status
from services.analysis_engine import analysis_engine_status
from services.next_prediction_center import build_next_prediction_dashboard
from services.operations_center import operation_error_summary, operation_errors, operation_metrics, operation_timeline
from services.system_health import build_system_health
from services.voting_engine import model_status

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


def _today_count(records: list[dict]) -> int:
    today = date.today().isoformat()
    count = 0
    for item in records:
        created = str(item.get("created_at") or item.get("predict_time") or "")
        if created.startswith(today):
            count += 1
    return count


def _last_verified_at(records: list[dict]) -> str | None:
    for item in records:
        if item.get("winning_numbers"):
            return item.get("updated_at")
    return None


def _collector_metrics() -> dict:
    metrics = operation_metrics()
    components = metrics.get("components") or []
    relevant = [
        item
        for item in components
        if item.get("component") in ("collector", "official_collector", "official_catch_up", "collector_kuaishou")
    ]
    total_runs = sum(int(item.get("total_runs") or 0) for item in relevant)
    success_count = sum(int(item.get("success_count") or 0) for item in relevant)
    success_rate = round((success_count / total_runs) * 100, 2) if total_runs else 0
    return {"total_runs": total_runs, "success_count": success_count, "success_rate": success_rate}


def _latest_error() -> dict | None:
    errors = operation_errors(10).get("data") or []
    for item in errors:
        if not item.get("resolved"):
            return item
    return errors[0] if errors else None


@router.get("/status")
def api_admin_status(request: Request):
    catch_up = get_catch_up_status(fetch_source=False)
    collector = get_collector_status()
    next_prediction = build_next_prediction_dashboard()
    analysis_status = analysis_engine_status()
    models = model_status()
    history_records = get_prediction_history_records(100)
    history_count = get_prediction_history_count()
    latest_prediction = get_latest_prediction_history()
    history_stats = get_prediction_history_statistics(100)
    collector_metrics = _collector_metrics()
    latest_error = _latest_error()
    lag_count = catch_up.get("lag_count")
    system_health = build_system_health(save=False)
    error_summary = operation_error_summary()
    timeline = operation_timeline(10).get("data") or []
    operations_status = "error" if error_summary.get("unresolved", 0) else system_health.get("status", "unknown")
    operations = {
        "status": operations_status,
        "error_summary": error_summary,
        "errors": operation_errors(10).get("data") or [],
        "timeline": timeline,
    }

    database_status = "ok" if catch_up.get("database_latest_issue") else "warning"
    prediction_history_status = "ok" if history_count > 0 else "warning"
    ai_engine_status = "ok" if next_prediction.get("status") in ("ok", "empty") else "error"
    collector_status = _status_from_lag(lag_count)

    return {
        "status": "ok",
        "system": {
            "service": "ok",
            "collector": collector_status,
            "scheduler": _scheduler_status(request),
            "database": database_status,
            "prediction_history": prediction_history_status,
            "ai_engine": ai_engine_status,
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
            "today_count": _today_count(history_records),
            "latest_prediction": latest_prediction,
            "last_prediction_issue": latest_prediction.get("prediction_issue") if latest_prediction else None,
            "last_verified_at": _last_verified_at(history_records),
        },
        "analysis_engine": analysis_status,
        "model_engine": models,
        "system_health": system_health,
        "operations": operations,
        "hit_rate": history_stats,
        "collector": {
            "official_source": "Taiwan Lottery Official API",
            "last_fetch": catch_up.get("last_successful_collect_time"),
            "success_rate": collector_metrics.get("success_rate"),
            "recent_error": latest_error,
            "raw_status": collector,
        },
        "next_prediction": next_prediction,
    }
