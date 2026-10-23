"""Read-only research helpers for unrestricted repeat-number shadow ranking.

No database writes, scheduling, or production recommendation changes.
"""
from __future__ import annotations

from math import comb
from typing import Iterable, Sequence

UNIVERSE = frozenset(range(1, 81))
DRAW_SIZE = 20
BASE_RATE = DRAW_SIZE / len(UNIVERSE)


def _draw(numbers: Iterable[int]) -> frozenset[int]:
    values = list(numbers)
    if len(values) != DRAW_SIZE or len(set(values)) != DRAW_SIZE:
        raise ValueError("A draw must contain 20 distinct numbers")
    if any(type(n) is not int or n not in UNIVERSE for n in values):
        raise ValueError("Draw numbers must be integers from 1 to 80")
    return frozenset(values)


def repeat_count_distribution() -> dict[int, float]:
    """Exact hypergeometric null for overlap of consecutive draws."""
    denominator = comb(80, 20)
    return {
        k: comb(20, k) * comb(60, 20 - k) / denominator
        for k in range(21)
    }


def consecutive_overlaps(draws: Sequence[Iterable[int]]) -> list[int]:
    parsed = [_draw(d) for d in draws]
    return [len(a & b) for a, b in zip(parsed, parsed[1:])]


def repeat_streaks(draws: Sequence[Iterable[int]]) -> dict[int, int]:
    """Streak lengths ending at the last observed draw (zero if absent)."""
    parsed = [_draw(d) for d in draws]
    streaks = {n: 0 for n in UNIVERSE}
    for draw in parsed:
        for n in UNIVERSE:
            streaks[n] = streaks[n] + 1 if n in draw else 0
    return streaks


def select_shadow_twenty(
    scores: dict[int, float], previous_draw: Iterable[int], tie_seed: int = 0
) -> dict:
    """Select top 20 freely, without repeat quotas or repeat bonuses."""
    previous = _draw(previous_draw)
    if set(scores) != UNIVERSE:
        raise ValueError("Scores must cover exactly all 80 numbers")
    from math import isfinite
    if any(not isinstance(s, (int, float)) or not isfinite(s) for s in scores.values()):
        raise ValueError("Scores must be finite numbers")
    # Full-cycle affine permutation avoids systematically favoring low numbers on ties.
    selected = sorted(UNIVERSE, key=lambda n: (-scores[n], ((n - 1) * 37 + tie_seed) % 80))[:DRAW_SIZE]
    repeated = sorted(set(selected) & previous)
    return {
        "selected_numbers": sorted(selected),
        "repeated_numbers": repeated,
        "repeat_count": len(repeated),
        "new_count": DRAW_SIZE - len(repeated),
        "null_expected_hits": 5.0,
    }


def walk_forward_repeat_rate(
    draws: Sequence[Iterable[int]], minimum_history: int = 100
) -> dict:
    """Evaluate repeat continuation only after enough prior observations.

    Each prediction uses strictly earlier transitions; never the target draw.
    Shrinks empirical rate toward the 25% null to limit small-sample noise.
    """
    parsed = [_draw(d) for d in draws]
    if minimum_history < 2:
        raise ValueError("minimum_history must be >= 2")
    successes = 0
    trials = 0
    evaluations = []
    for i in range(1, len(parsed)):
        if i >= minimum_history:
            estimate = (successes + 25.0) / (trials + 100.0)
            actual = len(parsed[i - 1] & parsed[i])
            evaluations.append({
                "target_index": i,
                "predicted_repeat_rate": estimate,
                "observed_repeat_count": actual,
                "baseline_repeat_rate": BASE_RATE,
            })
        successes += len(parsed[i - 1] & parsed[i])
        trials += DRAW_SIZE
    return {
        "evaluations": evaluations,
        "evaluated_draws": len(evaluations),
        "mean_observed_repeats": (
            sum(e["observed_repeat_count"] for e in evaluations) / len(evaluations)
            if evaluations else None
        ),
        "null_expected_repeats": 5.0,
    }
