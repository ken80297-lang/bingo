from fastapi import APIRouter
from pydantic import BaseModel

from database.simulation_store import get_latest_simulation_run, get_simulation_history
from services.simulation_model import run_simulation

router = APIRouter(prefix="/api/simulation", tags=["Simulation"])


class SimulationRequest(BaseModel):
    window: int = 100
    groups: int = 5
    numbers_per_group: int = 10


@router.post("/run")
def api_simulation_run(payload: SimulationRequest):
    return run_simulation(
        window=payload.window,
        groups=payload.groups,
        numbers_per_group=payload.numbers_per_group,
    )


@router.get("/latest")
def api_simulation_latest():
    return {
        "status": "ok",
        "data": get_latest_simulation_run(),
    }


@router.get("/history")
def api_simulation_history(limit: int = 20):
    return {
        "status": "ok",
        "data": get_simulation_history(limit),
    }
