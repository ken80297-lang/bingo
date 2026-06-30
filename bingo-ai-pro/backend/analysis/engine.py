from database.cloud_draws import get_cloud_history_draws

from analysis.hot_cold import analyze as hot_cold_analyze
from analysis.basic import analyze as basic_analyze
from analysis.consecutive import analyze as consecutive_analyze
from analysis.repeat import analyze as repeat_analyze
from analysis.missing import analyze as missing_analyze
from analysis.brother import analyze as brother_analyze
from analysis.diagonal import analyze as diagonal_analyze


def analyze_all(limit=50):
    draws = get_cloud_history_draws(limit)

    if not draws:
        return {
            "status": "error",
            "message": "沒有資料"
        }

    return {
        "status": "ok",
        "total": len(draws),
        "hot_cold": hot_cold_analyze(draws),
        "basic": basic_analyze(draws),
        "consecutive": consecutive_analyze(draws),
        "repeat": repeat_analyze(draws),
        "missing": missing_analyze(draws),
        "brother": brother_analyze(draws),
        "diagonal": diagonal_analyze(draws),
    }