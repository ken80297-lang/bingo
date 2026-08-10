from fastapi import APIRouter

from database.prediction_history_store import (
    get_latest_prediction_history,
    get_prediction_history_records,
    get_prediction_history_summary_records,
    get_prediction_history_statistics,
)
from services.next_prediction_center import build_next_prediction_dashboard

router = APIRouter(prefix="/api", tags=["Next Prediction"])


@router.get("/next-prediction")
def api_next_prediction():
    return build_next_prediction_dashboard()


@router.get("/prediction-history/latest")
def api_prediction_history_latest():
    return {"status": "ok", "data": get_latest_prediction_history()}


@router.get("/prediction-history/history")
def api_prediction_history(limit: int | None = None, view: str = "full"):
    normalized_view = str(view or "full").strip().lower()
    if normalized_view == "summary":
        safe_limit = max(1, min(int(limit or 20), 100))
        data = get_prediction_history_summary_records(safe_limit)
    else:
        safe_limit = max(1, min(int(limit or 30), 500))
        data = get_prediction_history_records(safe_limit)
    return {"status": "ok", "view": "summary" if normalized_view == "summary" else "full", "data": data}


@router.get("/prediction-history/statistics")
def api_prediction_history_statistics(limit: int = 100):
    return {"status": "ok", "data": get_prediction_history_statistics(limit)}
