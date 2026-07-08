from __future__ import annotations

import logging
import random
from collections import Counter

from database.adaptive_weight_store import get_active_adaptive_weights
from database.analysis_store import get_analysis_history, get_latest_analysis_history
from database.collector_store import (
    get_kuaishou_history,
    get_latest_draw_history,
    get_latest_kuaishou_snapshot,
)
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
    "\u8d85\u7d1a\u734e\u865f",
    "\u8d85\u7d1a\u865f",
    "\u8d85\u7d1a\u734e\u865f\u865f\u78bc",
]

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


def _safe_float(value, default: float = 0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_score(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0
    return max(0, min(1, value / maximum))


def _issue_sort_key(issue: str) -> tuple[int, str]:
    try:
        return (0, f"{int(issue):020d}")
    except Exception:
        return (1, issue)


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


def _recent_missing_numbers(super_numbers: list[int], recent_window: int = 20) -> list[int]:
    recent = set(super_numbers[:recent_window])
    return [number for number in range(1, 81) if number not in recent]


def _gap_pattern_candidates(super_numbers: list[int], trend: set[int]) -> set[int]:
    candidates = set()
    if super_numbers:
        latest = super_numbers[0]
        for gap in [1, 2, 9, 10, 11]:
            for value in [latest - gap, latest + gap]:
                if 1 <= value <= 80:
                    candidates.add(value)
    for number in trend:
        for gap in [1, 2, 9, 10, 11]:
            for value in [number - gap, number + gap]:
                if 1 <= value <= 80:
                    candidates.add(value)
    return candidates


def _dynamic_super_reason(
    number: int,
    hot: list[int],
    cold: list[int],
    recent_missing: list[int],
    gap_candidates: set[int],
    explored: bool,
) -> str:
    parts = []
    if number in hot:
        parts.append("\u71b1\u9580\u8d8b\u52e2")
    if number in cold or number in recent_missing:
        parts.append("\u88dc\u865f\u6a5f\u6703")
    if number in gap_candidates:
        parts.append("\u5dee\u503c\u578b\u614b")
    if explored:
        parts.append("issue seed \u63a2\u7d22")
    return " + ".join(parts) if parts else "\u5e73\u8861\u5019\u9078"


def _build_super_recommendation(simulation: dict, adaptive: dict | None, best: dict, issue: str | None) -> dict:
    super_numbers = _load_super_numbers(100)
    counter = Counter(super_numbers)
    hot = [number for number, _ in counter.most_common(5)]
    cold_pool = [number for number in range(1, 81) if number not in counter]
    cold = (cold_pool + [number for number, _ in counter.most_common()[-5:]])[:5]
    recent_missing = _recent_missing_numbers(super_numbers)
    trend = _simulation_trend_numbers(simulation)
    gap_candidates = _gap_pattern_candidates(super_numbers, trend)
    adaptive_hit_rate = _safe_float((adaptive or {}).get("hit_rate"))
    strategy_factor = _normalize_score(_safe_float(best.get("rank_score")), 300)
    max_count = max(counter.values()) if counter else 1
    based_on_issue = issue or simulation.get("source_issue")
    rng = random.Random(f"super:{based_on_issue or 'unknown'}")

    scored = []
    for number in range(1, 81):
        hot_score = _normalize_score(counter.get(number, 0), max_count)
        cold_rebound_score = 1 if number in cold_pool else 1 - hot_score
        recent_missing_score = 1 if number in recent_missing else 0
        gap_score = 1 if number in gap_candidates else 0
        exploration_score = min(1, rng.random() + (adaptive_hit_rate * 0.05) + (strategy_factor * 0.05))
        explored = exploration_score >= 0.92
        score = (
            hot_score * 0.35
            + cold_rebound_score * 0.25
            + recent_missing_score * 0.20
            + gap_score * 0.10
            + exploration_score * 0.10
        ) * 100
        scored.append(
            {
                "number": number,
                "confidence": round(max(0, min(100, score)), 2),
                "reason": _dynamic_super_reason(number, hot, cold, recent_missing, gap_candidates, explored),
            }
        )

    scored.sort(key=lambda item: item["confidence"], reverse=True)
    recommended = []
    source_checks = [
        lambda item: item["number"] in hot,
        lambda item: item["number"] in cold or item["number"] in recent_missing,
        lambda item: item["number"] in gap_candidates,
        lambda item: "issue seed" in item["reason"],
    ]
    for check in source_checks:
        for item in scored:
            if item["number"] not in [picked["number"] for picked in recommended] and check(item):
                recommended.append(item)
                break
        if len(recommended) == 3:
            break
    for item in scored:
        if len(recommended) == 3:
            break
        if item["number"] not in [picked["number"] for picked in recommended]:
            recommended.append(item)
    recommended.sort(key=lambda item: item["confidence"], reverse=True)

    return {
        "based_on_issue": based_on_issue,
        "source_issue": simulation.get("source_issue"),
        "recommended": recommended[:3],
        "hot": hot,
        "cold": cold[:5],
        "recent_missing": recent_missing[:5],
        "gap_candidates": sorted(gap_candidates)[:10],
    }


def _build_sync_status(simulation_issue: str | None, recommendation_issue: str | None, super_issue: str | None) -> dict:
    values = [
        str(value)
        for value in [simulation_issue, recommendation_issue, super_issue]
        if value not in (None, "")
    ]
    return {
        "status": "ok" if len(values) == 3 and len(set(values)) == 1 else "warning",
        "simulation_issue": simulation_issue,
        "recommendation_issue": recommendation_issue,
        "super_issue": super_issue,
    }


def _confidence(total_score: float, max_total_score: float, rank_score: float, hit_rate: float, quality_status: str) -> float:
    value = (
        _normalize_score(total_score, max_total_score) * 0.40
        + _normalize_score(rank_score, 300) * 0.30
        + max(0, min(1, hit_rate)) * 0.20
        + _quality_value(quality_status) * 0.10
    ) * 100
    return round(max(0, min(100, value)), 2)


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
        super_recommendation = _build_super_recommendation(simulation, adaptive, best, issue)
        super_issue = super_recommendation.get("based_on_issue") or super_recommendation.get("source_issue")
        if issue and str(super_issue) != str(issue):
            super_recommendation = _build_super_recommendation(simulation, adaptive, best, issue)
            super_issue = super_recommendation.get("based_on_issue") or super_recommendation.get("source_issue")
        sync = _build_sync_status(simulation.get("source_issue"), issue, super_issue)

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
            "sync": sync,
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
