from __future__ import annotations

import logging
import random
from collections import Counter

from database.analysis_store import get_analysis_history
from database.collector_store import get_kuaishou_history
from database.simulation_store import save_simulation_run

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


def _score_candidate(numbers: list[int], features: dict, rng: random.Random) -> dict:
    number_set = set(numbers)
    hot = set(features.get("hot_numbers", []))
    cold = set(features.get("cold_numbers", []))
    repeats = set(features.get("recent_repeat_numbers", []))
    missing = set(features.get("missing_numbers", []))

    hot_cold_score = len(number_set & hot) * 8 + len(number_set & cold) * 3 + len(number_set & missing) * 2
    consecutive_score = len(_find_consecutive(numbers)) * 4
    repeat_score = len(number_set & repeats) * 7
    big_count = len([number for number in numbers if number >= 41])
    odd_count = len([number for number in numbers if number % 2 == 1])
    balance_score = max(0, 20 - abs(big_count - (len(numbers) / 2)) * 4 - abs(odd_count - (len(numbers) / 2)) * 3)
    tail_score = len(set(number % 10 for number in numbers)) * 1.5
    random_score = rng.uniform(0, 6)
    laowanjia_score = repeat_score + consecutive_score + len(number_set & hot) * 3

    total_score = hot_cold_score + laowanjia_score + balance_score + tail_score + random_score

    return {
        "hot_cold_score": round(hot_cold_score, 2),
        "laowanjia_score": round(laowanjia_score, 2),
        "balance_score": round(balance_score, 2),
        "tail_score": round(tail_score, 2),
        "random_score": round(random_score, 2),
        "total_score": round(total_score, 2),
    }


def _generate_candidates(features: dict, groups: int, numbers_per_group: int) -> list[dict]:
    rng = random.Random()
    pool = _candidate_pool(features)
    attempts = max(groups * 12, 40)
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
        scores = _score_candidate(numbers, features, rng)
        candidates.append(
            {
                "numbers": numbers,
                "scores": scores,
                "total_score": scores["total_score"],
            }
        )

    candidates.sort(key=lambda item: item["total_score"], reverse=True)
    top = candidates[:groups]
    for index, item in enumerate(top, start=1):
        item["rank"] = index
    return top


def run_simulation(window: int = 100, groups: int = 5, numbers_per_group: int = 10) -> dict:
    try:
        window = max(1, min(int(window), 1000))
        groups = max(1, min(int(groups), 50))
        numbers_per_group = max(1, min(int(numbers_per_group), 20))

        draws = _load_recent_draws(window)
        if not draws:
            return {
                "status": "error",
                "message": "no historical data available",
                "results": [],
            }

        features = _build_features(draws)
        results = _generate_candidates(features, groups, numbers_per_group)
        payload = {
            "window": window,
            "groups": groups,
            "numbers_per_group": numbers_per_group,
            "features": features,
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
