from __future__ import annotations

import logging

from database.adaptive_weight_store import (
    get_active_adaptive_weights,
    get_adaptive_weight_history,
    save_adaptive_weights,
)
from database.simulation_evaluation_store import get_latest_simulation_evaluation

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    "laowanjia_weight": 0.30,
    "hot_cold_weight": 0.35,
    "balance_weight": 0.20,
    "tail_weight": 0.10,
    "random_weight": 0.05,
}


def _base_weights() -> dict:
    active = get_active_adaptive_weights()
    if not active:
        return DEFAULT_WEIGHTS.copy()
    return {
        "laowanjia_weight": float(active.get("laowanjia_weight") or DEFAULT_WEIGHTS["laowanjia_weight"]),
        "hot_cold_weight": float(active.get("hot_cold_weight") or DEFAULT_WEIGHTS["hot_cold_weight"]),
        "balance_weight": float(active.get("balance_weight") or DEFAULT_WEIGHTS["balance_weight"]),
        "tail_weight": float(active.get("tail_weight") or DEFAULT_WEIGHTS["tail_weight"]),
        "random_weight": float(active.get("random_weight") or DEFAULT_WEIGHTS["random_weight"]),
    }


def _normalize(weights: dict) -> dict:
    weights["tail_weight"] = max(float(weights.get("tail_weight", 0)), 0.05)
    weights["random_weight"] = min(max(float(weights.get("random_weight", 0)), 0), 0.25)

    for key in DEFAULT_WEIGHTS:
        weights[key] = max(float(weights.get(key, 0)), 0)

    total = sum(weights.values())
    if total <= 0:
        return DEFAULT_WEIGHTS.copy()

    normalized = {key: value / total for key, value in weights.items()}

    if normalized["tail_weight"] < 0.05:
        shortage = 0.05 - normalized["tail_weight"]
        normalized["tail_weight"] = 0.05
        _reduce_other_weights(normalized, shortage, exclude={"tail_weight"})

    if normalized["random_weight"] > 0.25:
        overflow = normalized["random_weight"] - 0.25
        normalized["random_weight"] = 0.25
        _add_to_other_weights(normalized, overflow, exclude={"random_weight"})

    total = sum(normalized.values())
    return {key: round(value / total, 6) for key, value in normalized.items()}


def _reduce_other_weights(weights: dict, amount: float, exclude: set[str]) -> None:
    keys = [key for key in weights if key not in exclude]
    available = sum(weights[key] for key in keys)
    if available <= 0:
        return
    for key in keys:
        weights[key] = max(0, weights[key] - amount * (weights[key] / available))


def _add_to_other_weights(weights: dict, amount: float, exclude: set[str]) -> None:
    keys = [key for key in weights if key not in exclude]
    available = sum(weights[key] for key in keys)
    if available <= 0:
        share = amount / len(keys)
        for key in keys:
            weights[key] += share
        return
    for key in keys:
        weights[key] += amount * (weights[key] / available)


def _next_version() -> int:
    try:
        history = get_adaptive_weight_history(1)
        if history:
            return int(history[0].get("version") or 0) + 1
    except Exception:
        logger.exception("failed to load adaptive weight history for version")
    return 1


def update_adaptive_weights() -> dict:
    try:
        evaluation = get_latest_simulation_evaluation()
        if not evaluation:
            return {
                "status": "error",
                "message": "no simulation evaluation available",
                "weights": None,
            }

        if evaluation.get("strategy") != "walk_forward" or not evaluation.get("leakage_safe"):
            return {
                "status": "error",
                "message": "latest evaluation is not leakage-safe walk-forward",
                "weights": None,
            }

        weights = _base_weights()
        hit_rate = float(evaluation.get("hit_rate") or 0)

        if hit_rate >= 0.65:
            weights["laowanjia_weight"] += 0.04
            weights["hot_cold_weight"] += 0.04
            weights["balance_weight"] -= 0.03
            weights["random_weight"] -= 0.02
        elif hit_rate < 0.5:
            weights["random_weight"] += 0.06
            weights["laowanjia_weight"] -= 0.05
            weights["hot_cold_weight"] -= 0.02
            weights["balance_weight"] += 0.01

        normalized = _normalize(weights)
        payload = {
            "version": _next_version(),
            "strategy": "adaptive_from_walk_forward",
            "window": evaluation.get("window"),
            **normalized,
            "average_hits": evaluation.get("average_hits"),
            "hit_rate": hit_rate,
            "source_evaluation_id": evaluation.get("id"),
            "is_active": True,
        }
        saved = save_adaptive_weights(payload)

        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "weights": payload,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("adaptive weight update failed")
        return {"status": "error", "message": str(exc), "weights": None}

