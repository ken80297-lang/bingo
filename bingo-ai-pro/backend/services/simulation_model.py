from __future__ import annotations

import logging
import random
from collections import Counter

from database.analysis_store import get_analysis_history
from database.adaptive_weight_store import get_active_adaptive_weights
from database.collector_store import get_kuaishou_history
from database.simulation_store import get_simulation_run_by_issue, save_simulation_run

logger = logging.getLogger(__name__)
MODEL_VERSION = "v1"
EXPLORATION_RATE = 0.05

DEFAULT_SCORE_WEIGHTS = {
    "laowanjia_weight": 0.30,
    "hot_cold_weight": 0.35,
    "balance_weight": 0.20,
    "tail_weight": 0.10,
    "random_weight": 0.05,
}


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


def _load_recent_draws(window: int) -> list[dict]:
    draws = []

    try:
        for item in get_analysis_history(window):
            numbers = _as_int_list(item.get("numbers"))
            if len(numbers) == 20:
                draws.append({"issue": item.get("issue"), "numbers": numbers})
    except Exception:
        logger.exception("failed to load analysis history for simulation")

    if draws:
        return draws[:window]

    try:
        for item in get_kuaishou_history(window):
            numbers = _numbers_from_snapshot(item)
            if len(numbers) == 20:
                draws.append({"issue": item.get("issue"), "numbers": numbers})
    except Exception:
        logger.exception("failed to load kuaishou snapshots for simulation")

    return draws[:window]


def _find_consecutive(numbers: list[int]) -> list[list[int]]:
    sorted_numbers = sorted(numbers)
    return [
        [number, number + 1]
        for number in sorted_numbers
        if number + 1 in sorted_numbers
    ]


def _build_features(draws: list[dict]) -> dict:
    all_numbers = []
    for draw in draws:
        all_numbers.extend(draw["numbers"])

    counter = Counter(all_numbers)
    hot_numbers = [number for number, _ in counter.most_common(15)]
    cold_numbers = [number for number, _ in counter.most_common()[-15:]]
    appeared = set(all_numbers)
    missing_numbers = [number for number in range(1, 81) if number not in appeared]

    recent_repeat = []
    if len(draws) >= 2:
        recent_repeat = sorted(set(draws[0]["numbers"]) & set(draws[1]["numbers"]))

    latest_numbers = draws[0]["numbers"] if draws else []
    big_count = len([number for number in latest_numbers if number >= 41])
    small_count = len([number for number in latest_numbers if number <= 40])
    odd_count = len([number for number in latest_numbers if number % 2 == 1])
    even_count = len([number for number in latest_numbers if number % 2 == 0])

    tail_distribution = Counter(number % 10 for number in all_numbers)

    return {
        "sample_size": len(draws),
        "hot_numbers": hot_numbers,
        "cold_numbers": cold_numbers,
        "recent_repeat_numbers": recent_repeat,
        "missing_numbers": missing_numbers[:30],
        "big_small_ratio": {"big": big_count, "small": small_count},
        "odd_even_ratio": {"odd": odd_count, "even": even_count},
        "consecutive_numbers": _find_consecutive(latest_numbers),
        "tail_distribution": {
            str(tail): count
            for tail, count in sorted(tail_distribution.items())
        },
    }


def _candidate_pool(features: dict) -> list[int]:
    pool = []
    for key in ["hot_numbers", "recent_repeat_numbers", "missing_numbers", "cold_numbers"]:
        for number in features.get(key, []):
            if number not in pool:
                pool.append(number)
    for number in range(1, 81):
        if number not in pool:
            pool.append(number)
    return pool


def _load_score_weights() -> dict:
    try:
        active = get_active_adaptive_weights()
        if not active:
            return {"source": "default", **DEFAULT_SCORE_WEIGHTS}
        return {
            "source": "adaptive",
            "adaptive_weight_id": active.get("id"),
            "adaptive_weight_version": active.get("version"),
            "laowanjia_weight": float(active.get("laowanjia_weight") or DEFAULT_SCORE_WEIGHTS["laowanjia_weight"]),
            "hot_cold_weight": float(active.get("hot_cold_weight") or DEFAULT_SCORE_WEIGHTS["hot_cold_weight"]),
            "balance_weight": float(active.get("balance_weight") or DEFAULT_SCORE_WEIGHTS["balance_weight"]),
            "tail_weight": float(active.get("tail_weight") or DEFAULT_SCORE_WEIGHTS["tail_weight"]),
            "random_weight": float(active.get("random_weight") or DEFAULT_SCORE_WEIGHTS["random_weight"]),
        }
    except Exception:
        logger.exception("failed to load adaptive weights for simulation")
        return {"source": "default", **DEFAULT_SCORE_WEIGHTS}


