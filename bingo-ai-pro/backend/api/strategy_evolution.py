from fastapi import APIRouter

from services.strategy_evolution import (
    latest_strategy_evolution,
    run_strategy_evolution,
    strategy_evolution_history,
)

router = APIRouter(prefix="/api/strategy-evolution", tags=["Strategy Evolution"])


@router.post("/run")
def api_strategy_evolution_run():
    return run_strategy_evolution()


@router.get("/latest")
def api_strategy_evolution_latest():
    return latest_strategy_evolution()


@router.get("/history")
def api_strategy_evolution_history(limit: int = 20):
    return strategy_evolution_history(limit)
