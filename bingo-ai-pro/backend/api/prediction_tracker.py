from fastapi import APIRouter

from services.prediction_tracker import (
    latest_prediction,
    prediction_history,
    prediction_statistics,
)

router = APIRouter(prefix="/api/prediction", tags=["Prediction Tracker"])


@router.get("/latest")
def api_prediction_latest():
    return latest_prediction()


@router.get("/history")
def api_prediction_history(limit: int = 30):
    return prediction_history(limit)


@router.get("/statistics")
def api_prediction_statistics():
    return prediction_statistics()
