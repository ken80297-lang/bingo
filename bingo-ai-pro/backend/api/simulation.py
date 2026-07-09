from fastapi import APIRouter
from pydantic import BaseModel

from database.simulation_store import (
    get_latest_simulation_run,
    get_simulation_history,
    get_simulation_run_by_issue,
)
from services.simulation_model import get_production_latest_issue, run_simulation

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
    latest_issue = get_production_latest_issue()
    if latest_issue:
        production_run = get_simulation_run_by_issue(latest_issue)
        if production_run:
            return {
                "status": "ok",
                "latest_issue": latest_issue,
                "data": production_run,
            }

        fallback = get_latest_simulation_run()
        return {
            "status": "outdated",
            "latest_issue": latest_issue,
            "simulation_issue": fallback.get("source_issue") if fallback else None,
            "data": fallback,
        }

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
