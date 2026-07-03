from fastapi import APIRouter

from analysis.backtest import run_backtest
from database.cloud_draws import get_cloud_history_draws

router = APIRouter(
    prefix="/api",
    tags=["回測"],
)


@router.get("/backtest")
def api_backtest(issue: str | None = None, limit: int = 500):
    draws = get_cloud_history_draws(limit)
    return run_backtest(draws, issue)