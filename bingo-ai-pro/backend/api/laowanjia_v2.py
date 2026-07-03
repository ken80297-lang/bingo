from fastapi import APIRouter

from analysis.laowanjia_v2 import analyze_v2
from database.cloud_draws import get_cloud_history_draws

router = APIRouter(
    prefix="/api",
    tags=["老玩家 AI Pro"],
)


@router.get("/laowanjia-v2")
def api_laowanjia_v2(limit: int = 120):
    draws = get_cloud_history_draws(limit)
    return analyze_v2(draws)