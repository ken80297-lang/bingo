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
from database.analysis_store import build_analysis_record
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


def _adaptive_scores(models, adaptive, strength=1.0):
    scores = {}
    multipliers = {}
    for model in models:
        key = str(model.get("model") or "")
        weight_key = KEYS.get(key)
        multiplier = float(adaptive.get(weight_key, 1.0)) if adaptive and weight_key else 1.0
        multiplier = 1.0 + (multiplier - 1.0) * float(strength)
        multiplier = max(0.5, min(1.5, multiplier))
        multipliers[key] = multiplier
        confidence = float(model.get("confidence") or 0)
        base_weight = max(1.0, confidence / 20.0) * multiplier
        for rank, number in enumerate(_numbers({"numbers": model.get("candidate_numbers") or []})):
            scores[number] = scores.get(number, 0.0) + base_weight + max(0, 20 - rank) * 0.15
    return scores, multipliers


def _signal(analysis):
    cluster_score = float(analysis.get("cluster_score") or 0)
    pattern_score = (
        len(analysis.get("consecutive") or []) * 2
        + len(analysis.get("twins") or [])
        + float(analysis.get("diagonal_score") or 0) / 12.0
    )
    return cluster_score, pattern_score


def _quantile(values, q):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _regime(analysis, prior_signals):
    cluster_score, pattern_score = _signal(analysis)
    if len(prior_signals) < 30:
        return "normal"
    cluster_cut = _quantile([item[0] for item in prior_signals[-100:]], 2 / 3)
    pattern_cut = _quantile([item[1] for item in prior_signals[-100:]], 2 / 3)
    cluster_excess = cluster_score - cluster_cut
    pattern_excess = pattern_score - pattern_cut
    if cluster_excess < 0 and pattern_excess < 0:
        return "normal"
    if pattern_excess > cluster_excess:
        return "pattern_active"
    return "large_cluster"


def _conditional_weights(performance_by_regime, regime, version):
    bucket = performance_by_regime.get(regime) or {}
    if any(len(bucket.get(model) or []) < 20 for model in MODELS):
        return None
    averages = {model: mean(bucket[model][-100:]) for model in MODELS}
    center = mean(averages.values()) or 1.0
    return {
        "strategy": "v7_conditional",
        "version": version,
        **{KEYS[model]: max(0.5, min(1.5, averages[model] / center)) for model in MODELS},
    }


