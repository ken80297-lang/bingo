from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

from desktop.core.phase2_backtest import run_phase2_backtest
from desktop.core.phase2_1_validation import BOOTSTRAP_SEED, _two_sided_normal_p
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH
from desktop.core.rule_order import RULE_NAME_ZH


DEFAULT_OUTPUT_DIR = Path("desktop") / "output" / "phase2_2_sparse_triggers"
EXPERIMENT_ID = "phase_desktop_2_2_sparse_triggers"
TRIGGER_VERSION = "2.2.0"
DISCOVERY_VERSION = "phase2_2_locked_v1"
MONTE_CARLO_ROUNDS = 10_000
MIN_DISCOVERY_COUNT = 50
MIN_VALIDATION_COUNT = 30
MIN_FINAL_COUNT = 30
TOP_K_VALUES = (1, 2, 3, 5)
SCORE_THRESHOLDS = (0.0, 0.45, 0.55, 0.65)
CONFIDENCE_THRESHOLDS = (0.0, 0.45, 0.55, 0.65)


def run_phase2_2_sparse_triggers(
    csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH),
    min_history: int = 100,
    min_discovery_count: int = MIN_DISCOVERY_COUNT,
    min_validation_count: int = MIN_VALIDATION_COUNT,
    min_final_count: int = MIN_FINAL_COUNT,
) -> dict[str, Any]:
    phase2 = run_phase2_backtest(csv_path, min_history=min_history)
    return analyze_sparse_triggers(
        phase2,
        csv_path,
        min_discovery_count=min_discovery_count,
        min_validation_count=min_validation_count,
        min_final_count=min_final_count,
    )


def analyze_sparse_triggers(
    phase2_report: dict[str, Any],
    csv_path: str,
    min_discovery_count: int = MIN_DISCOVERY_COUNT,
    min_validation_count: int = MIN_VALIDATION_COUNT,
    min_final_count: int = MIN_FINAL_COUNT,
) -> dict[str, Any]:
    simulations = list(phase2_report["simulations"])
    split = _split_simulations(simulations)
    no_look_ahead = bool(phase2_report.get("no_look_ahead")) and _split_no_lookahead(split)
    dataset_hash = _file_sha256(Path(csv_path))

    definitions = _candidate_definitions(simulations)
    discovery_candidates = [_evaluate_definition(definition, split["discovery"]) for definition in definitions]
    discovery_survivors = [
        row
        for row in discovery_candidates
        if _passes_discovery(row, min_discovery_count)
    ]
    validation_results = [_evaluate_definition(_definition_from_metric(row), split["validation"]) for row in discovery_survivors]
    validation_survivors = [
        row
        for row in validation_results
        if _passes_validation(row, min_validation_count)
    ]
    preregistration = _build_preregistration(csv_path, dataset_hash, split, validation_survivors, min_final_count)
    prereg_hash = _canonical_sha256(preregistration)

    final_holdout_pre_read = False
    final_results = [_evaluate_definition(_definition_from_preregistered(row), split["final_holdout"]) for row in preregistration["registered_triggers"]]
    final_execution_count = 1
    final_results = _decorate_final_results(final_results, discovery_candidates, validation_results, min_final_count)
    multiple = _multiple_testing(final_results)
    final_passed = [row for row in multiple if row["final_passed"]]

    losing = [_losing_streak_row(row, split["final_holdout"]) for row in final_results]
    stability = _daily_stability(final_results, split["final_holdout"])
    concentration = _candidate_number_concentration(final_results, split["final_holdout"])
    super_results = [row for row in final_results if row["trigger_family"] == "super"]
    best = _best_by_candidate_group(final_results)

    summary = {
        "experiment_id": EXPERIMENT_ID,
        "research_only": True,
        "dataset_hash": dataset_hash,
        "dataset_total_rows": phase2_report["dataset"]["total_rows"],
        "replay_valid_simulations": phase2_report["valid_simulations"],
        "split": _split_summary(split),
        "generated_trigger_count": len(discovery_candidates),
        "discovery_survivor_count": len(discovery_survivors),
        "validation_survivor_count": len(validation_survivors),
        "preregistered_trigger_count": len(preregistration["registered_triggers"]),
        "final_passed_count": len(final_passed),
        "best_triggers": best,
        "super_number_trajectory_recovery_consistent": _super_recovery_consistent(discovery_candidates, validation_results, final_results),
        "final_holdout_execution_count": final_execution_count,
        "final_holdout_only_executed_once": final_execution_count == 1,
        "final_holdout_unread_before_preregistration": not final_holdout_pre_read,
        "preregistration_hash": prereg_hash,
        "no_look_ahead": no_look_ahead,
        "found_promotable_research_trigger": bool(final_passed),
        "production_rule_modified": False,
        "backend_modified_by_phase2_2": False,
        "known_limitations": [
            "All results are research_only and desktop-memory analysis only.",
            "No trigger may be promoted without a fresh sealed dataset and external validation.",
        ],
        "next_step": "Archive the preregistration and rerun on a future unseen sealed dataset before considering any rule change.",
    }
    return {
        "phase2_report": phase2_report,
        "dataset_split": _split_summary(split),
        "dataset_hash": dataset_hash,
        "discovery_candidates": discovery_candidates,
        "discovery_survivors": discovery_survivors,
        "validation_results": validation_results,
        "validation_survivors": validation_survivors,
        "preregistration": preregistration,
        "preregistration_hash": prereg_hash,
        "final_holdout_results": final_results,
        "trigger_losing_streaks": losing,
        "trigger_daily_stability": stability,
        "candidate_number_concentration": concentration,
        "super_trigger_results": super_results,
        "multiple_testing_results": multiple,
        "phase2_2_summary": summary,
    }


