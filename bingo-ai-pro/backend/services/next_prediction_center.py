from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from database.analysis_store import get_latest_analysis_history
from database.prediction_history_store import (
    get_latest_prediction_history,
    get_prediction_history_statistics,
    save_prediction_history,
)
from database.recommendation_center_store import get_latest_recommendation_run

logger = logging.getLogger(__name__)


def _as_int_list(values: Any) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    return numbers


def _pairs(numbers: list[int], diff: int) -> list[list[int]]:
    number_set = set(numbers)
    return [[n, n + diff] for n in numbers if n + diff in number_set]


def _tails(numbers: list[int]) -> list[int]:
    return sorted({number % 10 for number in numbers})


def _big_small(numbers: list[int]) -> str:
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "偏大"
    if small > big:
        return "偏小"
    return "均衡"


def _odd_even(numbers: list[int]) -> str:
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "偏單"
    if even > odd:
        return "偏雙"
    return "均衡"


def _patch_numbers(numbers: list[int]) -> list[int]:
    candidates = []
    for number in numbers:
        for diff in (1, 2, 10):
            for value in (number - diff, number + diff):
                if 1 <= value <= 80 and value not in numbers and value not in candidates:
                    candidates.append(value)
    return candidates[:8]


def _super_number(recommendation: dict) -> int | None:
    super_recommendation = recommendation.get("super_recommendation") or {}
    recommended = super_recommendation.get("recommended") or []
    if recommended:
        try:
            return int(recommended[0].get("number"))
        except Exception:
            return None
    return None


def _reasons(recommendation: dict, numbers: list[int], laowanjia: dict | None) -> list[str]:
    reasons = []
    if recommendation.get("best_strategy"):
        reasons.append(f"目前最佳策略為 {recommendation.get('best_strategy')}，AI 依最新模擬結果排序。")
    if _pairs(numbers, 2):
        reasons.append("號碼組合包含雙生號，符合老玩家常看的貼近型態。")
    if _pairs(numbers, 1):
        reasons.append("本組含連號，適合觀察短線群聚延伸。")
    score = (laowanjia or {}).get("total_laowanjia_feature_score")
    if score is None:
        score = (laowanjia or {}).get("laowanjia_score")
    if laowanjia and (score or 0) >= 60:
        reasons.append("老玩家特徵分數偏高，模式成立度較佳。")
    if not reasons:
        reasons.append("AI 依據近期模擬分數、資料品質與策略排名產生本期推薦。")
    return reasons[:5]


def _possible_star(analysis: dict | None) -> str:
    if not analysis:
        return "三星/四星觀察"
    if analysis.get("six_star"):
        return "六星觀察"
    if analysis.get("five_star"):
        return "五星觀察"
    if analysis.get("four_star"):
        return "四星觀察"
    if analysis.get("three_star"):
        return "三星觀察"
    return "三星/四星觀察"


def _alert_level(value: int) -> dict:
    value = max(0, min(5, int(value or 0)))
    return {"stars": value, "percent": value * 20}


