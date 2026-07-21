from __future__ import annotations

import logging
import json
import random
import time
from collections import Counter

from database.adaptive_weight_store import get_active_adaptive_weights
from database.analysis_store import get_analysis_history, get_latest_analysis_history
from database.collector_store import (
    get_kuaishou_history,
    get_latest_draw_history,
    get_latest_kuaishou_snapshot,
)
from database.data_quality_store import get_data_quality_status
from database.prediction_history_store import get_latest_prediction_history
from database.recommendation_center_store import save_recommendation_run
from database.simulation_store import get_latest_simulation_run, get_simulation_run_by_issue
from database.strategy_ranking_store import get_latest_strategy_rankings
from db import get_latest_draw
from services.simulation_model import ensure_simulation_for_issue, get_production_latest_issue
from services.voting_engine import build_voting_result

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

RECOMMENDATION_NUMBER_COUNT = 20


def _safe_float(value, default: float = 0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _recommendation_numbers(values) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in result:
            result.append(number)
    return result


def _trace_step(
    trace: list[dict],
    *,
    model_name: str,
    stage: str,
    input_count: int,
    output_count: int,
    reason: str,
) -> None:
    trace.append(
        {
            "model_name": model_name,
            "stage": stage,
            "input_count": max(0, int(input_count or 0)),
            "output_count": max(0, int(output_count or 0)),
            "removed_count": max(0, int(input_count or 0) - int(output_count or 0)),
            "reason": reason,
        }
    )


def _candidate_sources(voting: dict, candidates: list[dict]) -> list[tuple[str, list[int]]]:
    sources: list[tuple[str, list[int]]] = [
        ("Voting Engine", voting.get("final_candidates") or []),
    ]
    for model_name, payload in (voting.get("model_scores") or {}).items():
        if isinstance(payload, dict):
            sources.append((f"Model:{model_name}", payload.get("candidate_numbers") or []))
    for index, item in enumerate(candidates or [], start=1):
        sources.append((f"Simulation Group {index}", item.get("numbers") or []))
    return sources


def _build_20_number_output(
    seed_numbers,
    sources: list[tuple[str, list[int]]],
    trace: list[dict] | None = None,
) -> list[int]:
    trace = trace if trace is not None else []
    merged: list[int] = []
    raw_input_count = 0

    def extend_unique(values) -> None:
        for number in _recommendation_numbers(values):
            if number not in merged:
                merged.append(number)

    seed = _recommendation_numbers(seed_numbers)
    raw_input_count += len(seed_numbers or [])
    extend_unique(seed)
    _trace_step(
        trace,
        model_name="Recommendation Center",
        stage="Seed Normalize",
        input_count=len(seed_numbers or []),
        output_count=len(seed),
        reason="integer_1_80_unique",
    )

    for source_name, values in sources:
        normalized = _recommendation_numbers(values)
        before = len(merged)
        extend_unique(normalized)
        raw_input_count += len(values or [])
        _trace_step(
            trace,
            model_name=source_name,
            stage="Source Normalize",
            input_count=len(values or []),
            output_count=len(normalized),
            reason="integer_1_80_unique",
        )
        _trace_step(
            trace,
            model_name=source_name,
            stage="Merge",
            input_count=before + len(normalized),
            output_count=len(merged),
            reason="preserve_rank_unique_merge",
        )

    final_numbers = sorted(merged[:RECOMMENDATION_NUMBER_COUNT])
    _trace_step(
        trace,
        model_name="Recommendation Center",
        stage="Final Recommendation",
        input_count=len(merged),
        output_count=len(final_numbers),
        reason=(
            "top_20_unique_sorted"
            if len(final_numbers) == RECOMMENDATION_NUMBER_COUNT
            else "insufficient_unique_candidates"
        ),
    )
    _trace_step(
        trace,
        model_name="Recommendation Center",
        stage="Raw Input Summary",
        input_count=raw_input_count,
        output_count=len(final_numbers),
        reason="audit_total_raw_candidates_to_final",
    )
    return final_numbers


def _recommendation_output_status(numbers: list[int], trace: list[dict], voting: dict) -> dict:
    raw_summary = next(
        (step for step in reversed(trace) if step.get("stage") == "Raw Input Summary"),
        None,
    )
    unique_input = (
        int(raw_summary.get("input_count") or 0)
        if raw_summary
        else max((step.get("output_count") or 0 for step in trace), default=0)
    )
    model_count = len(voting.get("models") or [])
    removed_count = (
        int(raw_summary.get("removed_count") or 0)
        if raw_summary
        else max(0, unique_input - len(numbers))
    )
    is_valid = len(numbers) == RECOMMENDATION_NUMBER_COUNT
    return {
        "required_count": RECOMMENDATION_NUMBER_COUNT,
        "input_count": unique_input,
        "output_count": len(numbers),
        "model_count": model_count,
        "removed_count": removed_count,
        "is_valid": is_valid,
        "reason": "generated_20_numbers" if is_valid else "recommendation_insufficient",
    }


def _append_unique(result: list[int], values, *, exclude: set[int] | None = None) -> None:
    excluded = exclude or set()
    for number in _recommendation_numbers(values):
        if number not in excluded and number not in result:
            result.append(number)


def _flatten_number_groups(values) -> list[int]:
    flattened: list[int] = []
    for item in values or []:
        if isinstance(item, (list, tuple, set)):
            flattened.extend(item)
        else:
            flattened.append(item)
    return _recommendation_numbers(flattened)


def _number_zone(number: int) -> int:
    return min(3, max(0, (int(number) - 1) // 20))


def _number_tail(number: int) -> int:
    return int(number) % 10


def _issue_seed(issue: str | None, target_issue: str | None) -> int:
    text = f"{issue or ''}:{target_issue or ''}"
    return sum(ord(char) for char in text)


def _previous_fast_path_numbers(context: dict) -> list[int]:
    previous = context.get("previous_recommend_numbers")
    if previous is not None:
        return _recommendation_numbers(previous)
    try:
        latest = get_latest_prediction_history() or {}
        return _recommendation_numbers(latest.get("recommend_numbers"))
    except Exception:
        logger.exception("fast path previous prediction lookup failed")
        return []


def _build_fast_path_numbers(
    analysis: dict,
    *,
    source_issue: str,
    target_issue: str | None,
    previous_numbers: list[int],
    trace: list[dict],
) -> tuple[list[int], dict]:
    source_weights = {
        "patch_numbers": 9.0,
        "missing_numbers": 8.0,
        "cold_numbers": 7.0,
        "hot_numbers": 6.0,
        "diagonal_pattern": 5.0,
        "repeated_numbers": 3.5,
        "latest_draw_numbers": 1.0,
    }
    source_values = {
        "patch_numbers": _recommendation_numbers(analysis.get("patch_numbers")),
        "missing_numbers": _recommendation_numbers(analysis.get("missing_numbers")),
        "cold_numbers": _recommendation_numbers(analysis.get("cold_numbers")),
        "hot_numbers": _recommendation_numbers(analysis.get("hot_numbers")),
        "diagonal_pattern": _flatten_number_groups(analysis.get("diagonal_pattern")),
        "repeated_numbers": _recommendation_numbers(analysis.get("repeated_numbers")),
        "latest_draw_numbers": _recommendation_numbers(analysis.get("numbers")),
    }
    previous_set = set(_recommendation_numbers(previous_numbers))
    latest_set = set(source_values["latest_draw_numbers"])
    seed = _issue_seed(source_issue, target_issue)
    scores: dict[int, float] = {}
    reasons: dict[int, list[str]] = {}

    for number in range(1, 81):
        zone = _number_zone(number)
        tail = _number_tail(number)
        rotation = ((number * 17 + seed * 7) % 80) / 80
        middle_bias = 0.25 if zone in (1, 2) else 0
        score = 1.0 + rotation + middle_bias
        reason = ["full_pool_fill"]
        if number in latest_set:
            score -= 1.4
            reason.append("latest_draw_downweight")
        if number in previous_set:
            score -= 4.0
            reason.append("previous_prediction_downweight")
        for source_name, values in source_values.items():
            if number in values:
                score += source_weights[source_name]
                reason.append(source_name)
        scores[number] = score
        reasons[number] = reason

    ranked = sorted(range(1, 81), key=lambda number: (-scores[number], _number_zone(number), _number_tail(number), number))
    selected: list[int] = []
    zone_counts = {zone: 0 for zone in range(4)}
    tail_counts = {tail: 0 for tail in range(10)}
    previous_count = 0
    zone_quota = {zone: 5 for zone in range(4)}
    tail_limit = 3
    previous_limit = 10

    def can_select(number: int, *, relaxed: bool = False) -> bool:
        if number in selected:
            return False
        zone = _number_zone(number)
        tail = _number_tail(number)
        if not relaxed and zone_counts[zone] >= zone_quota[zone]:
            return False
        if not relaxed and tail_counts[tail] >= tail_limit:
            return False
        if not relaxed and number in previous_set and previous_count >= previous_limit:
            return False
        return True

    def add_number(number: int) -> None:
        nonlocal previous_count
        selected.append(number)
        zone_counts[_number_zone(number)] += 1
        tail_counts[_number_tail(number)] += 1
        if number in previous_set:
            previous_count += 1

    for zone in range(4):
        for number in [candidate for candidate in ranked if _number_zone(candidate) == zone]:
            if zone_counts[zone] >= zone_quota[zone]:
                break
            if can_select(number):
                add_number(number)

    for number in ranked:
        if len(selected) >= RECOMMENDATION_NUMBER_COUNT:
            break
        if can_select(number):
            add_number(number)

    for number in ranked:
        if len(selected) >= RECOMMENDATION_NUMBER_COUNT:
            break
        if can_select(number, relaxed=True):
            add_number(number)

    selected = sorted(selected[:RECOMMENDATION_NUMBER_COUNT])
    top_sources = sum(len(values) for values in source_values.values())
    _trace_step(
        trace,
        model_name="Production Fast Path",
        stage="Source Scoring",
        input_count=top_sources + 80,
        output_count=len(ranked),
        reason="analysis_weighted_full_pool_scoring",
    )
    _trace_step(
        trace,
        model_name="Production Fast Path",
        stage="Previous Recommendation Penalty",
        input_count=len(previous_set),
        output_count=previous_count,
        reason="previous_recommendation_downweighted_overlap_limited",
    )
    _trace_step(
        trace,
        model_name="Production Fast Path",
        stage="Zone Tail Balance",
        input_count=len(ranked),
        output_count=len(selected),
        reason="four_zone_quota_tail_cap_hot_cold_missing_balance",
    )
    diversity = {
        "zone_counts": {str(zone): zone_counts[zone] for zone in range(4)},
        "tail_count": len({number % 10 for number in selected}),
        "previous_overlap_count": len(set(selected) & previous_set),
        "previous_overlap_limit": previous_limit,
        "top_ranked": ranked[:30],
        "selected_reasons": {str(number): reasons[number] for number in selected},
    }
    return selected, diversity


def calculate_fast_recommendation(
    issue: str | None,
    target_issue: str | None,
    context: dict | None = None,
) -> dict:
    started = time.perf_counter()
    trace: list[dict] = []
    try:
        context = context or {}
        source_issue = str(issue or "").strip()
        if not source_issue:
            return {
                "status": "skipped",
                "message": "missing source issue",
                "recommendation": None,
                "timings_ms": {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
            }

        mark = time.perf_counter()
        analysis = get_latest_analysis_history() or {}
        analysis_issue = str(analysis.get("issue") or "")
        timings = {"analysis_ms": round((time.perf_counter() - mark) * 1000, 2)}
        if analysis_issue != source_issue:
            return {
                "status": "skipped",
                "message": "latest analysis is not for source issue",
                "reason": "analysis_issue_mismatch",
                "source_issue": source_issue,
                "analysis_issue": analysis_issue,
                "recommendation": None,
                "timings_ms": {**timings, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
            }

        previous_numbers = _previous_fast_path_numbers(context)
        numbers, diversity = _build_fast_path_numbers(
            analysis,
            source_issue=source_issue,
            target_issue=target_issue,
            previous_numbers=previous_numbers,
            trace=trace,
        )
        _trace_step(
            trace,
            model_name="Production Fast Path",
            stage="Analysis Merge",
            input_count=80,
            output_count=len(numbers),
            reason="analysis_diversified_lightweight_merge",
        )
        output = _recommendation_output_status(numbers, trace, {"models": [{"model_name": "Production Fast Path"}]})
        timings["result_build_ms"] = round((time.perf_counter() - mark) * 1000, 2)
        confidence = 62 if output.get("is_valid") else 0
        recommendation = {
            "issue": source_issue,
            "target_issue": target_issue,
            "best_strategy": "ProductionFastPath",
            "confidence": confidence,
            "data_quality_status": "ok",
            "super_recommendation": {"recommended": [{"number": analysis.get("super_number")}]} if analysis.get("super_number") else {"recommended": []},
            "sync": {"status": "ok", "simulation_issue": source_issue, "recommendation_issue": source_issue, "super_issue": source_issue},
            "model_scores": {
                "production_fast_path": {
                    "label": "Production Fast Path",
                    "confidence": confidence,
                    "candidate_numbers": numbers,
                    "reason": "Built from latest analysis with zone, tail, source balance, and previous overlap control.",
                    "diversity": diversity,
                }
            },
            "winning_model": "production_fast_path",
            "model_voting": {
                "status": "skipped",
                "reason": "production_fast_path_does_not_run_v7_voting",
                "final_candidates": numbers,
                "confidence": confidence,
                "model_scores": {},
            },
            "recommendation_trace": trace,
            "recommendation_output": output,
            "diversity": diversity,
            "explanation": "Production fast path built from latest analysis with zone, tail, source balance, and previous overlap control.",
            "context": {**context, "fast_path": True},
            "timings_ms": {**timings, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
            "results": [
                {
                    "rank": 1,
                    "numbers": numbers,
                    "confidence": confidence,
                    "total_score": confidence,
                    "strategy": "ProductionFastPath",
                    "explanation": "Latest analysis lightweight production fast path.",
                    "model_scores": {},
                    "winning_model": "production_fast_path",
                }
            ],
        }
        return {
            "status": "ok" if output.get("is_valid") else "skipped",
            "recommendation": recommendation if output.get("is_valid") else None,
            "persisted": False,
            "timings_ms": recommendation["timings_ms"],
            "reason": None if output.get("is_valid") else "recommendation_insufficient",
        }
    except Exception as exc:
        logger.exception("fast recommendation calculation failed")
        return {
            "status": "error",
            "message": str(exc),
            "recommendation": None,
            "persisted": False,
            "timings_ms": {"total_ms": round((time.perf_counter() - started) * 1000, 2)},
        }


def _record_recommendation_event(event_type: str, status: str, issue: str | None, output: dict) -> None:
    try:
        from services.operations_center import record_operation_event

        record_operation_event(
            component="recommendation",
            event_type=event_type,
            status=status,
            issue=issue,
            message=json.dumps(
                {
                    "event_type": event_type,
                    "issue": issue,
                    "input_count": output.get("input_count"),
                    "output_count": output.get("output_count"),
                    "model_count": output.get("model_count"),
                    "removed_count": output.get("removed_count"),
                    "reason": output.get("reason"),
                },
                ensure_ascii=False,
            ),
            error_type=None if status == "ok" else output.get("reason"),
        )
    except Exception:
        logger.exception("recommendation event recording failed")


def _normalize_score(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0
    return max(0, min(1, value / maximum))


def _issue_sort_key(issue: str) -> tuple[int, str]:
    try:
        return (0, f"{int(issue):020d}")
    except Exception:
        return (1, issue)


def _is_test_issue(issue: str | None, source: str | None = None) -> bool:
    if issue is None:
        return True

    issue_text = str(issue).strip().upper()
    source_text = str(source or "").strip().lower()

    if not issue_text:
        return True
    if issue_text.startswith("99") or issue_text.startswith("TEST"):
        return True
    if "test" in source_text or "phase" in source_text:
        return True

    try:
        if int(issue_text) >= 900000000 and source_text != "kuaishou":
            return True
    except Exception:
        pass

    return False


def _production_issue(item: dict | None, default_source: str | None = None) -> str | None:
    if not item or item.get("issue") is None:
        return None
    issue = str(item.get("issue"))
    source = item.get("source") or default_source
    return None if _is_test_issue(issue, source) else issue


def _latest_issue() -> str | None:
    candidates = []

    production_issue = get_production_latest_issue()
    if production_issue:
        return production_issue

    try:
        kuaishou = get_latest_kuaishou_snapshot()
        issue = _production_issue(kuaishou, "kuaishou")
        if issue:
            return issue
    except Exception:
        logger.exception("failed to load kuaishou issue for recommendation center")

    for loader in [get_latest_analysis_history, get_latest_draw_history]:
        try:
            item = loader()
            issue = _production_issue(item)
            if issue:
                candidates.append(issue)
        except Exception:
            logger.exception("failed to load latest collector issue for recommendation center")

    try:
        latest = get_latest_draw()
        issue = _production_issue(latest)
        if issue:
            candidates.append(issue)
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
        if issue is None:
            return None
        current = int(issue)
        recent = [
            int(item.get("issue"))
            for item in get_kuaishou_history(5)
            if str(item.get("issue") or "").isdigit()
        ]
        if len(recent) >= 2 and recent[0] == current and recent[0] - recent[1] == 1:
            return str(current + 1)
        logger.warning("target issue cannot be confirmed from collector metadata: %s", issue)
        return None
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


def calculate_recommendation(
    issue: str | None,
    target_issue: str | None,
    context: dict | None = None,
) -> dict:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    mark = started
    try:
        context = context or {}
        issue = str(issue) if issue is not None else _latest_issue()
        simulation = get_simulation_run_by_issue(issue) if issue else None
        if issue and not simulation and context.get("ensure_simulation", True):
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
                "timings_ms": {**timings, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
            }
        timings["simulation_ms"] = round((time.perf_counter() - mark) * 1000, 2)

        mark = time.perf_counter()
        rankings = get_latest_strategy_rankings()
        best = _best_strategy(rankings)
        adaptive = get_active_adaptive_weights()
        quality = get_data_quality_status()
        timings["metadata_ms"] = round((time.perf_counter() - mark) * 1000, 2)
        quality_status = quality.get("status", "unknown")
        issue = simulation.get("source_issue") or issue
        target_issue = target_issue

        candidates = simulation.get("results", [])[:5]
        mark = time.perf_counter()
        voting = build_voting_result(100)
        timings["voting_ms"] = round((time.perf_counter() - mark) * 1000, 2)
        voting_candidates = voting.get("final_candidates") or []
        mark = time.perf_counter()
        recommendation_trace = list(voting.get("trace") or [])
        number_sources = _candidate_sources(voting, candidates)
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
        timings["source_merge_ms"] = round((time.perf_counter() - mark) * 1000, 2)

        mark = time.perf_counter()
        results = []
        confidences = []
        if voting_candidates:
            candidates = [
                {
                    "numbers": voting_candidates,
                    "total_score": voting.get("confidence", 0),
                    "scores": {
                        "model_scores": voting.get("model_scores", {}),
                        "winning_model": voting.get("winning_model"),
                    },
                }
            ] + candidates[:4]
            max_total_score = max(_safe_float(item.get("total_score")) for item in candidates) or 1

        for index, item in enumerate(candidates, start=1):
            item_trace = recommendation_trace if index == 1 else []
            item_numbers = _build_20_number_output(
                item.get("numbers", []),
                number_sources,
                item_trace,
            )
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
                    "numbers": item_numbers,
                    "confidence": confidence,
                    "total_score": total_score,
                    "strategy": best_strategy,
                    "explanation": explanation,
                    "model_scores": voting.get("model_scores", {}),
                    "winning_model": voting.get("winning_model"),
                }
            )

        first_numbers = results[0].get("numbers") if results else []
        recommendation_output = _recommendation_output_status(first_numbers, recommendation_trace, voting)
        timings["result_build_ms"] = round((time.perf_counter() - mark) * 1000, 2)
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
            "model_scores": voting.get("model_scores", {}),
            "winning_model": voting.get("winning_model"),
            "model_voting": voting,
            "recommendation_trace": recommendation_trace,
            "recommendation_output": recommendation_output,
            "explanation": run_explanation,
            "context": context,
            "timings_ms": {**timings, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
        }
        return {
            "status": "ok",
            "recommendation": {
                **run,
                "results": results,
            },
            "persisted": False,
            "timings_ms": run["timings_ms"],
        }
    except Exception as exc:
        logger.exception("recommendation calculation failed")
        return {
            "status": "error",
            "message": str(exc),
            "recommendation": None,
            "persisted": False,
            "timings_ms": {**timings, "total_ms": round((time.perf_counter() - started) * 1000, 2)},
        }


def generate_recommendation_center(
    issue_override: str | None = None,
    target_issue_override: str | None = None,
    *,
    persist: bool = False,
    calculate_only: bool = False,
    context: dict | None = None,
) -> dict:
    try:
        issue = str(issue_override) if issue_override is not None else _latest_issue()
        target_issue = target_issue_override or _target_issue(issue)
        calculated = calculate_recommendation(
            issue,
            target_issue,
            context={**(context or {}), "ensure_simulation": True},
        )
        if calculated.get("status") != "ok":
            return calculated
        recommendation = calculated.get("recommendation") or {}
        results = recommendation.get("results") or []
        if calculate_only or not persist:
            return {
                **calculated,
                "persisted": False,
                "saved": {"status": "skipped", "reason": "preview_only"},
                "prediction_history": {"status": "skipped", "reason": "single_entry_prediction_service_required"},
            }
        output = recommendation.get("recommendation_output") or {}
        if not output.get("is_valid"):
            _record_recommendation_event("recommendation_insufficient", "warning", issue, output)
            return {
                **calculated,
                "status": "skipped",
                "persisted": False,
                "saved": {"status": "skipped", "reason": "recommendation_insufficient"},
                "prediction_history": {"status": "skipped", "reason": "single_entry_prediction_service_required"},
            }
        _record_recommendation_event("recommendation_generated", "ok", issue, output)
        run = {key: value for key, value in recommendation.items() if key != "results"}
        saved = save_recommendation_run(run, results)
        run["prediction_history"] = {"status": "skipped", "reason": "single_entry_prediction_service_required"}

        try:
            from services.learning_engine import save_live_prediction_snapshot

            run["learning_snapshot"] = save_live_prediction_snapshot({**run, "results": results})
        except Exception as exc:
            logger.exception("learning live snapshot save failed")
            run["learning_snapshot"] = {"status": "error", "message": str(exc)}

        try:
            from services.prediction_tracker import register_recommendation_prediction

            run["prediction_tracker"] = register_recommendation_prediction(
                {**run, "results": results},
                saved.get("run_id"),
                recommendation.get("simulation_run_id"),
            )
        except Exception as exc:
            logger.exception("prediction tracker registration failed")
            run["prediction_tracker"] = {"status": "error", "message": str(exc)}

        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "recommendation": {
                **run,
                "results": results,
            },
            "saved": saved,
            "persisted": saved.get("status") == "ok",
        }
    except Exception as exc:
        logger.exception("recommendation center generation failed")
        return {"status": "error", "message": str(exc), "recommendation": None, "persisted": False}
