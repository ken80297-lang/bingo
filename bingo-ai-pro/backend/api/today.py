from datetime import datetime

from fastapi import APIRouter

from analysis.laowanjia_v3 import analyze_v3
from database.cloud_draws import get_cloud_history_draws

router = APIRouter(
    prefix="/api",
    tags=["今日推薦"],
)


@router.get("/today")
def api_today(limit: int = 120):
    draws = get_cloud_history_draws(limit)
    result = analyze_v3(draws)

    if result.get("status") != "ok":
        return result

    recommend = result["recommend"]

    return {
        "status": "ok",
        "title": "今日老玩家 AI 推薦",
        "issue": result["issue"],
        "latest_numbers": result["latest_numbers"],
        "three_star": recommend["three_star"],
        "four_star": recommend["four_star"],
        "five_star": recommend["five_star"],
        "top10": recommend["top10"],
        "top20": recommend["top20"],
        "super_candidates": recommend["super_candidates"],
        "confidence": recommend["confidence"],
        "hot20": result["trend"]["hot20"],
        "hot_tails": result["trend"]["hot_tails"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }