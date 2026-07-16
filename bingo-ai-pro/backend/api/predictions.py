from fastapi import APIRouter

from services.prediction_lifecycle import (
    prediction_lifecycle_history,
    prediction_lifecycle_repair_dry_run,
    prediction_lifecycle_statistics,
)

router = APIRouter(prefix="/api/predictions", tags=["Prediction Lifecycle"])


@router.get("/history")
def api_predictions_history(limit: int = 50):
    return prediction_lifecycle_history(limit)


@router.get("/statistics")
def api_predictions_statistics(limit: int = 100):
    return prediction_lifecycle_statistics(limit)


@router.get("/repair/dry-run")
def api_predictions_repair_dry_run():
    return prediction_lifecycle_repair_dry_run()
