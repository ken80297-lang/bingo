"""Shadow feature learning for Bingo AI Pro.

Pure feature extraction only: this module never changes production prediction
weights. Draws are expected newest-first and only information present in the
supplied history is used.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from math import comb

WINDOWS = (5, 10, 20, 30, 50, 100, 300)
UNIVERSE_SIZE = 80
DRAW_SIZE = 20


def _numbers(draw: dict) -> tuple[int, ...]:
    return tuple(sorted({int(n) for n in draw.get("numbers", []) if 1 <= int(n) <= 80}))


def _super(draw: dict) -> int | None:
    raw = draw.get("super_number", draw.get("super"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 80 else None


def omission_ages(draws: list[dict]) -> dict[int, int]:
    """Consecutive draws since each number last appeared."""
    ages = {}
    for number in range(1, 81):
        age = 0
        for draw in draws:
            if number in _numbers(draw):
                break
            age += 1
        ages[number] = age
    return ages


def super_omission_ages(draws: list[dict]) -> dict[int, int]:
    ages = {}
    supers = [_super(draw) for draw in draws]
    for number in range(1, 81):
        age = 0
        for value in supers:
            if value == number:
                break
            age += 1
        ages[number] = age
    return ages


def pair_lift(draws: list[dict], window: int = 30) -> list[dict]:
    """Rank pair co-occurrence against the random 20-of-80 baseline."""
    sample = [_numbers(d) for d in draws[:window]]
    sample = [nums for nums in sample if len(nums) == DRAW_SIZE]
    n = len(sample)
    if not n:
        return []

    counts = Counter()
    for nums in sample:
        counts.update(combinations(nums, 2))

    baseline = comb(DRAW_SIZE, 2) / comb(UNIVERSE_SIZE, 2)
    rows = []
    for pair, count in counts.items():
        rate = count / n
        rows.append({
            "numbers": list(pair),
            "count": count,
            "rate": rate,
            "baseline": baseline,
            "lift": rate / baseline,
        })
    return sorted(rows, key=lambda row: (row["lift"], row["count"], row["numbers"]), reverse=True)


def triple_lift(draws: list[dict], window: int = 30) -> list[dict]:
    sample = [_numbers(d) for d in draws[:window]]
    sample = [nums for nums in sample if len(nums) == DRAW_SIZE]
    n = len(sample)
    if not n:
        return []

    counts = Counter()
    for nums in sample:
        counts.update(combinations(nums, 3))

    baseline = comb(DRAW_SIZE, 3) / comb(UNIVERSE_SIZE, 3)
    rows = []
    for triple, count in counts.items():
        rate = count / n
        rows.append({
            "numbers": list(triple),
            "count": count,
            "rate": rate,
            "baseline": baseline,
            "lift": rate / baseline,
        })
    return sorted(rows, key=lambda row: (row["lift"], row["count"], row["numbers"]), reverse=True)


def super_features(draws: list[dict], window: int = 30) -> dict:
    sample = draws[:window]
    supers = [value for value in (_super(d) for d in sample) if value is not None]
    number_counts = Counter(supers)
    tail_counts = Counter(value % 10 for value in supers)
    return {
        "sample_size": len(supers),
        "number_counts": dict(sorted(number_counts.items())),
        "tail_counts": dict(sorted(tail_counts.items())),
        "number_omission": super_omission_ages(draws),
    }


def build_shadow_snapshot(draws: list[dict]) -> dict:
    """Build diagnostics for learning/verification without production influence."""
    available = [w for w in WINDOWS if len(draws) >= w]
    windows = available or ([len(draws)] if draws else [])
    return {
        "mode": "shadow",
        "production_weight_effect": False,
        "history_size": len(draws),
        "windows": {
            str(window): {
                "top_pairs": pair_lift(draws, window)[:20],
                "top_triples": triple_lift(draws, window)[:12],
                "super": super_features(draws, window),
            }
            for window in windows
        },
        "number_omission": omission_ages(draws),
    }
