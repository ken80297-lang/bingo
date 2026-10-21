"""Read-only, deterministic top-5/top-6 evaluation helpers.

Never use actual draw numbers to select candidates. Call rank_candidates at
prediction time with scores known at that time, persist the selections, and
call evaluate_selection only after the target draw is published.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite


def rank_candidates(recommended_twenty: Sequence[int], scores: Mapping[int, float]) -> dict[str, list[int]]:
    """Rank strictly within the frozen 20-number production recommendation."""
    numbers = list(recommended_twenty)
    if len(numbers) != 20 or len(set(numbers)) != 20 or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 80 for n in numbers):
        raise ValueError("recommended_twenty must contain 20 distinct integers from 1 to 80")
    for n in numbers:
        if n not in scores or not isfinite(float(scores[n])):
            raise ValueError(f"missing or non-finite prediction-time score for {n}")
    ordered = sorted(numbers, key=lambda n: (-float(scores[n]), n))
    return {"top5": ordered[:5], "top6": ordered[:6]}


def evaluate_selection(selection: Sequence[int], actual_twenty: Sequence[int]) -> int:
    """Score a previously frozen selection against the actual target draw."""
    picks, actual = list(selection), list(actual_twenty)
    if len(picks) not in (5, 6) or len(set(picks)) != len(picks):
        raise ValueError("selection must contain 5 or 6 distinct numbers")
    if len(actual) != 20 or len(set(actual)) != 20:
        raise ValueError("actual_twenty must contain 20 distinct numbers")
    if any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 80 for n in picks + actual):
        raise ValueError("all numbers must be integers from 1 to 80")
    return len(set(picks).intersection(actual))


def random_baseline(selection_size: int) -> float:
    if selection_size not in (5, 6):
        raise ValueError("selection_size must be 5 or 6")
    return selection_size / 4.0
