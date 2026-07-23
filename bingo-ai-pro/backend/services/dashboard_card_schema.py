from __future__ import annotations

from typing import Any


SMALL_MAX = 40
DRAW_SIZE = 20


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def valid_numbers(values: Any, *, limit: int | None = None) -> list[int]:
    numbers: list[int] = []
    if isinstance(values, str):
        try:
            import json

            parsed = json.loads(values)
            values = parsed if isinstance(parsed, list) else [values]
        except Exception:
            values = [values]
    for value in values or []:
        number = as_int(value)
        if number is not None and 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    numbers.sort()
    return numbers[:limit] if limit else numbers


def confidence_percent(value: Any, *, fallback: float = 0) -> float:
    try:
        number = float(value)
    except Exception:
        number = fallback
    if 0 < number <= 1:
        number *= 100
    return round(max(0, min(100, number)), 2)


def confidence_ratio(value: Any, *, fallback: float = 0) -> float:
    return round(confidence_percent(value, fallback=fallback) / 100, 4)


def super_candidates(record: dict) -> list[int]:
    candidates = valid_numbers(
        record.get("super_number_candidates")
        or record.get("super_candidates")
        or record.get("super_recommendation")
        or []
    )
    primary = as_int(record.get("super_number"))
    if primary and 1 <= primary <= 80 and primary not in candidates:
        candidates.insert(0, primary)
    return candidates[:3]


def size_prediction(numbers: list[int], record: dict | None = None) -> dict:
    record = record or {}
    stored = record.get("size_prediction")
    if isinstance(stored, dict):
        small = as_int(stored.get("small_count"))
        large = as_int(stored.get("large_count"))
        if small is not None and large is not None and small + large == DRAW_SIZE:
            return {
                "small_count": small,
                "large_count": large,
                "label": stored.get("label") or _size_label(small, large),
                "confidence": confidence_ratio(stored.get("confidence"), fallback=record.get("confidence") or 0),
                "confidence_percent": confidence_percent(stored.get("confidence"), fallback=record.get("confidence") or 0),
                "source": stored.get("source") or "prediction_record",
                "fallback_used": bool(stored.get("fallback_used", False)),
            }
    small = sum(1 for number in numbers if number <= SMALL_MAX)
    large = len(numbers) - small
    fallback_used = len(numbers) != DRAW_SIZE
    return {
        "small_count": small,
        "large_count": large,
        "label": _size_label(small, large),
        "confidence": confidence_ratio(record.get("confidence"), fallback=0),
        "confidence_percent": confidence_percent(record.get("confidence"), fallback=0),
        "source": "recommend_numbers_distribution",
        "fallback_used": fallback_used,
        "fallback_reason": "recommend_numbers_count_not_20" if fallback_used else None,
    }


def odd_even_prediction(numbers: list[int], record: dict | None = None) -> dict:
    record = record or {}
    stored = record.get("odd_even_prediction")
    if isinstance(stored, dict):
        odd = as_int(stored.get("odd_count"))
        even = as_int(stored.get("even_count"))
        if odd is not None and even is not None and odd + even == DRAW_SIZE:
            return {
                "odd_count": odd,
                "even_count": even,
                "label": stored.get("label") or _odd_even_label(odd, even),
                "confidence": confidence_ratio(stored.get("confidence"), fallback=record.get("confidence") or 0),
                "confidence_percent": confidence_percent(stored.get("confidence"), fallback=record.get("confidence") or 0),
                "source": stored.get("source") or "prediction_record",
                "fallback_used": bool(stored.get("fallback_used", False)),
            }
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    fallback_used = len(numbers) != DRAW_SIZE
    return {
        "odd_count": odd,
        "even_count": even,
        "label": _odd_even_label(odd, even),
        "confidence": confidence_ratio(record.get("confidence"), fallback=0),
        "confidence_percent": confidence_percent(record.get("confidence"), fallback=0),
        "source": "recommend_numbers_distribution",
        "fallback_used": fallback_used,
        "fallback_reason": "recommend_numbers_count_not_20" if fallback_used else None,
    }


def high_probability_numbers(record: dict, recommend_numbers: list[int], rule_library: dict | None = None) -> dict:
    stored = valid_numbers(record.get("high_probability_numbers"), limit=5)
    if len(stored) == 5 and set(stored).issubset(set(recommend_numbers)):
        details = _stored_high_probability_details(record, stored)
        return {
            "numbers": stored,
            "details": details,
            "fallback_used": False,
            "source": "prediction_record",
        }

    scores = _score_recommend_numbers(record, recommend_numbers, rule_library)
    ranked = sorted(
        recommend_numbers,
        key=lambda number: (-scores.get(number, {}).get("score", 0), number),
    )[:5]
    details = [
        {
            "number": number,
            "score": round(scores.get(number, {}).get("score", 0), 2),
            "rank": index + 1,
            "reasons": scores.get(number, {}).get("reasons", [])[:4],
        }
        for index, number in enumerate(ranked)
    ]
    return {
        "numbers": ranked,
        "details": details,
        "fallback_used": True,
        "source": "score_blend",
        "fallback_reason": "stored_high_probability_numbers_unavailable",
    }