def _alerts(numbers: list[int], super_number: int | None) -> dict:
    consecutive = len(_pairs(numbers, 1))
    twins = len(_pairs(numbers, 2))
    tails = _tails(numbers)
    cluster = max(
        sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 81, 10)
    ) if numbers else 0
    patch = len(_patch_numbers(numbers))
    return {
        "cluster_alert": _alert_level(cluster - 2),
        "patch_alert": _alert_level(patch // 2),
        "twin_alert": _alert_level(twins),
        "consecutive_alert": _alert_level(consecutive),
        "super_alert": _alert_level(3 if super_number else 1),
    }


def build_prediction_history_record(recommendation: dict) -> dict | None:
    results = recommendation.get("results") or []
    first = results[0] if results else {}
    numbers = _as_int_list(first.get("numbers"))
    if not numbers:
        return None
    reasons = _reasons(recommendation, numbers, None)
    return {
        "issue": recommendation.get("issue"),
        "prediction_issue": recommendation.get("target_issue"),
        "predict_time": datetime.utcnow().isoformat(),
        "strategy": recommendation.get("best_strategy") or first.get("strategy") or "AI",
        "confidence": recommendation.get("confidence") or first.get("confidence"),
        "recommend_numbers": numbers,
        "super_number": _super_number(recommendation),
        "three_star": numbers[:3],
        "four_star": numbers[:4],
        "twins": _pairs(numbers, 2),
        "consecutive": _pairs(numbers, 1),
        "patch_numbers": _patch_numbers(numbers),
        "tails": _tails(numbers),
        "big_small": _big_small(numbers),
        "odd_even": _odd_even(numbers),
        "reasons": reasons,
        "model_scores": recommendation.get("model_scores") or {},
        "winning_model": recommendation.get("winning_model"),
    }


def save_recommendation_prediction_history(recommendation: dict) -> dict:
    try:
        record = build_prediction_history_record(recommendation)
        if not record:
            return {"status": "skipped", "message": "no recommendation numbers"}
        return save_prediction_history(record)
    except Exception as exc:
        logger.exception("failed to save recommendation prediction history")
        return {"status": "error", "message": str(exc)}


def _fallback_recommendation() -> dict | None:
    recommendation = get_latest_recommendation_run()
    if not recommendation:
        return None
    record = build_prediction_history_record(recommendation)
    if not record:
        return None
    record["reasons"] = _reasons(recommendation, record["recommend_numbers"], None)
    return record


def build_next_prediction_dashboard() -> dict:
    latest_history = get_latest_prediction_history()
    fallback = None if latest_history else _fallback_recommendation()
    prediction = latest_history or fallback
    analysis = get_latest_analysis_history()
    stats = get_prediction_history_statistics(100)

    if not prediction:
        message = "尚未累積 AI 預測紀錄，系統已開始保存後續推薦。"
        return {
            "status": "empty",
            "message": message,
            "next_recommendation": {"message": message},
            "laowanjia": {"message": "尚無老玩家特徵資料。"},
            "reasons": [message],
            "alerts": {},
            "history": stats,
        }

    numbers = _as_int_list(prediction.get("recommend_numbers"))
    super_number = prediction.get("super_number")
    laowanjia_score = (analysis or {}).get("laowanjia_score") or 0
    conclusion = "老玩家模式成立" if laowanjia_score >= 60 else "老玩家模式觀察中"

    return {
        "status": "ok",
        "next_recommendation": {
            "prediction_issue": prediction.get("prediction_issue"),
            "confidence": prediction.get("confidence") or 0,
            "candidates": numbers,
            "super_number": super_number,
            "three_star": prediction.get("three_star") or numbers[:3],
            "four_star": prediction.get("four_star") or numbers[:4],
            "twins": prediction.get("twins") or _pairs(numbers, 2),
            "consecutive": prediction.get("consecutive") or _pairs(numbers, 1),
            "patch_numbers": prediction.get("patch_numbers") or _patch_numbers(numbers),
            "tails": prediction.get("tails") or _tails(numbers),
            "big_small": prediction.get("big_small") or _big_small(numbers),
            "odd_even": prediction.get("odd_even") or _odd_even(numbers),
            "model_scores": prediction.get("model_scores") or {},
            "winning_model": prediction.get("winning_model"),
        },
        "laowanjia": {
            "laowanjia_score": laowanjia_score,
            "current_pattern": (analysis or {}).get("pattern") or conclusion,
            "hot_zone": (analysis or {}).get("hot_zone") or "觀察最新熱區",
            "patch_numbers": prediction.get("patch_numbers") or _patch_numbers(numbers),
            "possible_star": _possible_star(analysis),
            "diagonal_score": (analysis or {}).get("diagonal_score", 0),
            "gap_score": (analysis or {}).get("gap_score", 0),
            "twins": prediction.get("twins") or _pairs(numbers, 2),
            "super_number": super_number,
            "big_small": prediction.get("big_small") or _big_small(numbers),
            "odd_even": prediction.get("odd_even") or _odd_even(numbers),
            "tails": prediction.get("tails") or _tails(numbers),
            "conclusion": (analysis or {}).get("ai_pattern") or conclusion,
        },
        "reasons": prediction.get("reasons") or _reasons({}, numbers, analysis),
        "confidence": prediction.get("confidence") or 0,
        "alerts": _alerts(numbers, super_number),
        "history": stats,
        "fallback": latest_history is None,
    }