def run(draws, warmup=100, strengths=(1.0, 2.0, 3.0, 5.0)):
    clean = [{**draw, "issue": str(draw.get("issue")), "numbers": _numbers(draw)} for draw in draws]
    clean = [draw for draw in clean if len(draw["numbers"]) == 20 and draw["issue"].isdigit()]
    clean.sort(key=lambda draw: int(draw["issue"]))

    performance = defaultdict(list)
    performance_by_regime = defaultdict(lambda: defaultdict(list))
    rows = []
    version = 0
    previous_by_strength = {float(strength): [] for strength in strengths}
    previous_conditional = []
    previous_neutral = []
    previous_off = []
    prior_signals = []

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
        analysis_recent = list(reversed(clean[max(0, index - 100):index - 1]))
        analysis = build_analysis_record(source, recent_draws=analysis_recent)
        regime = _regime(analysis, prior_signals)
        conditional = _conditional_weights(performance_by_regime, regime, version + 1)
        conditional_scores, conditional_multipliers = _adaptive_scores(models, conditional) if conditional else ({}, {})
        conditional_numbers, _ = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_conditional,
            trace=[],
            adaptive_number_scores=conditional_scores,
        )
        learned_by_strength = {}
        multipliers_by_strength = {}
        for strength in strengths:
            strength = float(strength)
            adaptive_scores, strength_multipliers = _adaptive_scores(models, adaptive, strength=strength) if adaptive else ({}, {})
            learned_numbers, _ = _build_fast_path_numbers(
                analysis,
                source_issue=source["issue"],
                target_issue=target["issue"],
                previous_numbers=previous_by_strength[strength],
                trace=[],
                adaptive_number_scores=adaptive_scores,
            )
            learned_by_strength[strength] = learned_numbers
            multipliers_by_strength[strength] = strength_multipliers
        neutral_scores, _ = _adaptive_scores(
            models,
            {"strategy": "v7_models", **{key: 1.0 for key in KEYS.values()}},
        )
        neutral, _ = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_neutral,
            trace=[],
            adaptive_number_scores=neutral_scores,
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
                model_hits = _hits((model.get("candidate_numbers") or [])[:20], official)
                performance[key].append(model_hits)
                performance_by_regime[regime][key].append(model_hits)

        learned_hits = {strength: _hits(numbers, official) for strength, numbers in learned_by_strength.items()}
        neutral_hits = _hits(neutral, official)
        off_hits = _hits(off, official)
        rows.append({
            "issue": target["issue"],
            "adaptive_enabled": adaptive is not None,
            "regime": regime,
            "conditional_enabled": conditional is not None,
            "conditional20": _hits(conditional_numbers, official),
            "conditional_multipliers": conditional_multipliers,
            "conditional_changed_numbers": len(set(conditional_numbers) ^ set(neutral)) // 2,
            "conditional_same_set": set(conditional_numbers) == set(neutral),
            "conditional_multiplier_spread": (max(conditional_multipliers.values()) - min(conditional_multipliers.values())) if conditional_multipliers else 0.0,
            "learned_by_strength": {str(strength): hits for strength, hits in learned_hits.items()},
            "neutral20": neutral_hits,
            "off20": off_hits,
            "multipliers_by_strength": {str(strength): values for strength, values in multipliers_by_strength.items()},
        })
        for strength, numbers in learned_by_strength.items():
            previous_by_strength[strength] = numbers
        previous_conditional = conditional_numbers
        previous_neutral = neutral
        previous_off = off
        prior_signals.append(_signal(analysis))

    active = [row for row in rows if row["adaptive_enabled"]]
    strength_summary = {}
    for strength in strengths:
        key = str(float(strength))
        deltas = [row["learned_by_strength"][key] - row["neutral20"] for row in active]
        strength_summary[key] = {
            "mean20": mean(row["learned_by_strength"][key] for row in active) if active else 0,
            "vs_neutral": _ci(deltas),
            "wins": sum(delta > 0 for delta in deltas),
            "ties": sum(delta == 0 for delta in deltas),
            "losses": sum(delta < 0 for delta in deltas),
        }
    neutral_off = [row["neutral20"] - row["off20"] for row in active]
    conditional_active = [row for row in rows if row.get("conditional_enabled")]
    conditional_deltas = [row["conditional20"] - row["neutral20"] for row in conditional_active]
    regime_summary = {}
    for regime in sorted({row.get("regime") for row in conditional_active}):
        group = [row for row in conditional_active if row.get("regime") == regime]
        deltas = [row["conditional20"] - row["neutral20"] for row in group]
        model_performance = {}
        bucket = performance_by_regime.get(regime) or {}
        for model in MODELS:
            values = list(bucket.get(model) or [])
            model_performance[model] = {
                "samples": len(values),
                "mean20": mean(values) if values else 0,
                "recent100_mean20": mean(values[-100:]) if values else 0,
            }
        regime_summary[regime] = {
            "issues": len(group),
            "same_set_issues": sum(bool(row.get("conditional_same_set")) for row in group),
            "mean_changed_numbers": mean(row.get("conditional_changed_numbers", 0) for row in group),
            "mean_multiplier_spread": mean(row.get("conditional_multiplier_spread", 0.0) for row in group),
            "conditional20": mean(row["conditional20"] for row in group),
            "neutral20": mean(row["neutral20"] for row in group),
            "vs_neutral": _ci(deltas),
            "models": model_performance,
        }
    return {
        "summary": {
            "issues": len(rows),
            "adaptive_active_issues": len(active),
            "neutral20": mean(row["neutral20"] for row in active) if active else 0,
            "off20": mean(row["off20"] for row in active) if active else 0,
            "strengths": strength_summary,
            "paired_neutral_minus_off_20": _ci(neutral_off),
            "conditional": {
                "active_issues": len(conditional_active),
                "mean20": mean(row["conditional20"] for row in conditional_active) if conditional_active else 0,
                "neutral20": mean(row["neutral20"] for row in conditional_active) if conditional_active else 0,
                "vs_neutral": _ci(conditional_deltas),
                "wins": sum(delta > 0 for delta in conditional_deltas),
                "ties": sum(delta == 0 for delta in conditional_deltas),
                "losses": sum(delta < 0 for delta in conditional_deltas),
                "same_set_issues": sum(bool(row.get("conditional_same_set")) for row in conditional_active),
                "changed_set_issues": sum(not bool(row.get("conditional_same_set")) for row in conditional_active),
                "mean_changed_numbers": mean(row.get("conditional_changed_numbers", 0) for row in conditional_active) if conditional_active else 0,
                "mean_multiplier_spread": mean(row.get("conditional_multiplier_spread", 0.0) for row in conditional_active) if conditional_active else 0,
                "regimes": regime_summary,
            },
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