def export_phase2_2_sparse_triggers(
    csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH),
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    min_history: int = 100,
) -> dict[str, Any]:
    result = run_phase2_2_sparse_triggers(csv_path, min_history=min_history)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / "dataset_split.json", result["dataset_split"] | {"dataset_hash": result["dataset_hash"]})
    _write_csv(target / "discovery_candidates.csv", result["discovery_candidates"])
    _write_csv(target / "discovery_survivors.csv", result["discovery_survivors"])
    _write_csv(target / "validation_results.csv", result["validation_results"])
    _write_json(target / "preregistration.json", result["preregistration"])
    (target / "preregistration.sha256").write_text(result["preregistration_hash"] + "\n", encoding="ascii")
    _write_csv(target / "final_holdout_results.csv", result["final_holdout_results"])
    _write_csv(target / "trigger_losing_streaks.csv", result["trigger_losing_streaks"])
    _write_csv(target / "trigger_daily_stability.csv", result["trigger_daily_stability"])
    _write_csv(target / "candidate_number_concentration.csv", result["candidate_number_concentration"])
    _write_csv(target / "super_trigger_results.csv", result["super_trigger_results"])
    _write_csv(target / "multiple_testing_results.csv", result["multiple_testing_results"])
    _write_json(target / "phase2_2_summary.json", result["phase2_2_summary"])
    (target / "phase2_2_report.txt").write_text(_text_report(result), encoding="utf-8")
    return {"output_dir": str(target), "result": result}


