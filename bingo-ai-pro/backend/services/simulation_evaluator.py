from __future__ import annotations

import logging

from database.analysis_store import get_analysis_history
from database.collector_store import get_kuaishou_history
from database.simulation_evaluation_store import save_simulation_evaluation
from database.simulation_store import get_latest_simulation_run
from services.simulation_model import _build_features, _generate_candidates

logger = logging.getLogger(__name__)


def _as_int_list(values) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80:
            numbers.append(number)
    return numbers


def _numbers_from_snapshot(snapshot: dict) -> list[int]:
    parsed = snapshot.get("parsed_json") or {}
    api_data = parsed.get("api_get_data") if isinstance(parsed, dict) else None
    latest = (api_data.get("data") or [{}])[0] if isinstance(api_data, dict) else {}
    return _as_int_list(latest.get("\u4e00\u822c\u734e\u865f") or snapshot.get("numbers"))


def _load_history(window: int) -> list[dict]:
    history = []
    try:
        for item in get_analysis_history(window):
            numbers = _as_int_list(item.get("numbers"))
            if len(numbers) == 20:
                history.append({"issue": item.get("issue"), "numbers": numbers})
    except Exception:
        logger.exception("failed to load analysis history for simulation evaluation")

    if history:
        return history[:window]

    try:
        for item in get_kuaishou_history(window):
            numbers = _numbers_from_snapshot(item)
            if len(numbers) == 20:
                history.append({"issue": item.get("issue"), "numbers": numbers})
    except Exception:
        logger.exception("failed to load kuaishou history for simulation evaluation")

    return history[:window]


def _issue_sort_key(draw: dict) -> tuple[int, str]:
    issue = draw.get("issue")
    try:
        return (0, f"{int(issue):020d}")
    except Exception:
        return (1, str(issue or ""))


def _bucket(hit_count: int) -> str:
    if hit_count >= 5:
        return "hit_5_plus"
    return f"hit_{hit_count}"


def evaluate_latest_simulation(window: int = 100) -> dict:
    try:
        window = max(1, min(int(window), 1000))
        run = get_latest_simulation_run() or {}
        groups = max(1, min(int(run.get("groups") or 5), 50))
        numbers_per_group = max(1, min(int(run.get("numbers_per_group") or 10), 20))

        history_limit = min(max(window * 2, window + 100), 1000)
        history = sorted(_load_history(history_limit), key=_issue_sort_key)
        if len(history) < 2:
            return {
                "status": "error",
                "message": "not enough historical data available",
                "evaluation": None,
            }

        buckets = {
            "hit_0": 0,
            "hit_1": 0,
            "hit_2": 0,
            "hit_3": 0,
            "hit_4": 0,
            "hit_5_plus": 0,
        }
        hit_values = []

        for index in range(1, len(history)):
            draw = history[index]
            training_draws = history[max(0, index - window):index]
            if not training_draws:
                continue

            features = _build_features(list(reversed(training_draws)))
            candidates = [
                set(_as_int_list(item.get("numbers")))
                for item in _generate_candidates(features, groups, numbers_per_group)
                if item.get("numbers")
            ]
            actual = set(_as_int_list(draw["numbers"]))
            best_hit = max((len(actual & candidate) for candidate in candidates), default=0)
            hit_values.append(best_hit)
            buckets[_bucket(best_hit)] += 1

        evaluated_issues = len(hit_values)
        average_hits = round(sum(hit_values) / evaluated_issues, 4) if evaluated_issues else 0
        best_hits = max(hit_values) if hit_values else 0
        hit_rate = round(
            (buckets["hit_3"] + buckets["hit_4"] + buckets["hit_5_plus"]) / evaluated_issues,
            4,
        ) if evaluated_issues else 0

        evaluation = {
            "run_id": run.get("id"),
            "strategy": "walk_forward",
            "window": window,
            "evaluated_issues": evaluated_issues,
            **buckets,
            "average_hits": average_hits,
            "best_hits": best_hits,
            "hit_rate": hit_rate,
            "hit_distribution": buckets.copy(),
            "leakage_safe": True,
        }
        saved = save_simulation_evaluation(evaluation)

        return {
            "status": "ok",
            "evaluation": evaluation,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("simulation evaluation failed")
        return {"status": "error", "message": str(exc), "evaluation": None}
