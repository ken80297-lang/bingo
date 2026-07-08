from __future__ import annotations

import logging

from database.adaptive_weight_store import get_active_adaptive_weights
from database.data_quality_store import get_data_quality_status
from database.recommendation_center_store import save_recommendation_run
from database.simulation_store import get_latest_simulation_run
from database.strategy_ranking_store import get_latest_strategy_rankings
from db import get_latest_draw

logger = logging.getLogger(__name__)

QUALITY_SCORE = {
    "ok": 1.0,
    "warning": 0.8,
    "error": 0.5,
    "unknown": 0.6,
}


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
    try:
        latest = get_latest_draw()
        return latest.get("issue") if latest else None
    except Exception:
        logger.exception("failed to load latest issue for recommendation center")
        return None


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


def _confidence(total_score: float, max_total_score: float, rank_score: float, hit_rate: float, quality_status: str) -> float:
    value = (
        _normalize_score(total_score, max_total_score) * 0.40
        + _normalize_score(rank_score, 300) * 0.30
        + max(0, min(1, hit_rate)) * 0.20
        + _quality_value(quality_status) * 0.10
    ) * 100
    return round(max(0, min(100, value)), 2)


def _explanation(strategy: str, hit_rate: float, weight_source: str, quality_status: str, confidence: float) -> str:
    return (
        f"Current best strategy is {strategy}. "
        f"Recent walk-forward hit_rate is {hit_rate:.4f}. "
        f"The system uses {weight_source} weights. "
        f"Data Quality status is {quality_status}. "
        f"This group confidence is {confidence:.2f}%."
    )


def generate_recommendation_center() -> dict:
    try:
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
        issue = _latest_issue()
        target_issue = _target_issue(issue)

        candidates = simulation.get("results", [])[:5]
        max_total_score = max(_safe_float(item.get("total_score")) for item in candidates) or 1
        best_strategy = best.get("strategy", "Adaptive")
        rank_score = _safe_float(best.get("rank_score"))
        strategy_hit_rate = _safe_float(best.get("hit_rate") or (adaptive or {}).get("hit_rate"))
        weight_source = "adaptive" if adaptive else "default"

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
        )
        run = {
            "issue": issue,
            "target_issue": target_issue,
            "best_strategy": best_strategy,
            "confidence": run_confidence,
            "data_quality_status": quality_status,
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

