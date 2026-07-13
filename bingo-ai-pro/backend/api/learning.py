from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from services.learning_engine import (
    backfill_learning_records,
    get_learning_history,
    get_learning_models_summary,
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


@router.get("/history")
def api_learning_history(
    limit: int = 100,
    offset: int = 0,
    issue: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    prediction_type: str | None = None,
    verification_status: str | None = None,
    learned_status: str | None = None,
):
    return get_learning_history(
        limit=limit,
        offset=offset,
        issue=issue,
        model_name=model_name,
        model_version=model_version,
        prediction_type=prediction_type,
        verification_status=verification_status,
        learned_status=learned_status,
    )


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
