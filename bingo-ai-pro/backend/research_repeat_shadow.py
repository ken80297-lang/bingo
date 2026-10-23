"""Offline walk-forward research on historical 20-number Bingo draws.

Input CSV: chronological rows, 20 columns n1..n20 (or number_1..number_20).
No production DB access or writes. Report is printed as JSON.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from services.repeat_number_shadow import (
    _draw, consecutive_overlaps, repeat_count_distribution,
    select_shadow_twenty,
)


def read_draws(path: Path) -> list[list[int]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        if not rows.fieldnames:
            raise ValueError("CSV must have a header")
        names = [f"n{i}" for i in range(1, 21)]
        if not all(n in rows.fieldnames for n in names):
            names = [f"number_{i}" for i in range(1, 21)]
        if not all(n in rows.fieldnames for n in names):
            raise ValueError("CSV requires n1..n20 or number_1..number_20")
        records = list(rows)
        issue_column = next((name for name in ("issue", "draw_issue", "issue_number") if name in rows.fieldnames), None)
        if issue_column is None:
            raise ValueError("CSV must include issue, draw_issue, or issue_number to verify chronology")
        issues = [int(row[issue_column]) for row in records]
        if any(b <= a for a, b in zip(issues, issues[1:])):
            raise ValueError("Issues must be strictly increasing with no duplicates")
        return [sorted(_draw(int(row[n]) for n in names)) for row in records]


def evaluate(draws: list[list[int]], warmup: int = 100) -> dict:
    if warmup < 2 or len(draws) <= warmup:
        raise ValueError("Need more draws than warmup (minimum warmup 2)")
    # Frequency model is an intentionally simple control, not a claimed edge.
    # Scores for draw i use only draws strictly before i.
    scores = {n: 0 for n in range(1, 81)}
    for draw in draws[:warmup]:
        for n in draw:
            scores[n] += 1
    baseline_hits, candidate_hits, repeat_counts = [], [], []
    for i in range(warmup, len(draws)):
        previous = set(draws[i - 1])
        actual = set(draws[i])
        # Control: top-20 by cumulative frequency, no explicit repeat feature.
        control = select_shadow_twenty(scores, previous)
        # Candidate: one-step repeat feature with Laplace-style shrinkage.
        # This is deliberately evaluated separately and must beat control out-of-sample.
        candidate_scores = {n: scores[n] + (1 if n in previous else 0) for n in scores}
        candidate = select_shadow_twenty(candidate_scores, previous)
        baseline_hits.append(len(set(control["selected_numbers"]) & actual))
        candidate_hits.append(len(set(candidate["selected_numbers"]) & actual))
        repeat_counts.append(candidate["repeat_count"])
        for n in actual:
            scores[n] += 1
    diffs = [a - b for a, b in zip(candidate_hits, baseline_hits)]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    variance = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1) if n > 1 else 0
    se = math.sqrt(variance / n)
    return {
        "sample_size": n,
        "warmup": warmup,
        "random_expected_hits": 5.0,
        "control_mean_hits": sum(baseline_hits) / n,
        "candidate_mean_hits": sum(candidate_hits) / n,
        "paired_mean_hit_difference": mean_diff,
        "paired_normal_approx_95pct_interval": [mean_diff - 1.96 * se, mean_diff + 1.96 * se],
        "candidate_mean_repeated_numbers": sum(repeat_counts) / n,
        "overlap_null_expected": sum(k * p for k, p in repeat_count_distribution().items()),
        "notes": "Exploratory only. Serial dependence and multiple comparisons require stronger inference.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(evaluate(read_draws(args.csv_path), args.warmup), indent=2))


if __name__ == "__main__":
    main()
