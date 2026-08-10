from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from database.learning_store import get_learning_records
from services.learning_engine import (
    ENGINE_VERSION,
    backfill_learning_records,
    get_learning_history,
    get_learning_models_summary,
    get_learning_observation,
    get_learning_status,
    get_model_performance,
    recalculate_issue,
)

router = APIRouter(prefix="/api/learning", tags=["Learning Engine"])


class RecalculateRequest(BaseModel):
    issue: str


@router.get("/status")
def api_learning_status():
    return get_learning_status()


@router.get("/models")
def api_learning_models():
    return get_learning_models_summary()


@router.get("/observation")
def api_learning_observation():
    return get_learning_observation()


@router.get("/history")
def api_learning_history(
    limit: int = 100,
    offset: int = 0,
    view: str = "summary",
    issue: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    prediction_type: str | None = None,
    verification_status: str | None = None,
    learned_status: str | None = None,
):
    filters = {
        "limit": max(1, min(int(limit or 100), 500)),
        "offset": max(0, int(offset or 0)),
        "issue": issue,
        "model_name": model_name,
        "model_version": model_version,
        "prediction_type": prediction_type,
        "verification_status": verification_status,
        "learned_status": learned_status,
    }
    if str(view or "summary").strip().lower() == "full":
        return {
            "status": "ok",
            "engine_version": ENGINE_VERSION,
            "view": "full",
            "data": get_learning_records(**filters),
        }
    payload = get_learning_history(**filters)
    payload["view"] = "summary"
    return payload


@router.get("/performance")
def api_learning_performance(
    model_name: str | None = None,
    window: str = "100",
    top_n: int | None = None,
    prediction_type: str | None = None,
):
    parsed_window: int | str = "all" if window == "all" else int(window)
    return get_model_performance(
        model_name=model_name,
        window=parsed_window,
        top_n=top_n,
        prediction_type=prediction_type,
    )


@router.post("/recalculate")
def api_learning_recalculate(payload: RecalculateRequest):
    return recalculate_issue(payload.issue)


@router.post("/backfill")
def api_learning_backfill(limit: int = 50):
    return backfill_learning_records(limit)
