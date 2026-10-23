"""Offline Top-6 combination research. Never reads or changes production data.

Usage:
  python research_top6_combinations.py chronological_draws.csv --warmup 100

Draws must be strictly chronological, with issue and n1..n20 columns.
All selections use prior draws only. No model is assumed to beat chance.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from itertools import combinations
from pathlib import Path

from research_repeat_shadow import read_draws


def choose_six(history: list[list[int]], method: str, window: int) -> list[int]:
    """Rank all 80 numbers; optionally reward historical co-occurrence."""
    recent = history[-window:]
    frequencies = Counter(n for draw in recent for n in draw)
    if method == "frequency":
        return sorted(sorted(range(1, 81), key=lambda n: (-frequencies[n], n))[:6])
    if method == "repeat":
        last = set(history[-1])
        return sorted(sorted(range(1, 81), key=lambda n: (-(frequencies[n] + (n in last)), n))[:6])
    if method == "combination":
        # Greedy six-number selection using historical pair co-occurrence.
        # Pair weights are deliberately small; evaluated out of sample.
        pair_counts = Counter(pair for draw in recent for pair in combinations(sorted(draw), 2))
        selected: list[int] = []
        while len(selected) < 6:
            next_n = max(
                (n for n in range(1, 81) if n not in selected),
                key=lambda n: (
                    frequencies[n] + 0.1 * sum(pair_counts[tuple(sorted((n, s)))] / max(1, len(recent)) for s in selected),
                    -n,
                ),
            )
            selected.append(next_n)
        return sorted(selected)
    raise ValueError(f"Unknown method: {method}")


def evaluate(draws: list[list[int]], warmup: int = 100, window: int = 100) -> dict:
    if warmup < 2 or window < 2 or len(draws) <= warmup:
        raise ValueError("Require warmup >= 2, window >= 2 and additional evaluation draws")
    strategies = ("frequency", "repeat", "combination")
    results = {method: Counter() for method in strategies}
    hit_series = {method: [] for method in strategies}
    for i in range(warmup, len(draws)):
        history = draws[:i]
        actual = set(draws[i])
        for method in strategies:
            prediction = choose_six(history, method, window)
            hits = len(set(prediction) & actual)
            results[method][hits] += 1
            hit_series[method].append(hits)
    total = len(draws) - warmup
    output = {}
    for method in strategies:
        hits = hit_series[method]
        mean = sum(hits) / total
        variance = sum((x - mean) ** 2 for x in hits) / (total - 1) if total > 1 else 0
        output[method] = {
            "mean_hits": mean,
            "hits_distribution": {str(k): results[method][k] for k in range(7)},
            "four_plus": sum(x >= 4 for x in hits),
            "five_plus": sum(x >= 5 for x in hits),
            "six_of_six": results[method][6],
            "mean_normal_95pct_interval": [
                mean - 1.96 * math.sqrt(variance / total),
                mean + 1.96 * math.sqrt(variance / total),
            ],
        }
    return {
        "sample_size": total,
        "warmup": warmup,
        "window": window,
        "random_expected_mean_hits": 1.5,
        "random_six_of_six_probability": math.comb(20, 6) / math.comb(80, 6),
        "strategies": output,
        "warning": "Exploratory, non-independent draws and multiple testing; not evidence of predictive edge.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--window", type=int, default=100)
    args = parser.parse_args()
    draws = read_draws(args.csv_path)
    print(json.dumps(evaluate(draws, args.warmup, args.window), indent=2))


if __name__ == "__main__":
    main()
