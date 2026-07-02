from fastapi import APIRouter

from analysis.laowanjia import analyze
from database.cloud_draws import get_cloud_history_draws

router = APIRouter(
    prefix="/api",
    tags=["老玩家"],
)


@router.get("/laowanjia")
def api_laowanjia(limit: int = 120):
    draws = get_cloud_history_draws(limit)
    return analyze(draws)