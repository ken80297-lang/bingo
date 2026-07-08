from fastapi import APIRouter
from pydantic import BaseModel

from database.simulation_evaluation_store import (
    get_latest_simulation_evaluation,
    get_simulation_evaluation_history,
)
from services.simulation_evaluator import evaluate_latest_simulation

router = APIRouter(prefix="/api/simulation", tags=["Simulation Evaluation"])


class SimulationEvaluationRequest(BaseModel):
    window: int = 100


@router.post("/evaluate")
def api_simulation_evaluate(payload: SimulationEvaluationRequest):
    return evaluate_latest_simulation(window=payload.window)


@router.get("/evaluation/latest")
def api_simulation_evaluation_latest():
    return {
        "status": "ok",
        "data": get_latest_simulation_evaluation(),
    }


@router.get("/evaluation/history")
def api_simulation_evaluation_history(limit: int = 20):
    return {
        "status": "ok",
        "data": get_simulation_evaluation_history(limit),
    }
