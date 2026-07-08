from __future__ import annotations

import logging
from collections import Counter

from database.adaptive_weight_store import get_active_adaptive_weights
from database.analysis_store import get_analysis_history, get_latest_analysis_history
from database.collector_store import get_kuaishou_history, get_latest_draw_history, get_latest_kuaishou_snapshot
from database.data_quality_store import get_data_quality_status
from database.recommendation_center_store import save_recommendation_run
from database.simulation_store import get_latest_simulation_run, get_simulation_run_by_issue
from database.strategy_ranking_store import get_latest_strategy_rankings
from db import get_latest_draw
from services.simulation_model import ensure_simulation_for_issue

logger = logging.getLogger(__name__)

QUALITY_SCORE = {
    "ok": 1.0,
    "warning": 0.8,
    "error": 0.5,
    "unknown": 0.6,
}

SUPER_KEYS = [
    "super_number",
    "superNumber",
    "super",
    "超級獎號",
    "超級號",
    "超級獎號號碼",
]


def _safe_float(value, default: float = 0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_score(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0
    return max(0, min(1, value / maximum))


def _latest_issue() -> str | None:
    candidates = []
    for loader in [get_latest_analysis_history, get_latest_kuaishou_snapshot, get_latest_draw_history]:
        try:
            item = loader()
            if item and item.get("issue") is not None:
                candidates.append(str(item.get("issue")))
        except Exception:
            logger.exception("failed to load latest collector issue for recommendation center")

    try:
        latest = get_latest_draw()
        if latest and latest.get("issue") is not None:
            candidates.append(str(latest.get("issue")))
    except Exception:
        logger.exception("failed to load latest issue for recommendation center")

    if not candidates:
        return None
    numeric = [issue for issue in candidates if _issue_sort_key(issue)[0] == 0]
    if numeric:
        return sorted(numeric, key=_issue_sort_key)[-1]
    return sorted(candidates)[-1]


def _issue_sort_key(issue: str) -> tuple[int, str]:
    try:
        return (0, f"{int(issue):020d}")
    except Exception:
        return (1, issue)


def _target_issue(issue: str | None) -> str | None:
    try:
        return str(int(issue) + 1) if issue is not None else None
    except Exception:
        return None


def _best_strategy(rankings: list[dict]) -> dict:
    if not rankings:
        return {
            "strategy": "Adaptive",
            "rank_score": 0,
            "hit_rate": 0,
        }
    return rankings[0]


def _quality_value(status: str) -> float:
    return QUALITY_SCORE.get((status or "unknown").lower(), QUALITY_SCORE["unknown"])


def _as_super_number(value) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if 1 <= number <= 80 else None


def _find_super_in_mapping(value) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in SUPER_KEYS:
        number = _as_super_number(value.get(key))
        if number is not None:
            return number
    for nested in value.values():
        if isinstance(nested, dict):
            number = _find_super_in_mapping(nested)
            if number is not None:
                return number
        elif isinstance(nested, list):
            for item in nested:
                number = _find_super_in_mapping(item)
                if number is not None:
                    return number
    return None


def _load_super_numbers(window: int = 100) -> list[int]:
    numbers = []
    try:
        for item in get_analysis_history(window):
            number = _as_super_number(item.get("super_number"))
            if number is not None:
                numbers.append(number)
    except Exception:
        logger.exception("failed to load analysis super numbers")

    if len(numbers) >= min(window, 20):
        return numbers[:window]

    try:
        for item in get_kuaishou_history(window):
            parsed = item.get("parsed_json") or {}
            number = _find_super_in_mapping(parsed)
            if number is not None:
                numbers.append(number)
    except Exception:
        logger.exception("failed to load kuaishou super numbers")

    return numbers[:window]


def _simulation_trend_numbers(simulation: dict) -> set[int]:
    features = simulation.get("features") or {}
    candidates = []
    for key in ["hot_numbers", "recent_repeat_numbers", "cold_numbers"]:
        candidates.extend(features.get(key) or [])
    result = set()
    for value in candidates:
        number = _as_super_number(value)
        if number is not None:
            result.add(number)
    return result


def _super_reason(number: int, hot: list[int], cold: list[int], trend: set[int]) -> str:
    if number in hot and number in trend:
        return "hot + recent trend"
    if number in cold:
        return "cold rebound"
    if number in trend:
        return "strategy balance"
    return "balanced candidate"


def _build_super_recommendation(simulation: dict, adaptive: dict | None, best: dict) -> dict:
    super_numbers = _load_super_numbers(100)
    counter = Counter(super_numbers)
    hot = [number for number, _ in counter.most_common(5)]
    cold_pool = [number for number in range(1, 81) if number not in counter]
    cold = (cold_pool + [number for number, _ in counter.most_common()[-5:]])[:5]
    trend = _simulation_trend_numbers(simulation)
    adaptive_hit_rate = _safe_float((adaptive or {}).get("hit_rate"))
    strategy_factor = _normalize_score(_safe_float(best.get("rank_score")), 300)
    max_count = max(counter.values()) if counter else 1

    scored = []
    for number in range(1, 81):
        heat = _normalize_score(counter.get(number, 0), max_count)
        missing = 1 if number not in counter else 1 - heat
        strategy_bonus = 0.6
        if number in trend:
            strategy_bonus += 0.25
        strategy_bonus += min(0.15, adaptive_hit_rate * 0.15)
        score = ((heat * 0.50) + (missing * 0.30) + (strategy_factor * strategy_bonus * 0.20)) * 100
        scored.append(
            {
                "number": number,
                "confidence": round(max(0, min(100, score)), 2),
                "reason": _super_reason(number, hot, cold, trend),
            }
        )

    scored.sort(key=lambda item: item["confidence"], reverse=True)
    return {
        "recommended": scored[:3],
        "hot": hot,
        "cold": cold[:5],
    }


def _confidence(total_score: float, max_total_score: float, rank_score: float, hit_rate: float, quality_status: str) -> float:
    value = (
        _normalize_score(total_score, max_total_score) * 0.40
        + _normalize_score(rank_score, 300) * 0.30
        + max(0, min(1, hit_rate)) * 0.20
        + _quality_value(quality_status) * 0.10
    ) * 100
    return round(max(0, min(100, value)), 2)


FEATURE_LABELS = {
    "consecutive": "consecutive numbers",
    "missing": "missing-number rebound",
    "big_small_balance": "big/small balance",
    "odd_even_balance": "odd/even balance",
    "diagonal": "diagonal pattern",
    "gap": "gap pattern",
    "repeat": "repeat numbers",
    "twin": "twin numbers",
}


def _laowanjia_text(scores: dict | None) -> str:
    if not scores:
        return "Laowanjia feature score is not available."
    feature_score = _safe_float(scores.get("laowanjia_feature_score"))
    matches = scores.get("laowanjia_feature_matches") or []
    if not matches:
        return f"Laowanjia feature score is {feature_score:.2f}."
    labels = [FEATURE_LABELS.get(item, item) for item in matches[:3]]
    return (
        f"This group matches {', '.join(labels)}, "
        f"so its Laowanjia feature score is {feature_score:.2f}."
    )


def _explanation(
    strategy: str,
    hit_rate: float,
    weight_source: str,
    quality_status: str,
    confidence: float,
    scores: dict | None = None,
) -> str:
    return (
        f"Current best strategy is {strategy}. "
        f"Recent walk-forward hit_rate is {hit_rate:.4f}. "
        f"The system uses {weight_source} weights. "
        f"Data Quality status is {quality_status}. "
        f"{_laowanjia_text(scores)} "
        f"This group confidence is {confidence:.2f}%."
    )


def generate_recommendation_center() -> dict:
    try:
        issue = _latest_issue()
        simulation = get_simulation_run_by_issue(issue) if issue else None
        if issue and not simulation:
            created = ensure_simulation_for_issue(issue, window=100, groups=5, numbers_per_group=10)
            if created.get("status") == "ok":
                simulation = get_simulation_run_by_issue(issue)
        if not simulation:
            simulation = get_latest_simulation_run()
        if not simulation or not simulation.get("results"):
            return {
                "status": "error",
                "message": "no latest simulation available",
                "recommendation": None,
            }

        rankings = get_latest_strategy_rankings()
        best = _best_strategy(rankings)
        adaptive = get_active_adaptive_weights()
        quality = get_data_quality_status()
        quality_status = quality.get("status", "unknown")
        issue = simulation.get("source_issue") or issue
        target_issue = _target_issue(issue)

        candidates = simulation.get("results", [])[:5]
        max_total_score = max(_safe_float(item.get("total_score")) for item in candidates) or 1
        best_strategy = best.get("strategy", "Adaptive")
        rank_score = _safe_float(best.get("rank_score"))
        strategy_hit_rate = _safe_float(best.get("hit_rate") or (adaptive or {}).get("hit_rate"))
        weight_source = "adaptive" if adaptive else "default"
        super_recommendation = _build_super_recommendation(simulation, adaptive, best)

        results = []
        confidences = []
        for index, item in enumerate(candidates, start=1):
            total_score = _safe_float(item.get("total_score"))
            confidence = _confidence(
                total_score,
                max_total_score,
                rank_score,
                strategy_hit_rate,
                quality_status,
            )
            explanation = _explanation(
                best_strategy,
                strategy_hit_rate,
                weight_source,
                quality_status,
                confidence,
                item.get("scores"),
            )
            confidences.append(confidence)
            results.append(
                {
                    "rank": index,
                    "numbers": item.get("numbers", []),
                    "confidence": confidence,
                    "total_score": total_score,
                    "strategy": best_strategy,
                    "explanation": explanation,
                }
            )

        run_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0
        run_explanation = _explanation(
            best_strategy,
            strategy_hit_rate,
            weight_source,
            quality_status,
            run_confidence,
            (candidates[0] or {}).get("scores") if candidates else None,
        )
        run = {
            "issue": issue,
            "target_issue": target_issue,
            "best_strategy": best_strategy,
            "confidence": run_confidence,
            "data_quality_status": quality_status,
            "super_recommendation": super_recommendation,
            "explanation": run_explanation,
        }
        saved = save_recommendation_run(run, results)

        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "recommendation": {
                **run,
                "results": results,
            },
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("recommendation center generation failed")
        return {"status": "error", "message": str(exc), "recommendation": None}
