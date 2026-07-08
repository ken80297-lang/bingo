from __future__ import annotations

import logging
from collections import Counter

from database.analysis_store import get_analysis_history
from database.collector_store import get_kuaishou_history
from database.laowanjia_feature_store import save_laowanjia_feature

logger = logging.getLogger(__name__)


def _as_int_list(values) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


def _numbers_from_snapshot(snapshot: dict) -> list[int]:
    parsed = snapshot.get("parsed_json") or {}
    api_data = parsed.get("api_get_data") if isinstance(parsed, dict) else None
    latest = (api_data.get("data") or [{}])[0] if isinstance(api_data, dict) else {}
    return _as_int_list(latest.get("一般獎號") or snapshot.get("numbers"))


def _load_recent_draws(limit: int = 100) -> list[dict]:
    draws = []
    try:
        for item in get_analysis_history(limit):
            numbers = _as_int_list(item.get("numbers"))
            if numbers:
                draws.append(
                    {
                        "issue": item.get("issue"),
                        "numbers": numbers,
                        "super_number": item.get("super_number"),
                    }
                )
    except Exception:
        logger.exception("failed to load analysis history for laowanjia features")

    if draws:
        return draws[:limit]

    try:
        for item in get_kuaishou_history(limit):
            numbers = _numbers_from_snapshot(item)
            if numbers:
                draws.append(
                    {
                        "issue": item.get("issue"),
                        "numbers": numbers,
                        "super_number": None,
                    }
                )
    except Exception:
        logger.exception("failed to load kuaishou history for laowanjia features")

    return draws[:limit]


def _pairs(numbers: list[int], diff: int) -> list[list[int]]:
    number_set = set(numbers)
    return [[number, number + diff] for number in numbers if number + diff in number_set]


def _missing_numbers(draws: list[dict]) -> list[int]:
    appeared = set()
    for draw in draws:
        appeared.update(_as_int_list(draw.get("numbers")))
    return [number for number in range(1, 81) if number not in appeared]


def build_laowanjia_feature_record(draws: list[dict]) -> dict | None:
    if not draws:
        return None

    latest = draws[0]
    numbers = _as_int_list(latest.get("numbers"))
    if not numbers:
        return None

    previous_numbers = _as_int_list(draws[1].get("numbers")) if len(draws) > 1 else []
    previous_set = set(previous_numbers)
    number_set = set(numbers)
    recent_missing = _missing_numbers(draws[1:80] if len(draws) > 1 else draws)

    consecutive = _pairs(numbers, 1)
    twins = _pairs(numbers, 2)
    diagonal = _pairs(numbers, 9) + _pairs(numbers, 11)
    gap_candidates = {}
    for diff in [1, 2, 9, 10, 11]:
        matches = []
        for previous in previous_numbers:
            for candidate in [previous - diff, previous + diff]:
                if candidate in number_set:
                    matches.append(candidate)
        gap_candidates[str(diff)] = sorted(set(matches))

    repeat_numbers = sorted(number_set & previous_set)
    missing_hits = sorted(number_set & set(recent_missing))
    big_count = len([number for number in numbers if number >= 41])
    small_count = len(numbers) - big_count
    odd_count = len([number for number in numbers if number % 2 == 1])
    even_count = len(numbers) - odd_count

    consecutive_score = min(20, len(consecutive) * 4)
    twin_score = min(12, len(twins) * 3)
    diagonal_score = min(16, len(diagonal) * 4)
    gap_score = min(16, sum(len(values) for values in gap_candidates.values()) * 2)
    missing_score = min(12, len(missing_hits) * 3)
    big_small_score = max(0, 12 - abs(big_count - small_count) * 2)
    odd_even_score = max(0, 12 - abs(odd_count - even_count) * 2)
    repeat_score = min(20, len(repeat_numbers) * 4)
    total = round(
        consecutive_score
        + twin_score
        + diagonal_score
        + gap_score
        + missing_score
        + big_small_score
        + odd_even_score
        + repeat_score,
        2,
    )

    all_recent = []
    for draw in draws:
        all_recent.extend(_as_int_list(draw.get("numbers")))
    hot = [number for number, _ in Counter(all_recent).most_common(10)]

    return {
        "issue": str(latest.get("issue")) if latest.get("issue") is not None else None,
        "numbers": numbers,
        "super_number": latest.get("super_number"),
        "consecutive_score": consecutive_score,
        "twin_score": twin_score,
        "diagonal_score": diagonal_score,
        "gap_score": gap_score,
        "missing_score": missing_score,
        "big_small_score": big_small_score,
        "odd_even_score": odd_even_score,
        "repeat_score": repeat_score,
        "total_laowanjia_feature_score": total,
        "feature_json": {
            "consecutive_pairs": consecutive,
            "twin_pairs": twins,
            "diagonal_pairs": diagonal,
            "gap_candidates": gap_candidates,
            "missing_hits": missing_hits,
            "repeat_numbers": repeat_numbers,
            "recent_missing_numbers": recent_missing[:30],
            "hot_numbers": hot,
            "big_small": {"big": big_count, "small": small_count},
            "odd_even": {"odd": odd_count, "even": even_count},
        },
    }


def run_laowanjia_feature_analysis(limit: int = 100) -> dict:
    try:
        draws = _load_recent_draws(limit)
        record = build_laowanjia_feature_record(draws)
        if not record:
            return {"status": "error", "message": "no draw data available", "data": None}
        saved = save_laowanjia_feature(record)
        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "data": record,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("laowanjia feature analysis failed")
        return {"status": "error", "message": str(exc), "data": None}