def validation_diagnostics(card: dict) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    current_numbers = valid_numbers((card.get("current_draw") or {}).get("numbers"))
    recommend = valid_numbers(card.get("recommend_numbers"))
    high_prob = valid_numbers(card.get("high_probability_numbers"))
    supers = valid_numbers(card.get("super_candidates"))
    size = card.get("size_prediction") or {}
    odd_even = card.get("odd_even_prediction") or {}

    if current_numbers and len(current_numbers) != DRAW_SIZE:
        errors.append("current_draw_numbers_count_not_20")
    if recommend and len(recommend) != DRAW_SIZE:
        errors.append("recommend_numbers_count_not_20")
    if len(high_prob) != 5:
        errors.append("high_probability_numbers_count_not_5")
    if high_prob and not set(high_prob).issubset(set(recommend)):
        errors.append("high_probability_numbers_not_subset_of_recommend_numbers")
    if len(supers) > 3:
        errors.append("super_candidates_count_over_3")
    if as_int(size.get("small_count")) is None or as_int(size.get("large_count")) is None:
        warnings.append("size_prediction_missing_counts")
    elif as_int(size.get("small_count")) + as_int(size.get("large_count")) != DRAW_SIZE:
        errors.append("size_prediction_count_sum_not_20")
    if as_int(odd_even.get("odd_count")) is None or as_int(odd_even.get("even_count")) is None:
        warnings.append("odd_even_prediction_missing_counts")
    elif as_int(odd_even.get("odd_count")) + as_int(odd_even.get("even_count")) != DRAW_SIZE:
        errors.append("odd_even_prediction_count_sum_not_20")

    confidence = card.get("confidence")
    try:
        confidence_value = float(confidence)
        if not 0 <= confidence_value <= 1:
            errors.append("confidence_ratio_out_of_range")
    except Exception:
        errors.append("confidence_ratio_invalid")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _size_label(small: int, large: int) -> str:
    if small > large:
        return "small"
    if large > small:
        return "large"
    return "balanced"


def _odd_even_label(odd: int, even: int) -> str:
    if odd > even:
        return "odd"
    if even > odd:
        return "even"
    return "balanced"


def _stored_high_probability_details(record: dict, numbers: list[int]) -> list[dict]:
    details = record.get("high_probability_details")
    if isinstance(details, list):
        by_number = {
            as_int(item.get("number")): item
            for item in details
            if isinstance(item, dict) and as_int(item.get("number")) in numbers
        }
        if len(by_number) == len(numbers):
            return [
                {
                    "number": number,
                    "score": confidence_percent(by_number[number].get("score")),
                    "rank": index + 1,
                    "reasons": list(by_number[number].get("reasons") or [])[:4],
                }
                for index, number in enumerate(numbers)
            ]
    return [
        {
            "number": number,
            "score": confidence_percent(record.get("confidence"), fallback=0),
            "rank": index + 1,
            "reasons": ["prediction_record"],
        }
        for index, number in enumerate(numbers)
    ]


def _score_recommend_numbers(record: dict, recommend_numbers: list[int], rule_library: dict | None) -> dict[int, dict]:
    scores = {number: {"score": 0.0, "reasons": []} for number in recommend_numbers}
    model_scores = record.get("model_scores") or {}
    if isinstance(model_scores, dict):
        for model_name, payload in model_scores.items():
            if not isinstance(payload, dict):
                continue
            model_confidence = confidence_percent(payload.get("confidence") or payload.get("total_score"))
            model_candidates = set(valid_numbers(payload.get("candidate_numbers") or payload.get("numbers")))
            for number in recommend_numbers:
                if number in model_candidates:
                    scores[number]["score"] += model_confidence
                    scores[number]["reasons"].append(f"model:{model_name}")

    for rank, number in enumerate(recommend_numbers):
        scores[number]["score"] += max(0, 20 - rank)
        scores[number]["reasons"].append("recommendation_rank")

    for item in (rule_library or {}).get("rules") or []:
        if not isinstance(item, dict):
            continue
        rule_score = confidence_percent(item.get("score") or item.get("confidence"))
        for number in valid_numbers(item.get("candidate_numbers")):
            if number in scores:
                scores[number]["score"] += rule_score * 0.25
                scores[number]["reasons"].append(f"rule:{item.get('key') or item.get('name')}")

    for number, payload in scores.items():
        if not payload["reasons"]:
            payload["reasons"].append("recommend_numbers")
        payload["score"] = min(100, payload["score"])
    return scores
