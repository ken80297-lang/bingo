from fastapi import APIRouter

from database.prediction_history_store import (
    get_latest_prediction_history,
    get_prediction_history_records,
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
def api_prediction_history(limit: int = 30):
    return {"status": "ok", "data": get_prediction_history_records(limit)}


@router.get("/prediction-history/statistics")
def api_prediction_history_statistics(limit: int = 100):
    return {"status": "ok", "data": get_prediction_history_statistics(limit)}
