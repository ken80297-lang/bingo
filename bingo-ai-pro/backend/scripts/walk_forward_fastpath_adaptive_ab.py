from __future__ import annotations

"""Leakage-safe walk-forward A/B for the formal Production Fast Path.

A = FastPath with adaptive V7 number bonuses.
B = identical FastPath with adaptive bonuses disabled.

This script intentionally reuses recommendation_center._build_fast_path_numbers so
zone/tail/previous-overlap constraints are identical in both arms.
"""

import argparse
import json
import math
from collections import defaultdict
from statistics import mean

from database.collector_store import get_draw_history
from services.model_engine import run_all_models
from services.recommendation_center import _build_fast_path_numbers

MODELS = ("laowanjia", "hotcold", "missing", "pattern", "balance")
KEYS = {
    "laowanjia": "laowanjia_weight",
    "hotcold": "hot_cold_weight",
    "missing": "missing_weight",
    "pattern": "pattern_weight",
    "balance": "balance_weight",
}


def _numbers(draw):
    out = []
    for value in draw.get("numbers") or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in out:
            out.append(number)
    return out


def _hits(predicted, official):
    return len(set(predicted) & set(official))


def _ci(values):
    if not values:
        return {"mean": 0.0, "low": 0.0, "high": 0.0}
    avg = mean(values)
    if len(values) < 2:
        return {"mean": avg, "low": avg, "high": avg}
    se = math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1) / len(values))
    return {"mean": avg, "low": avg - 1.96 * se, "high": avg + 1.96 * se}


def _weights(performance, version):
    if any(len(performance[model]) < 20 for model in MODELS):
        return None
    averages = {model: mean(performance[model][-100:]) for model in MODELS}
    center = mean(averages.values()) or 1.0
    return {
        "strategy": "v7_models",
        "version": version,
        **{KEYS[model]: max(0.5, min(1.5, averages[model] / center)) for model in MODELS},
    }


def _adaptive_scores(models, adaptive):
    scores = {}
    multipliers = {}
    for model in models:
        key = str(model.get("model") or "")
        weight_key = KEYS.get(key)
        multiplier = float(adaptive.get(weight_key, 1.0)) if adaptive and weight_key else 1.0
        multiplier = max(0.5, min(1.5, multiplier))
        multipliers[key] = multiplier
        confidence = float(model.get("confidence") or 0)
        base_weight = max(1.0, confidence / 20.0) * multiplier
        for rank, number in enumerate(_numbers({"numbers": model.get("candidate_numbers") or []})):
            scores[number] = scores.get(number, 0.0) + base_weight + max(0, 20 - rank) * 0.15
    return scores, multipliers


def run(draws, warmup=100):
    clean = [{**draw, "issue": str(draw.get("issue")), "numbers": _numbers(draw)} for draw in draws]
    clean = [draw for draw in clean if len(draw["numbers"]) == 20 and draw["issue"].isdigit()]
    clean.sort(key=lambda draw: int(draw["issue"]))

    performance = defaultdict(list)
    rows = []
    version = 0
    previous_on = []
    previous_off = []

    for index in range(warmup, len(clean)):
        history = list(reversed(clean[max(0, index - 100):index]))
        target = clean[index]
        source = clean[index - 1]
        official = target["numbers"]

        payload = run_all_models(100, draws=history)
        models = payload.get("models") or []
        adaptive = _weights(performance, version + 1)
        if adaptive:
            version += 1
        adaptive_scores, multipliers = _adaptive_scores(models, adaptive) if adaptive else ({}, {})

        analysis = dict(source)
        on, _ = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_on,
            trace=[],
            adaptive_number_scores=adaptive_scores,
        )
        off, _ = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_off,
            trace=[],
            adaptive_number_scores=None,
        )

        for model in models:
            key = str(model.get("model") or "")
            if key in MODELS:
                performance[key].append(_hits((model.get("candidate_numbers") or [])[:20], official))

        on_hits = _hits(on, official)
        off_hits = _hits(off, official)
        rows.append({
            "issue": target["issue"],
            "adaptive_enabled": adaptive is not None,
            "on20": on_hits,
            "off20": off_hits,
            "delta": on_hits - off_hits,
            "multipliers": multipliers,
        })
        previous_on = on
        previous_off = off

    active = [row for row in rows if row["adaptive_enabled"]]
    deltas = [row["delta"] for row in active]
    return {
        "summary": {
            "issues": len(rows),
            "adaptive_active_issues": len(active),
            "on20": mean(row["on20"] for row in active) if active else 0,
            "off20": mean(row["off20"] for row in active) if active else 0,
            "paired_on_minus_off_20": _ci(deltas),
            "wins": sum(delta > 0 for delta in deltas),
            "ties": sum(delta == 0 for delta in deltas),
            "losses": sum(delta < 0 for delta in deltas),
        },
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(run(get_draw_history(args.limit), args.warmup)["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
