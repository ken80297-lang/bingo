from __future__ import annotations

import logging
from collections import Counter, defaultdict

from database.adaptive_weight_store import get_active_adaptive_weights
from database.prediction_tracker_store import get_prediction_history
from database.strategy_evolution_store import (
    get_latest_strategy_version,
    get_next_strategy_version_number,
    get_strategy_version_history,
    save_strategy_version,
)
from database.strategy_ranking_store import get_latest_strategy_rankings

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    "hot": 0.28,
    "cold": 0.14,
    "missing": 0.12,
    "gap": 0.10,
    "tail": 0.08,
    "balance": 0.10,
    "laowanjia": 0.14,
    "exploration": 0.04,
}


def _safe_float(value, default: float = 0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize(weights: dict) -> dict:
    weights = {key: max(0.0, _safe_float(weights.get(key), DEFAULT_WEIGHTS[key])) for key in DEFAULT_WEIGHTS}
    weights["exploration"] = min(max(weights["exploration"], 0.01), 0.12)
    non_exploration_total = sum(value for key, value in weights.items() if key != "exploration")
    target_total = 1.0 - weights["exploration"]
    if non_exploration_total <= 0:
        return DEFAULT_WEIGHTS.copy()
    for key in weights:
        if key != "exploration":
            weights[key] = weights[key] / non_exploration_total * target_total
    return {key: round(value, 4) for key, value in weights.items()}


def _prediction_samples(window: int) -> list[dict]:
    samples = []
    for run in get_prediction_history(window):
        results = run.get("results") or []
        if run.get("status") != "evaluated" or not results:
            continue
        first = results[0]
        samples.append(
            {
                "issue": run.get("actual_issue") or run.get("target_issue"),
                "hit_count": int(first.get("hit_count") or 0),
                "super_hit": bool(first.get("super_hit")),
                "confidence": _safe_float(first.get("confidence")),
                "strategy": first.get("strategy") or "Adaptive",
            }
        )
    return samples


def _strategy_summary(samples: list[dict]) -> dict:
    grouped = defaultdict(list)
    for item in samples:
        grouped[item["strategy"]].append(item)
    return {
        strategy: {
            "count": len(items),
            "average_hits": round(sum(item["hit_count"] for item in items) / len(items), 4),
            "super_hit_rate": round(sum(1 for item in items if item["super_hit"]) / len(items), 4),
        }
        for strategy, items in grouped.items()
        if items
    }


def _base_from_adaptive() -> dict:
    try:
        active = get_active_adaptive_weights()
    except Exception:
        logger.exception("failed to load adaptive weights for strategy evolution")
        active = None
    if not active:
        return DEFAULT_WEIGHTS.copy()

    hot_cold = _safe_float(active.get("hot_cold_weight"), 0.35)
    return _normalize(
        {
            "hot": hot_cold * 0.62,
            "cold": hot_cold * 0.25,
            "missing": hot_cold * 0.13,
            "gap": 0.10,
            "tail": _safe_float(active.get("tail_weight"), 0.08),
            "balance": _safe_float(active.get("balance_weight"), 0.10),
            "laowanjia": _safe_float(active.get("laowanjia_weight"), 0.14),
            "exploration": _safe_float(active.get("random_weight"), 0.04),
        }
    )


def _adjust_weights(base: dict, samples: list[dict], strategy_summary: dict) -> dict:
    weights = base.copy()
    average_hits = sum(item["hit_count"] for item in samples) / len(samples) if samples else 0
    super_hit_rate = sum(1 for item in samples if item["super_hit"]) / len(samples) if samples else 0

    ranking = []
    try:
        ranking = get_latest_strategy_rankings()
    except Exception:
        logger.exception("failed to load strategy rankings for strategy evolution")

    top_strategy = (ranking[0].get("strategy") if ranking else None) or max(
        strategy_summary,
        key=lambda key: strategy_summary[key]["average_hits"],
        default="Adaptive",
    )
    top_lower = str(top_strategy).lower()

    if "laowanjia" in top_lower:
        weights["laowanjia"] += 0.04
        weights["balance"] += 0.01
    elif "hotcold" in top_lower or "hot" in top_lower:
        weights["hot"] += 0.04
        weights["cold"] += 0.02
    elif "balanced" in top_lower or "balance" in top_lower:
        weights["balance"] += 0.04
        weights["tail"] += 0.01

    if average_hits < 4:
        weights["missing"] += 0.03
        weights["exploration"] += 0.02
    elif average_hits >= 5:
        weights["hot"] += 0.02
        weights["exploration"] -= 0.01

    if super_hit_rate >= 0.30:
        weights["gap"] += 0.02
    else:
        weights["cold"] += 0.02
        weights["missing"] += 0.01

    return _normalize(weights)


def run_strategy_evolution(window: int = 100) -> dict:
    try:
        window = max(1, min(int(window), 500))
        samples = _prediction_samples(window)
        evaluated = len(samples)
        average_hits = round(sum(item["hit_count"] for item in samples) / evaluated, 4) if evaluated else 0
        super_hit_rate = round(sum(1 for item in samples if item["super_hit"]) / evaluated, 4) if evaluated else 0
        rank_score = round(average_hits * 50 + super_hit_rate * 50, 4)
        previous = get_latest_strategy_version()
        previous_average = _safe_float((previous or {}).get("average_hits"))
        is_candidate = bool(evaluated >= 50 and average_hits > previous_average)
        strategy_summary = _strategy_summary(samples)
        weights = _adjust_weights(_base_from_adaptive(), samples, strategy_summary)
        version_number = get_next_strategy_version_number()
        description = (
            f"Strategy evolution v{version_number}: evaluated {evaluated} predictions, "
            f"average_hits={average_hits}, super_hit_rate={super_hit_rate}. "
            "Candidate only; adaptive weights are not changed."
        )
        version = {
            "version": version_number,
            "window": window,
            "evaluated_predictions": evaluated,
            "average_hits": average_hits,
            "super_hit_rate": super_hit_rate,
            "rank_score": rank_score,
            "is_active": False,
            "is_candidate": is_candidate,
            "description": description,
        }
        saved = save_strategy_version(version, weights)
        payload = {
            **version,
            "candidate": is_candidate,
            "recommended_weights": weights,
            "strategy_summary": strategy_summary,
        }
        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "data": payload,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("strategy evolution failed")
        return {"status": "error", "message": str(exc), "data": None}


def latest_strategy_evolution() -> dict:
    return {"status": "ok", "data": get_latest_strategy_version()}


def strategy_evolution_history(limit: int = 20) -> dict:
    return {"status": "ok", "data": get_strategy_version_history(limit)}