def _score_candidate(numbers: list[int], features: dict, rng: random.Random, weights: dict | None = None) -> dict:
    number_set = set(numbers)
    hot = set(features.get("hot_numbers", []))
    cold = set(features.get("cold_numbers", []))
    repeats = set(features.get("recent_repeat_numbers", []))
    missing = set(features.get("missing_numbers", []))
    weights = weights or {"source": "default", **DEFAULT_SCORE_WEIGHTS}

    hot_cold_score = len(number_set & hot) * 8 + len(number_set & cold) * 3 + len(number_set & missing) * 2
    consecutive_score = len(_find_consecutive(numbers)) * 4
    repeat_score = len(number_set & repeats) * 7
    big_count = len([number for number in numbers if number >= 41])
    odd_count = len([number for number in numbers if number % 2 == 1])
    balance_score = max(0, 20 - abs(big_count - (len(numbers) / 2)) * 4 - abs(odd_count - (len(numbers) / 2)) * 3)
    tail_score = len(set(number % 10 for number in numbers)) * 1.5
    random_score = rng.uniform(0, 6)
    laowanjia_score = repeat_score + consecutive_score + len(number_set & hot) * 3

    total_score = (
        hot_cold_score * weights.get("hot_cold_weight", DEFAULT_SCORE_WEIGHTS["hot_cold_weight"])
        + laowanjia_score * weights.get("laowanjia_weight", DEFAULT_SCORE_WEIGHTS["laowanjia_weight"])
        + balance_score * weights.get("balance_weight", DEFAULT_SCORE_WEIGHTS["balance_weight"])
        + tail_score * weights.get("tail_weight", DEFAULT_SCORE_WEIGHTS["tail_weight"])
        + random_score * weights.get("random_weight", DEFAULT_SCORE_WEIGHTS["random_weight"])
    )

    return {
        "hot_cold_score": round(hot_cold_score, 2),
        "laowanjia_score": round(laowanjia_score, 2),
        "balance_score": round(balance_score, 2),
        "tail_score": round(tail_score, 2),
        "random_score": round(random_score, 2),
        "weights": weights,
        "total_score": round(total_score, 2),
    }


def _explore_candidate(numbers: list[int], pool: list[int], rng: random.Random) -> list[int]:
    explored = list(numbers)
    swap_count = rng.randint(1, 2)
    for _ in range(swap_count):
        if not explored:
            break
        remove_at = rng.randrange(len(explored))
        available = [number for number in pool if number not in explored]
        if not available:
            break
        explored[remove_at] = rng.choice(available)
        explored = sorted(set(explored))
        while len(explored) < len(numbers):
            candidate = rng.choice(pool)
            if candidate not in explored:
                explored.append(candidate)
        explored = sorted(explored[:len(numbers)])
    return explored


def _generate_candidates(
    features: dict,
    groups: int,
    numbers_per_group: int,
    weights: dict | None = None,
    seed: str | None = None,
) -> list[dict]:
    rng = random.Random(str(seed)) if seed is not None else random.Random()
    weights = weights or {"source": "default", **DEFAULT_SCORE_WEIGHTS}
    pool = _candidate_pool(features)
    attempts = max(groups * 20, 80)
    candidates = []
    seen = set()

    weighted_pool = pool[:25] + pool[:15] + pool

    for _ in range(attempts):
        selected = sorted(set(rng.sample(weighted_pool, min(len(weighted_pool), numbers_per_group * 2))))
        while len(selected) < numbers_per_group:
            candidate = rng.choice(pool)
            if candidate not in selected:
                selected.append(candidate)
        numbers = sorted(selected[:numbers_per_group])
        key = tuple(numbers)
        if key in seen:
            continue
        seen.add(key)
        scores = _score_candidate(numbers, features, rng, weights)
        candidates.append(
            {
                "numbers": numbers,
                "scores": scores,
                "total_score": scores["total_score"],
            }
        )

    candidates.sort(key=lambda item: item["total_score"], reverse=True)
    top_pool = candidates[:20]
    top = []
    seen = set()
    for item in top_pool:
        numbers = item["numbers"]
        if seed is not None and rng.random() < EXPLORATION_RATE:
            numbers = _explore_candidate(numbers, pool, rng)
            scores = _score_candidate(numbers, features, rng, weights)
            item = {
                "numbers": numbers,
                "scores": {**scores, "exploration": True, "exploration_rate": EXPLORATION_RATE},
                "total_score": scores["total_score"],
            }
        key = tuple(item["numbers"])
        if key in seen:
            continue
        seen.add(key)
        top.append(item)
        if len(top) == groups:
            break
    if len(top) < groups:
        top = candidates[:groups]
    for index, item in enumerate(top, start=1):
        item["rank"] = index
    return top


def run_simulation(
    window: int = 100,
    groups: int = 5,
    numbers_per_group: int = 10,
    source_issue: str | None = None,
    force: bool = False,
) -> dict:
    try:
        window = max(1, min(int(window), 1000))
        groups = max(1, min(int(groups), 50))
        numbers_per_group = max(1, min(int(numbers_per_group), 20))
        source_issue = str(source_issue) if source_issue is not None else None

        if source_issue and not force:
            existing = get_simulation_run_by_issue(source_issue)
            if existing:
                return {
                    "status": "ok",
                    "skipped": True,
                    "message": "simulation already exists for source_issue",
                    "run": existing,
                    "results": existing.get("results", []),
                }

        draws = _load_recent_draws(window)
        if not draws:
            return {
                "status": "error",
                "message": "no historical data available",
                "results": [],
            }

        features = _build_features(draws)
        weights = _load_score_weights()
        seed = source_issue or (draws[0].get("issue") if draws else None)
        results = _generate_candidates(features, groups, numbers_per_group, weights, seed=seed)
        payload = {
            "window": window,
            "groups": groups,
            "numbers_per_group": numbers_per_group,
            "source_issue": source_issue or seed,
            "sample_size": len(draws),
            "model_version": MODEL_VERSION,
            "features": {**features, "score_weights": weights},
            "status": "ok",
        }
        saved = save_simulation_run(payload, results)

        return {
            "status": "ok",
            "run": {
                **payload,
                "storage": saved.get("storage"),
                "run_id": saved.get("run_id"),
            },
            "results": results,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("simulation failed")
        return {"status": "error", "message": str(exc), "results": []}


def ensure_simulation_for_issue(issue: str, window: int = 100, groups: int = 5, numbers_per_group: int = 10) -> dict:
    try:
        return run_simulation(
            window=window,
            groups=groups,
            numbers_per_group=numbers_per_group,
            source_issue=issue,
            force=False,
        )
    except Exception as exc:
        logger.exception("ensure simulation for issue failed")
        return {"status": "error", "message": str(exc), "results": []}
