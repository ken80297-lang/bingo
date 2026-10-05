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
    previous_isolated = {model: [] for model in MODELS}
    previous_normal_hotcold_gate = []
    previous_shadow = {"zone": [], "tail": [], "previous": []}
    source_names = ("patch_numbers", "missing_numbers", "cold_numbers", "hot_numbers", "diagonal_pattern", "repeated_numbers", "latest_draw_numbers")
    previous_source_shadow = {name: [] for name in source_names}
    previous_selective = []
    prior_regime_deltas = defaultdict(list)
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
        signal_cluster, signal_pattern = _signal(analysis)
        cluster_cut = _quantile([item[0] for item in prior_signals[-100:]], 2 / 3) if len(prior_signals) >= 30 else 0.0
        pattern_cut = _quantile([item[1] for item in prior_signals[-100:]], 2 / 3) if len(prior_signals) >= 30 else 0.0
        conditional = _conditional_weights(performance_by_regime, regime, version + 1)
        conditional_scores, conditional_multipliers = _adaptive_scores(models, conditional) if conditional else ({}, {})
        neutral_probe_scores, _ = _adaptive_scores(models, {"strategy": "neutral", **{key: 1.0 for key in KEYS.values()}})
        conditional_rank20 = set(sorted(conditional_scores, key=conditional_scores.get, reverse=True)[:20]) if conditional_scores else set()
        neutral_rank20 = set(sorted(neutral_probe_scores, key=neutral_probe_scores.get, reverse=True)[:20])
        raw_rank_changed = len(conditional_rank20 ^ neutral_rank20) // 2 if conditional else 0
        conditional_trace = []
        conditional_numbers, conditional_diversity = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_conditional,
            trace=conditional_trace,
            adaptive_number_scores=conditional_scores,
        )
        shadow_numbers = {}
        for shadow_name, overrides in (
            ("zone", {"zone_limit": 20}),
            ("tail", {"tail_limit": 20}),
            ("previous", {"previous_limit": 20}),
        ):
            shadow_numbers[shadow_name], _ = _build_fast_path_numbers(
                analysis,
                source_issue=source["issue"],
                target_issue=target["issue"],
                previous_numbers=previous_shadow[shadow_name],
                trace=[],
                adaptive_number_scores=conditional_scores,
                constraint_overrides=overrides,
            )
        source_shadow_numbers = {}
        for source_name in source_names:
            source_shadow_numbers[source_name], _ = _build_fast_path_numbers(
                analysis,
                source_issue=source["issue"],
                target_issue=target["issue"],
                previous_numbers=previous_source_shadow[source_name],
                trace=[],
                adaptive_number_scores=conditional_scores,
                source_weight_overrides={source_name: 0.0},
            )
        gated_conditional_numbers = conditional_numbers
        gated_hotcold_suppressed = False
        if conditional and regime == "normal":
            hotcold_key = KEYS.get("hotcold")
            hotcold_multiplier = float(conditional.get(hotcold_key, 1.0)) if hotcold_key else 1.0
            if abs(hotcold_multiplier - 1.0) >= 0.04:
                gated_weights = dict(conditional)
                gated_weights[hotcold_key] = 1.0
                gated_scores, _ = _adaptive_scores(models, gated_weights)
                gated_conditional_numbers, _ = _build_fast_path_numbers(
                    analysis,
                    source_issue=source["issue"],
                    target_issue=target["issue"],
                    previous_numbers=previous_normal_hotcold_gate,
                    trace=[],
                    adaptive_number_scores=gated_scores,
                )
                gated_hotcold_suppressed = True
        prior_deltas = prior_regime_deltas[regime]
        prior_ci = _ci(prior_deltas[-100:]) if len(prior_deltas) >= 30 else {"mean": 0.0, "low": 0.0, "high": 0.0}
        selective_enabled = bool(conditional and len(prior_deltas) >= 30 and prior_ci["low"] > 0)
        neutral_weight_map = {"strategy": "neutral"}
        neutral_weight_map.update({key: 1.0 for key in KEYS.values()})
        selective_scores = conditional_scores if selective_enabled else _adaptive_scores(models, neutral_weight_map)[0]
        selective_numbers, _ = _build_fast_path_numbers(
            analysis,
            source_issue=source["issue"],
            target_issue=target["issue"],
            previous_numbers=previous_selective,
            trace=[],
            adaptive_number_scores=selective_scores,
        )
        isolated_numbers = {}
        if conditional:
            for isolated_model in MODELS:
                isolated_weights = {"strategy": "v7_models", **{key: 1.0 for key in KEYS.values()}}
                isolated_key = KEYS.get(isolated_model)
                if isolated_key:
                    isolated_weights[isolated_key] = float(conditional.get(isolated_key, 1.0))
                isolated_scores, _ = _adaptive_scores(models, isolated_weights)
                isolated_numbers[isolated_model], _ = _build_fast_path_numbers(
                    analysis,
                    source_issue=source["issue"],
                    target_issue=target["issue"],
                    previous_numbers=previous_isolated[isolated_model],
                    trace=[],
                    adaptive_number_scores=isolated_scores,
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
        neutral, neutral_diversity = _build_fast_path_numbers(
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
        model_candidates = {str(model.get("model") or ""): set(_numbers({"numbers": model.get("candidate_numbers") or []})) for model in models}
        added_numbers = set(conditional_numbers) - set(neutral)
        removed_numbers = set(neutral) - set(conditional_numbers)
        model_swap_attribution = {}
        for model in MODELS:
            candidates = model_candidates.get(model) or set()
            model_swap_attribution[model] = {
                "added_supported": len(added_numbers & candidates),
                "added_supported_hits": len(added_numbers & candidates & set(official)),
                "removed_supported": len(removed_numbers & candidates),
                "removed_supported_hits": len(removed_numbers & candidates & set(official)),
            }
        rows.append({
            "issue": target["issue"],
            "adaptive_enabled": adaptive is not None,
            "regime": regime,
            "conditional_enabled": conditional is not None,
            "conditional20": _hits(conditional_numbers, official),
            "constraint_shadow_hits": {name: _hits(numbers, official) for name, numbers in shadow_numbers.items()},
            "source_shadow_hits": {name: _hits(numbers, official) for name, numbers in source_shadow_numbers.items()},
            "source_shadow_changed": {name: len(set(numbers) ^ set(conditional_numbers)) // 2 for name, numbers in source_shadow_numbers.items()},
            "constraint_shadow_changed": {name: len(set(numbers) ^ set(conditional_numbers)) // 2 for name, numbers in shadow_numbers.items()},
            "selective20": _hits(selective_numbers, official),
            "selective_enabled": selective_enabled,
            "normal_hotcold_gate20": _hits(gated_conditional_numbers, official),
            "normal_hotcold_gate_suppressed": gated_hotcold_suppressed,
            "conditional_multipliers": conditional_multipliers,
            "conditional_changed_numbers": len(set(conditional_numbers) ^ set(neutral)) // 2,
            "raw_rank_changed_numbers": raw_rank_changed,
            "full_score_rank_changed_numbers": len(set(conditional_diversity.get("top_ranked", [])[:20]) ^ set(neutral_diversity.get("top_ranked", [])[:20])) // 2,
            "conditional_same_set": set(conditional_numbers) == set(neutral),
            "conditional_multiplier_spread": (max(conditional_multipliers.values()) - min(conditional_multipliers.values())) if conditional_multipliers else 0.0,
            "conditional_added_hits": len((set(conditional_numbers) - set(neutral)) & set(official)),
            "conditional_removed_hits": len((set(neutral) - set(conditional_numbers)) & set(official)),
            "model_swap_attribution": model_swap_attribution,
            "isolated_model_hits": {model: _hits(numbers, official) for model, numbers in isolated_numbers.items()},
            "isolated_model_sets": {model: numbers for model, numbers in isolated_numbers.items()},
            "signal_cluster": signal_cluster,
            "signal_pattern": signal_pattern,
            "cluster_cut": cluster_cut,
            "pattern_cut": pattern_cut,
            "learned_by_strength": {str(strength): hits for strength, hits in learned_hits.items()},
            "neutral20": neutral_hits,
            "neutral_numbers": neutral,
            "official_numbers": official,
            "off20": off_hits,
            "multipliers_by_strength": {str(strength): values for strength, values in multipliers_by_strength.items()},
        })
        for strength, numbers in learned_by_strength.items():
            previous_by_strength[strength] = numbers
        previous_conditional = conditional_numbers
        previous_normal_hotcold_gate = gated_conditional_numbers
        for shadow_name, numbers in shadow_numbers.items():
            previous_shadow[shadow_name] = numbers
        for source_name, numbers in source_shadow_numbers.items():
            previous_source_shadow[source_name] = numbers
        previous_selective = selective_numbers
        if conditional:
            prior_regime_deltas[regime].append(_hits(conditional_numbers, official) - neutral_hits)
        for model, numbers in isolated_numbers.items():
            previous_isolated[model] = numbers
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
        swap_models = {}
        for model in MODELS:
            swap_models[model] = {
                "added_supported": sum(row.get("model_swap_attribution", {}).get(model, {}).get("added_supported", 0) for row in group),
                "added_supported_hits": sum(row.get("model_swap_attribution", {}).get(model, {}).get("added_supported_hits", 0) for row in group),
                "removed_supported": sum(row.get("model_swap_attribution", {}).get(model, {}).get("removed_supported", 0) for row in group),
                "removed_supported_hits": sum(row.get("model_swap_attribution", {}).get(model, {}).get("removed_supported_hits", 0) for row in group),
            }
        isolated_model_summary = {}
        for model in MODELS:
            isolated_deltas = [row.get("isolated_model_hits", {}).get(model, row["neutral20"]) - row["neutral20"] for row in group]
            isolated_model_summary[model] = {
                "mean20": mean(row.get("isolated_model_hits", {}).get(model, row["neutral20"]) for row in group),
                "vs_neutral": _ci(isolated_deltas),
                "wins": sum(delta > 0 for delta in isolated_deltas),
                "ties": sum(delta == 0 for delta in isolated_deltas),
                "losses": sum(delta < 0 for delta in isolated_deltas),
            }
        time_segments = {}
        ordered_group = sorted(group, key=lambda row: int(row["issue"]))
        for segment_index, segment_name in enumerate(("early", "middle", "late")):
            start = (len(ordered_group) * segment_index) // 3
            end = (len(ordered_group) * (segment_index + 1)) // 3
            segment = ordered_group[start:end]
            segment_models = {}
            for model in MODELS:
                segment_deltas = [row.get("isolated_model_hits", {}).get(model, row["neutral20"]) - row["neutral20"] for row in segment]
                segment_models[model] = {
                    "issues": len(segment),
                    "mean_delta": mean(segment_deltas) if segment_deltas else 0,
                    "vs_neutral": _ci(segment_deltas),
                    "wins": sum(delta > 0 for delta in segment_deltas),
                    "ties": sum(delta == 0 for delta in segment_deltas),
                    "losses": sum(delta < 0 for delta in segment_deltas),
                }
            time_segments[segment_name] = segment_models
        event_diagnostics = {}
        for model in MODELS:
            events = []
            for row in ordered_group:
                model_hits = row.get("isolated_model_hits", {}).get(model, row["neutral20"])
                if model_hits == row["neutral20"]:
                    continue
                isolated_set = set(row.get("isolated_model_sets", {}).get(model, []))
                neutral_set = set(row.get("neutral_numbers", []))
                official_set = set(row.get("official_numbers", []))
                events.append({
                    "issue": row["issue"],
                    "delta": model_hits - row["neutral20"],
                    "added": sorted(isolated_set - neutral_set),
                    "removed": sorted(neutral_set - isolated_set),
                    "added_hits": sorted((isolated_set - neutral_set) & official_set),
                    "removed_hits": sorted((neutral_set - isolated_set) & official_set),
                    "multiplier": row.get("conditional_multipliers", {}).get(model),
                    "cluster_score": row.get("signal_cluster"),
                    "pattern_score": row.get("signal_pattern"),
                    "cluster_excess": row.get("signal_cluster", 0) - row.get("cluster_cut", 0),
                    "pattern_excess": row.get("signal_pattern", 0) - row.get("pattern_cut", 0),
                })
            event_diagnostics[model] = events
        hotcold_multiplier_buckets = {}
        hotcold_events = event_diagnostics.get("hotcold", [])
        for bucket_name, low, high in (("lt_0_98", -999.0, 0.98), ("0_98_to_1_00", 0.98, 1.0), ("1_00_to_1_02", 1.0, 1.02), ("ge_1_02", 1.02, 999.0)):
            bucket_events = [event for event in hotcold_events if event.get("multiplier") is not None and low <= float(event["multiplier"]) < high]
            bucket_deltas = [event["delta"] for event in bucket_events]
            hotcold_multiplier_buckets[bucket_name] = {
                "events": len(bucket_events),
                "mean_delta": mean(bucket_deltas) if bucket_deltas else 0,
                "vs_neutral": _ci(bucket_deltas),
                "wins": sum(delta > 0 for delta in bucket_deltas),
                "ties": sum(delta == 0 for delta in bucket_deltas),
                "losses": sum(delta < 0 for delta in bucket_deltas),
                "mean_replacements": mean(len(event.get("added") or []) for event in bucket_events) if bucket_events else 0,
            }
        hotcold_deviation_buckets = {}
        ordered_hotcold_events = sorted(hotcold_events, key=lambda event: int(event["issue"]))
        for bucket_name, low, high in (("lt_1pct", 0.0, 0.01), ("1_to_2pct", 0.01, 0.02), ("2_to_4pct", 0.02, 0.04), ("ge_4pct", 0.04, 999.0)):
            bucket_events = [event for event in ordered_hotcold_events if event.get("multiplier") is not None and low <= abs(float(event["multiplier"]) - 1.0) < high]
            bucket_deltas = [event["delta"] for event in bucket_events]
            midpoint = len(bucket_events) // 2
            early_deltas = [event["delta"] for event in bucket_events[:midpoint]]
            late_deltas = [event["delta"] for event in bucket_events[midpoint:]]
            hotcold_deviation_buckets[bucket_name] = {
                "events": len(bucket_events),
                "mean_delta": mean(bucket_deltas) if bucket_deltas else 0,
                "vs_neutral": _ci(bucket_deltas),
                "wins": sum(delta > 0 for delta in bucket_deltas),
                "ties": sum(delta == 0 for delta in bucket_deltas),
                "losses": sum(delta < 0 for delta in bucket_deltas),
                "early": {"events": len(early_deltas), "mean_delta": mean(early_deltas) if early_deltas else 0, "vs_neutral": _ci(early_deltas)},
                "late": {"events": len(late_deltas), "mean_delta": mean(late_deltas) if late_deltas else 0, "vs_neutral": _ci(late_deltas)},
            }
        regime_summary[regime] = {
            "hotcold_multiplier_buckets": hotcold_multiplier_buckets,
            "hotcold_deviation_buckets": hotcold_deviation_buckets,
            "issues": len(group),
            "event_diagnostics": event_diagnostics,
            "isolated_models": isolated_model_summary,
            "time_segments": time_segments,
            "swap_model_attribution": swap_models,
            "same_set_issues": sum(bool(row.get("conditional_same_set")) for row in group),
            "mean_changed_numbers": mean(row.get("conditional_changed_numbers", 0) for row in group),
            "mean_multiplier_spread": mean(row.get("conditional_multiplier_spread", 0.0) for row in group),
            "changed_only": {
                "issues": sum(not bool(row.get("conditional_same_set")) for row in group),
                "added_hits": sum(row.get("conditional_added_hits", 0) for row in group if not row.get("conditional_same_set")),
                "removed_hits": sum(row.get("conditional_removed_hits", 0) for row in group if not row.get("conditional_same_set")),
                "net_hits": sum(row.get("conditional_added_hits", 0) - row.get("conditional_removed_hits", 0) for row in group if not row.get("conditional_same_set")),
            },
            "conditional20": mean(row["conditional20"] for row in group),
            "neutral20": mean(row["neutral20"] for row in group),
            "vs_neutral": _ci(deltas),
            "models": model_performance,
        }
    source_weight_ablations = {}
    for source_name in ("patch_numbers", "missing_numbers", "cold_numbers", "hot_numbers", "diagonal_pattern", "repeated_numbers", "latest_draw_numbers"):
        deltas = [row.get("source_shadow_hits", {}).get(source_name, row["conditional20"]) - row["conditional20"] for row in conditional_active]
        changes = [row.get("source_shadow_changed", {}).get(source_name, 0) for row in conditional_active]
        source_weight_ablations[source_name] = {
            "changed_issues": sum(value > 0 for value in changes),
            "mean_changed_numbers": mean(changes) if changes else 0,
            "vs_production_weights": _ci(deltas),
            "wins": sum(value > 0 for value in deltas),
            "ties": sum(value == 0 for value in deltas),
            "losses": sum(value < 0 for value in deltas),
        }
        constraint_shadows = {}
    for shadow_name in ("zone", "tail", "previous"):
        shadow_deltas = [row.get("constraint_shadow_hits", {}).get(shadow_name, row["conditional20"]) - row["conditional20"] for row in conditional_active]
        shadow_changes = [row.get("constraint_shadow_changed", {}).get(shadow_name, 0) for row in conditional_active]
        constraint_shadows[shadow_name] = {
            "changed_issues": sum(value > 0 for value in shadow_changes),
            "mean_changed_numbers": mean(shadow_changes) if shadow_changes else 0,
            "vs_production_constraints": _ci(shadow_deltas),
            "wins": sum(value > 0 for value in shadow_deltas),
            "ties": sum(value == 0 for value in shadow_deltas),
            "losses": sum(value < 0 for value in shadow_deltas),
        }
        full_score_changed = [row.get("full_score_rank_changed_numbers", 0) for row in conditional_active]
    raw_changed = [row.get("raw_rank_changed_numbers", 0) for row in conditional_active]
    final_changed = [row.get("conditional_changed_numbers", 0) for row in conditional_active]
    dilution_summary = {
        "issues": len(conditional_active),
        "raw_rank_changed_issues": sum(value > 0 for value in raw_changed),
        "full_score_rank_changed_issues": sum(value > 0 for value in full_score_changed),
        "mean_full_score_rank_changes": mean(full_score_changed) if full_score_changed else 0,
        "full_score_absorbed_issues": sum(raw > 0 and final == 0 for raw, final in zip(full_score_changed, final_changed)),
        "final_changed_issues": sum(value > 0 for value in final_changed),
        "mean_raw_rank_changes": mean(raw_changed) if raw_changed else 0,
        "mean_final_changes": mean(final_changed) if final_changed else 0,
        "fully_absorbed_issues": sum(raw > 0 and final == 0 for raw, final in zip(raw_changed, final_changed)),
    }
    selective_vs_neutral = [row["selective20"] - row["neutral20"] for row in conditional_active]
    selective_vs_conditional = [row["selective20"] - row["conditional20"] for row in conditional_active]
    selective_enabled_rows = [row for row in conditional_active if row.get("selective_enabled")]
    gated_deltas_vs_conditional = [row["normal_hotcold_gate20"] - row["conditional20"] for row in conditional_active]
    gated_deltas_vs_neutral = [row["normal_hotcold_gate20"] - row["neutral20"] for row in conditional_active]
    gated_triggered = [row for row in conditional_active if row.get("normal_hotcold_gate_suppressed")]
    gated_triggered_vs_conditional = [row["normal_hotcold_gate20"] - row["conditional20"] for row in gated_triggered]
    gated_triggered_vs_neutral = [row["normal_hotcold_gate20"] - row["neutral20"] for row in gated_triggered]
    return {
        "summary": {
            "issues": len(rows),
            "adaptive_active_issues": len(active),
            "neutral20": mean(row["neutral20"] for row in active) if active else 0,
            "off20": mean(row["off20"] for row in active) if active else 0,
            "strengths": strength_summary,
            "paired_neutral_minus_off_20": _ci(neutral_off),
            "adaptive_rank_dilution": dilution_summary,
            "constraint_shadow_arms": constraint_shadows,
            "source_weight_ablations": source_weight_ablations,
            "selective_confidence_gate": {
                "enabled_issues": len(selective_enabled_rows),
                "fallback_issues": len(conditional_active) - len(selective_enabled_rows),
                "mean20": mean(row["selective20"] for row in conditional_active) if conditional_active else 0,
                "vs_neutral": _ci(selective_vs_neutral),
                "vs_conditional": _ci(selective_vs_conditional),
                "wins_vs_neutral": sum(delta > 0 for delta in selective_vs_neutral),
                "ties_vs_neutral": sum(delta == 0 for delta in selective_vs_neutral),
                "losses_vs_neutral": sum(delta < 0 for delta in selective_vs_neutral),
            },
            "normal_hotcold_ge4_gate": {
                "triggered_issues": len(gated_triggered),
                "mean20": mean(row["normal_hotcold_gate20"] for row in conditional_active) if conditional_active else 0,
                "vs_conditional": _ci(gated_deltas_vs_conditional),
                "vs_neutral": _ci(gated_deltas_vs_neutral),
                "triggered_vs_conditional": _ci(gated_triggered_vs_conditional),
                "triggered_vs_neutral": _ci(gated_triggered_vs_neutral),
                "triggered_wins_vs_conditional": sum(delta > 0 for delta in gated_triggered_vs_conditional),
                "triggered_ties_vs_conditional": sum(delta == 0 for delta in gated_triggered_vs_conditional),
                "triggered_losses_vs_conditional": sum(delta < 0 for delta in gated_triggered_vs_conditional),
            },
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
                "changed_only": {
                    "issues": sum(not bool(row.get("conditional_same_set")) for row in conditional_active),
                    "added_hits": sum(row.get("conditional_added_hits", 0) for row in conditional_active if not row.get("conditional_same_set")),
                    "removed_hits": sum(row.get("conditional_removed_hits", 0) for row in conditional_active if not row.get("conditional_same_set")),
                    "net_hits": sum(row.get("conditional_added_hits", 0) - row.get("conditional_removed_hits", 0) for row in conditional_active if not row.get("conditional_same_set")),
                },
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