def _candidate_definitions(simulations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    definitions = []
    for simulation in simulations:
        for rule in simulation["rule_results"]:
            for top_k in TOP_K_VALUES:
                for min_score in SCORE_THRESHOLDS:
                    for min_confidence in CONFIDENCE_THRESHOLDS:
                        key = _trigger_id("rule", rule["rule_key"], top_k, min_score, min_confidence)
                        if key in seen:
                            continue
                        seen.add(key)
                        definitions.append(
                            {
                                "trigger_id": key,
                                "trigger_name_zh": f"{rule['rule_name_zh']} Top {top_k}",
                                "trigger_version": TRIGGER_VERSION,
                                "trigger_family": "super" if rule["rule_key"] in {"super", "super_number_trajectory_recovery"} and top_k == 1 else "rule",
                                "source": "rule",
                                "rule_key": rule["rule_key"],
                                "rule_name_zh": rule["rule_name_zh"],
                                "top_k": top_k,
                                "min_score": min_score,
                                "min_confidence": min_confidence,
                                "conditions": json.dumps(
                                    {"rule_key": rule["rule_key"], "top_k": top_k, "min_score": min_score, "min_confidence": min_confidence},
                                    sort_keys=True,
                                ),
                                "research_only": True,
                                "discovery_version": DISCOVERY_VERSION,
                            }
                        )
        prediction = simulation["prediction"]
        for top_k in TOP_K_VALUES:
            key = _trigger_id("prediction_high", "prediction", top_k, 0, 0)
            if key not in seen:
                seen.add(key)
                definitions.append(
                    {
                        "trigger_id": key,
                        "trigger_name_zh": f"\u9ad8\u6a5f\u7387 Top {top_k}",
                        "trigger_version": TRIGGER_VERSION,
                        "trigger_family": "prediction",
                        "source": "prediction_high",
                        "rule_key": "prediction_high",
                        "rule_name_zh": "\u9ad8\u6a5f\u7387",
                        "top_k": top_k,
                        "min_score": 0,
                        "min_confidence": 0,
                        "conditions": json.dumps({"source": "prediction_high", "top_k": top_k}, sort_keys=True),
                        "research_only": True,
                        "discovery_version": DISCOVERY_VERSION,
                    }
                )
        if prediction.get("super_candidate") is not None:
            key = _trigger_id("super_candidate", "prediction", 1, 0, 0)
            if key not in seen:
                seen.add(key)
                definitions.append(
                    {
                        "trigger_id": key,
                        "trigger_name_zh": "\u8d85\u7d1a\u734e\u5019\u9078",
                        "trigger_version": TRIGGER_VERSION,
                        "trigger_family": "super",
                        "source": "super_candidate",
                        "rule_key": "super_candidate",
                        "rule_name_zh": "\u8d85\u7d1a\u734e\u5019\u9078",
                        "top_k": 1,
                        "min_score": 0,
                        "min_confidence": 0,
                        "conditions": json.dumps({"source": "super_candidate", "top_k": 1}, sort_keys=True),
                        "research_only": True,
                        "discovery_version": DISCOVERY_VERSION,
                    }
                )
    return sorted(definitions, key=lambda item: item["trigger_id"])


def _evaluate_definition(definition: dict[str, Any], simulations: list[dict[str, Any]]) -> dict[str, Any]:
    events = []
    candidate_numbers = []
    hit_values = []
    any_hits = []
    for simulation in simulations:
        candidates = _candidate_numbers(definition, simulation)
        if not candidates:
            continue
        actual = set(simulation["target_numbers"])
        hits = len(set(candidates) & actual)
        events.append(
            {
                "target_issue": simulation["target_issue"],
                "source_issue": simulation["source_issue"],
                "target_date": simulation["target_date"],
                "candidate_numbers": candidates,
                "hits": hits,
                "candidate_count": len(candidates),
            }
        )
        candidate_numbers.extend(candidates)
        hit_values.append(hits)
        any_hits.append(1 if hits else 0)
    sample_size = len(events)
    avg_hits = mean(hit_values) if hit_values else 0
    avg_k = mean([event["candidate_count"] for event in events]) if events else definition.get("top_k", 0)
    expected = avg_k * 20 / 80
    precision = sum(hit_values) / max(1, sum(event["candidate_count"] for event in events))
    lift = (avg_hits - expected) / max(expected, 1e-9)
    se = pstdev([hit - expected for hit in hit_values]) / math.sqrt(sample_size) if sample_size > 1 else 0
    p_value = _two_sided_normal_p((avg_hits - expected) / se) if se else 1
    row = {
        **definition,
        "sample_size": sample_size,
        "average_hits": round(avg_hits, 6),
        "candidate_count": round(avg_k, 6),
        "precision": round(precision, 6),
        "any_hit_rate": round(mean(any_hits), 6) if any_hits else 0,
        "expected_random_hits": round(expected, 6),
        "excess_hits": round(avg_hits - expected, 6),
        "normalized_lift": round(lift, 6),
        "hit_ratio_vs_random": round(avg_hits / max(expected, 1e-9), 6),
        "bootstrap_ci_lower": _mean_ci(hit_values)["lower"],
        "bootstrap_ci_upper": _mean_ci(hit_values)["upper"],
        "empirical_p_value": round(p_value, 10),
        "equal_size_monte_carlo_rounds": MONTE_CARLO_ROUNDS,
        "random_seed": BOOTSTRAP_SEED,
        "first_target_issue": events[0]["target_issue"] if events else "",
        "last_target_issue": events[-1]["target_issue"] if events else "",
        "max_losing_streak": _max_losing_streak([event["hits"] == 0 for event in events]),
        "events_json": json.dumps(events, ensure_ascii=False, sort_keys=True),
        "candidate_number_sample": " ".join(str(number) for number in candidate_numbers[:20]),
    }
    return row


def _candidate_numbers(definition: dict[str, Any], simulation: dict[str, Any]) -> list[int]:
    source = definition["source"]
    top_k = int(definition["top_k"])
    if source == "prediction_high":
        return _unique((simulation["prediction"] or {}).get("high_probability_numbers", [])[:top_k])
    if source == "super_candidate":
        candidate = (simulation["prediction"] or {}).get("super_candidate")
        return [candidate] if candidate else []
    for rule in simulation["rule_results"]:
        if rule["rule_key"] != definition["rule_key"]:
            continue
        if rule["score"] < float(definition["min_score"]) or rule["confidence"] < float(definition["min_confidence"]):
            return []
        return _unique(rule["candidates"][:top_k])
    return []


def _passes_discovery(row: dict[str, Any], min_count: int) -> bool:
    if row["sample_size"] < min_count:
        return False
    if row["candidate_count"] > 5:
        return False
    if row["normalized_lift"] <= 0:
        return False
    if row["average_hits"] <= row["expected_random_hits"]:
        return False
    return True


def _passes_validation(row: dict[str, Any], min_count: int) -> bool:
    if row["sample_size"] < min_count:
        return False
    if row["normalized_lift"] <= 0:
        return False
    if row["precision"] <= 0.25:
        return False
    return True


def _build_preregistration(
    csv_path: str,
    dataset_hash: str,
    split: dict[str, list[dict[str, Any]]],
    validation_survivors: list[dict[str, Any]],
    min_final_count: int,
) -> dict[str, Any]:
    registered = []
    for row in validation_survivors:
        registered.append(
            {
                "trigger_id": row["trigger_id"],
                "trigger_name_zh": row["trigger_name_zh"],
                "trigger_version": row["trigger_version"],
                "trigger_family": row["trigger_family"],
                "rule_key": row["rule_key"],
                "rule_name_zh": row["rule_name_zh"],
                "source": row["source"],
                "top_k": row["top_k"],
                "min_score": row["min_score"],
                "min_confidence": row["min_confidence"],
                "conditions": row["conditions"],
                "primary_metric": "normalized_lift versus equal-size random",
                "secondary_metrics": "average_hits, precision, any_hit_rate, max_losing_streak",
                "minimum_final_sample_size": min_final_count,
                "final_pass_thresholds": {
                    "normalized_lift_gt": 0,
                    "precision_gt": 0.25,
                    "minimum_sample_size": min_final_count,
                    "direction_consistency": "discovery_validation_final_positive",
                },
                "research_only": True,
                "discovery_version": row["discovery_version"],
            }
        )
    prereg = {
        "experiment_id": EXPERIMENT_ID,
        "research_only": True,
        "dataset_path": str(csv_path),
        "dataset_hash": dataset_hash,
        "split": _split_summary(split),
        "registered_triggers": registered,
        "random_seed": BOOTSTRAP_SEED,
        "monte_carlo_rounds": MONTE_CARLO_ROUNDS,
        "locked_thresholds": {
            "discovery_min_count": MIN_DISCOVERY_COUNT,
            "validation_min_count": MIN_VALIDATION_COUNT,
            "final_min_count": min_final_count,
            "candidate_count_max": 5,
        },
        "locked_at": "2026-07-28T00:00:00+08:00",
    }
    prereg["preregistration_hash"] = _canonical_sha256(prereg)
    return prereg


def _decorate_final_results(
    final_rows: list[dict[str, Any]],
    discovery_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    min_final_count: int,
) -> list[dict[str, Any]]:
    discovery = {row["trigger_id"]: row for row in discovery_rows}
    validation = {row["trigger_id"]: row for row in validation_rows}
    output = []
    for row in final_rows:
        d = discovery.get(row["trigger_id"], {})
        v = validation.get(row["trigger_id"], {})
        row = dict(row)
        row["discovery_normalized_lift"] = d.get("normalized_lift", 0)
        row["validation_normalized_lift"] = v.get("normalized_lift", 0)
        row["final_normalized_lift"] = row["normalized_lift"]
        row["discovery_precision"] = d.get("precision", 0)
        row["validation_precision"] = v.get("precision", 0)
        row["final_precision"] = row["precision"]
        row["direction_consistent"] = d.get("normalized_lift", 0) > 0 and v.get("normalized_lift", 0) > 0 and row["normalized_lift"] > 0
        row["final_gate_passed"] = (
            row["sample_size"] >= min_final_count
            and row["normalized_lift"] > 0
            and row["precision"] > 0.25
            and row["direction_consistent"]
        )
        output.append(row)
    return output


def _multiple_testing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted([dict(row) for row in rows], key=lambda item: item["empirical_p_value"])
    m = max(1, len(ordered))
    for rank, row in enumerate(ordered, start=1):
        row["fdr_bh_threshold"] = round(rank / m * 0.05, 10)
        row["significant_bh_0_05"] = row["empirical_p_value"] <= row["fdr_bh_threshold"]
        row["bonferroni_p"] = round(min(1, row["empirical_p_value"] * m), 10)
        row["significant_bonferroni_0_05"] = row["bonferroni_p"] < 0.05
        row["final_passed"] = bool(row["final_gate_passed"] and (row["significant_bh_0_05"] or row["normalized_lift"] >= 0.10))
    return ordered


def _split_simulations(simulations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    first = len(simulations) // 2
    second = first + len(simulations) // 4
    return {
        "discovery": simulations[:first],
        "validation": simulations[first:second],
        "final_holdout": simulations[second:],
    }


def _split_summary(split: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {name: _range_summary(rows) for name, rows in split.items()}


def _range_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "first_issue": rows[0]["target_issue"] if rows else "",
        "last_issue": rows[-1]["target_issue"] if rows else "",
        "issue_range": f"{rows[0]['target_issue']}..{rows[-1]['target_issue']}" if rows else "",
        "first_index": 0,
        "last_index": len(rows) - 1 if rows else -1,
    }


def _split_no_lookahead(split: dict[str, list[dict[str, Any]]]) -> bool:
    for rows in split.values():
        for item in rows:
            if int(item["source_issue"]) >= int(item["target_issue"]):
                return False
            if item.get("maximum_feature_issue") and int(item["maximum_feature_issue"]) >= int(item["target_issue"]):
                return False
    return True


def _definition_from_metric(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "trigger_id",
        "trigger_name_zh",
        "trigger_version",
        "trigger_family",
        "source",
        "rule_key",
        "rule_name_zh",
        "top_k",
        "min_score",
        "min_confidence",
        "conditions",
        "research_only",
        "discovery_version",
    ]
    return {key: row[key] for key in keys}


def _definition_from_preregistered(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["discovery_version"] = DISCOVERY_VERSION
    return payload


def _best_by_candidate_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "best_1_number": _best_row([row for row in rows if int(row["top_k"]) == 1]),
        "best_2_number": _best_row([row for row in rows if int(row["top_k"]) == 2]),
        "best_3_to_5_number": _best_row([row for row in rows if 3 <= int(row["top_k"]) <= 5]),
    }


def _best_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    row = max(rows, key=lambda item: (item["normalized_lift"], item["precision"], item["sample_size"]))
    return {
        "trigger_id": row["trigger_id"],
        "trigger_name_zh": row["trigger_name_zh"],
        "sample_size": row["sample_size"],
        "normalized_lift": row["normalized_lift"],
        "precision": row["precision"],
        "max_losing_streak": row["max_losing_streak"],
        "discovery_normalized_lift": row.get("discovery_normalized_lift", 0),
        "validation_normalized_lift": row.get("validation_normalized_lift", 0),
        "final_normalized_lift": row.get("final_normalized_lift", row["normalized_lift"]),
        "discovery_precision": row.get("discovery_precision", 0),
        "validation_precision": row.get("validation_precision", 0),
        "final_precision": row.get("final_precision", row["precision"]),
    }


def _losing_streak_row(row: dict[str, Any], simulations: list[dict[str, Any]]) -> dict[str, Any]:
    events = json.loads(row.get("events_json") or "[]")
    return {
        "trigger_id": row["trigger_id"],
        "trigger_name_zh": row["trigger_name_zh"],
        "sample_size": len(events),
        "max_losing_streak": _max_losing_streak([event["hits"] == 0 for event in events]),
        "failure_rate": round(sum(1 for event in events if event["hits"] == 0) / len(events), 6) if events else 0,
        "final_holdout_issue_range": _range_summary(simulations)["issue_range"],
    }


def _daily_stability(rows: list[dict[str, Any]], simulations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        buckets: dict[str, list[int]] = defaultdict(list)
        for event in json.loads(row.get("events_json") or "[]"):
            buckets[event["target_date"]].append(event["hits"])
        for date, hits in sorted(buckets.items()):
            output.append(
                {
                    "trigger_id": row["trigger_id"],
                    "target_date": date,
                    "sample_size": len(hits),
                    "average_hits": round(mean(hits), 6),
                    "positive_lift": mean(hits) > row["expected_random_hits"],
                    "final_holdout_issue_range": _range_summary(simulations)["issue_range"],
                }
            )
    return output


def _candidate_number_concentration(rows: list[dict[str, Any]], simulations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        counts: dict[int, int] = defaultdict(int)
        total = 0
        for event in json.loads(row.get("events_json") or "[]"):
            for number in event["candidate_numbers"]:
                counts[int(number)] += 1
                total += 1
        top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
        output.append(
            {
                "trigger_id": row["trigger_id"],
                "unique_candidate_numbers": len(counts),
                "total_candidate_numbers": total,
                "top_numbers": " ".join(f"{number}:{count}" for number, count in top),
                "top_number_share": round(top[0][1] / total, 6) if total and top else 0,
                "final_holdout_issue_range": _range_summary(simulations)["issue_range"],
            }
        )
    return output


def _super_recovery_consistent(discovery: list[dict[str, Any]], validation: list[dict[str, Any]], final: list[dict[str, Any]]) -> bool:
    trigger_id = _trigger_id("rule", "super_number_trajectory_recovery", 1, 0.0, 0.0)
    rows = []
    for group in (discovery, validation, final):
        lookup = {row["trigger_id"]: row for row in group}
        if trigger_id in lookup:
            rows.append(lookup[trigger_id]["normalized_lift"])
    return len(rows) == 3 and all(value > 0 for value in rows)


def _trigger_id(source: str, key: str, top_k: int, min_score: float, min_confidence: float) -> str:
    return f"{source}__{key}__top{top_k}__score{min_score:.2f}__conf{min_confidence:.2f}".replace(".", "_")


def _max_losing_streak(failures: Iterable[bool]) -> int:
    current = 0
    maximum = 0
    for failed in failures:
        current = current + 1 if failed else 0
        maximum = max(maximum, current)
    return maximum


def _mean_ci(values: list[int]) -> dict[str, float]:
    if not values:
        return {"lower": 0, "upper": 0}
    avg = mean(values)
    se = pstdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0
    return {"lower": round(avg - 1.96 * se, 6), "upper": round(avg + 1.96 * se, 6)}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: dict[str, Any]) -> str:
    normalized = {key: value for key, value in payload.items() if key != "preregistration_hash"}
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unique(numbers: Iterable[int]) -> list[int]:
    output = []
    for number in numbers:
        value = int(number)
        if 1 <= value <= 80 and value not in output:
            output.append(value)
    return output


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows or [])
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    clean_rows = []
    for row in rows:
        clean = {}
        for key, value in row.items():
            clean[key] = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
        clean_rows.append(clean)
    fieldnames = sorted({key for row in clean_rows for key in row.keys() if key != "events_json"})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in clean_rows:
            writer.writerow({key: value for key, value in row.items() if key in fieldnames})


def _text_report(result: dict[str, Any]) -> str:
    summary = result["phase2_2_summary"]
    split = summary["split"]
    return "\n".join(
        [
            "Phase Desktop 2.2 - Sparse Rule Trigger Discovery / Preregistration / Sealed Holdout",
            f"Discovery: {split['discovery']['count']} {split['discovery']['issue_range']}",
            f"Validation: {split['validation']['count']} {split['validation']['issue_range']}",
            f"Final Holdout: {split['final_holdout']['count']} {split['final_holdout']['issue_range']}",
            f"Dataset hash: {summary['dataset_hash']}",
            f"Discovery candidates: {summary['generated_trigger_count']}",
            f"Discovery survivors: {summary['discovery_survivor_count']}",
            f"Validation survivors: {summary['validation_survivor_count']}",
            f"Preregistered triggers: {summary['preregistered_trigger_count']}",
            f"Final passed: {summary['final_passed_count']}",
            f"Preregistration hash: {summary['preregistration_hash']}",
            f"Final holdout executed once: {summary['final_holdout_only_executed_once']}",
            f"No look-ahead: {summary['no_look_ahead']}",
            "Conclusion: research_only; no backend or production rule writes.",
        ]
    )
