from __future__ import annotations

import logging

from database.adaptive_weight_store import get_active_adaptive_weights
from database.analysis_store import get_analysis_history
from database.simulation_evaluation_store import get_latest_simulation_evaluation
from database.strategy_ranking_store import save_strategy_rankings

logger = logging.getLogger(__name__)


def _clamp(value: float, minimum: float = 0, maximum: float = 1) -> float:
    return max(minimum, min(maximum, value))


def _rank_score(average_hits: float, hit_rate: float) -> float:
    return round((average_hits * 50) + (hit_rate * 50), 4)


def _strategy_row(strategy: str, evaluation: dict, average_hits: float, hit_rate: float) -> dict:
    return {
        "strategy": strategy,
        "window": evaluation.get("window"),
        "evaluated_issues": evaluation.get("evaluated_issues"),
        "average_hits": round(max(0, average_hits), 4),
        "best_hits": evaluation.get("best_hits"),
        "hit_rate": round(_clamp(hit_rate), 4),
        "hit_distribution": evaluation.get("hit_distribution") or {},
        "rank_score": _rank_score(max(0, average_hits), _clamp(hit_rate)),
        "is_current": True,
    }


def _weight_value(weights: dict | None, key: str, default: float) -> float:
    if not weights:
        return default
    try:
        return float(weights.get(key) or default)
    except Exception:
        return default


def build_strategy_rankings() -> dict:
    try:
        evaluation = get_latest_simulation_evaluation()
        if not evaluation:
            return {
                "status": "error",
                "message": "no simulation evaluation available",
                "rankings": [],
            }

        active_weights = get_active_adaptive_weights()
        try:
            get_analysis_history(1)
        except Exception:
            logger.exception("analysis history read failed while building strategy rankings")

        base_average_hits = float(evaluation.get("average_hits") or 0)
        base_hit_rate = float(evaluation.get("hit_rate") or 0)
        laowanjia_weight = _weight_value(active_weights, "laowanjia_weight", 0.30)
        hot_cold_weight = _weight_value(active_weights, "hot_cold_weight", 0.35)
        balance_weight = _weight_value(active_weights, "balance_weight", 0.20)
        tail_weight = _weight_value(active_weights, "tail_weight", 0.10)

        adaptive_factor = 1.0 if active_weights else 0.98
        laowanjia_factor = 0.9 + laowanjia_weight
        hot_cold_factor = 0.9 + hot_cold_weight
        balanced_factor = 0.85 + balance_weight + (tail_weight * 0.5)

        rankings = [
            _strategy_row(
                "Adaptive",
                evaluation,
                base_average_hits * adaptive_factor,
                base_hit_rate * adaptive_factor,
            ),
            _strategy_row(
                "Laowanjia",
                evaluation,
                base_average_hits * laowanjia_factor,
                base_hit_rate * laowanjia_factor,
            ),
            _strategy_row(
                "HotCold",
                evaluation,
                base_average_hits * hot_cold_factor,
                base_hit_rate * hot_cold_factor,
            ),
            _strategy_row(
                "Balanced",
                evaluation,
                base_average_hits * balanced_factor,
                base_hit_rate * balanced_factor,
            ),
        ]
        rankings.sort(key=lambda row: row["rank_score"], reverse=True)
        saved = save_strategy_rankings(rankings)

        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "source_evaluation_id": evaluation.get("id"),
            "active_adaptive_weight_id": active_weights.get("id") if active_weights else None,
            "rankings": rankings,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("strategy ranking update failed")
        return {"status": "error", "message": str(exc), "rankings": []}

