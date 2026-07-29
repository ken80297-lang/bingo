from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from statistics import NormalDist, mean, median, pstdev
from typing import Any, Iterable

from desktop.core.phase2_backtest import run_phase2_backtest
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH


RANDOM_20_EXPECTED = 5.0
RANDOM_HIGH5_EXPECTED = 1.25
SUPER_RANDOM_RATE = 1 / 80
BOOTSTRAP_SEED = 20260728
BOOTSTRAP_ROUNDS = 10_000
MONTE_CARLO_ROUNDS = 1_000


def run_phase2_1_validation(csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH), min_history: int = 100) -> dict[str, Any]:
    report = run_phase2_backtest(csv_path, min_history=min_history)
    simulations = report["simulations"]
    overall = _metric_significance([item["hits_20"] for item in simulations], RANDOM_20_EXPECTED)
    high5 = _metric_significance([item["hits_high5"] for item in simulations], RANDOM_HIGH5_EXPECTED)
    super_stats = _binomial_significance([bool(item["super_hit"]) for item in simulations], SUPER_RANDOM_RATE)
    big_small = _state_baselines(simulations, "big_small", "big_small_hit")
    odd_even = _state_baselines(simulations, "odd_even", "odd_even_hit")
    rule_equal = _rule_equal_size_random(report)
    enabled_disabled = _rule_enabled_vs_disabled(report)
    walk_forward = _walk_forward(simulations, initial_train=1000, validation_size=200, step=200)
    candidate_analysis = _candidate_count_analysis(report)
    multiple = _multiple_testing(rule_equal, overall, high5, super_stats)
    losing = _losing_streaks(report)
    audit = _metric_definition_audit()
    return {
        "phase2_report": report,
        "metric_definition_audit": audit,
        "overall_significance": overall,
        "high5_significance": high5,
        "super_candidate_significance": super_stats,
        "big_small_baselines": big_small,
        "odd_even_baselines": odd_even,
        "rule_equal_size_random_baseline": rule_equal,
        "rule_enabled_vs_disabled": enabled_disabled,
        "walk_forward": walk_forward,
        "rule_candidate_count_analysis": candidate_analysis,
        "multiple_testing_results": multiple,
        "losing_streak_analysis": losing,
    }


def _metric_definition_audit() -> dict[str, Any]:
    return {
        "research_only": True,
        "success_rate_redefined": True,
        "old_success_rate_problem": "candidate_any_hit_rate is nearly guaranteed for large candidate sets and must not be interpreted as predictive quality.",
        "rule_metrics": {
            "candidate_any_hit_rate": "share of target issues where the rule candidate set hit at least one actual number",
            "average_candidate_hits": "mean count of actual target numbers found in the rule candidate set",
            "expected_random_hits": "candidate_count * 20 / 80",
            "excess_hits": "actual_hit_count - expected_random_hits",
            "normalized_lift": "excess_hits / max(expected_random_hits, epsilon)",
            "hit_ratio_vs_random": "actual_hit_count / max(expected_random_hits, epsilon)",
        },
        "prediction_metrics": {
            "average_20_hits_baseline": RANDOM_20_EXPECTED,
            "average_high5_hits_baseline": RANDOM_HIGH5_EXPECTED,
            "super_candidate_baseline": SUPER_RANDOM_RATE,
        },
    }


def _metric_significance(values: list[float], expected: float) -> dict[str, Any]:
    n = len(values)
    if not values:
        return _empty_metric(expected)
    avg = mean(values)
    sd = pstdev(values) if n > 1 else 0
    se = sd / math.sqrt(n) if n else 0
    ci = _normal_ci(avg, se)
    effect = (avg - expected) / sd if sd else 0
    z = (avg - expected) / se if se else 0
    p_value = _two_sided_normal_p(z)
    bootstrap = _bootstrap_ci(values, BOOTSTRAP_ROUNDS, BOOTSTRAP_SEED)
    return {
        "sample_size": n,
        "mean": round(avg, 6),
        "median": round(median(values), 6),
        "standard_deviation": round(sd, 6),
        "standard_error": round(se, 6),
        "confidence_interval_95": ci,
        "expected_baseline": expected,
        "difference_vs_baseline": round(avg - expected, 6),
        "effect_size": round(effect, 6),
        "bootstrap_confidence_interval_95": bootstrap,
        "p_value_vs_baseline": round(p_value, 10),
        "ci_excludes_baseline": not (ci["lower"] <= expected <= ci["upper"]),
        "bootstrap_rounds": BOOTSTRAP_ROUNDS,
        "seed": BOOTSTRAP_SEED,
    }


def _binomial_significance(values: list[bool], expected_rate: float) -> dict[str, Any]:
    n = len(values)
    hits = sum(1 for value in values if value)
    rate = hits / n if n else 0
    ci = _wilson_ci(hits, n)
    se = math.sqrt(expected_rate * (1 - expected_rate) / n) if n else 0
    z = (rate - expected_rate) / se if se else 0
    return {
        "sample_size": n,
        "hit_count": hits,
        "hit_rate": round(rate, 6),
        "expected_baseline": expected_rate,
        "confidence_interval_95": ci,
        "difference_vs_baseline": round(rate - expected_rate, 6),
        "p_value_vs_baseline": round(_two_sided_normal_p(z), 10),
        "ci_excludes_baseline": not (ci["lower"] <= expected_rate <= ci["upper"]),
    }


def _state_baselines(simulations: list[dict[str, Any]], prediction_key: str, hit_key: str) -> list[dict[str, Any]]:
    actual = [_actual_state(item, prediction_key) for item in simulations]
    predicted = [(item.get("prediction") or {}).get(prediction_key) for item in simulations]
    labels = sorted({value for value in actual + predicted if value not in (None, "")})
    rows = []
    rows.append(_classification_row("Desktop AI", actual, predicted, labels))
    rows.append(_classification_row("Majority baseline", actual, _majority_predictions(actual), labels))
    rows.append(_classification_row("Previous-state baseline", actual, _previous_predictions(actual), labels))
    rows.append(_classification_row("Reversal baseline", actual, _reversal_predictions(actual), labels))
    rows.append(_classification_row("Rolling majority 100", actual, _rolling_majority_predictions(actual, 100), labels))
    rows.append(_classification_row("Proportional random baseline", actual, _proportional_random_predictions(actual, labels), labels))
    majority_accuracy = rows[1]["accuracy"]
    for row in rows:
        row["majority_baseline_lift"] = round(row["accuracy"] - majority_accuracy, 6)
    return rows


def _rule_equal_size_random(report: dict[str, Any]) -> list[dict[str, Any]]:
    simulations = report["simulations"]
    rows = []
    for key, perf in sorted(report["rule_performance"].items()):
        candidate_counts = []
        actual_hits = []
        any_hits = []
        for sim in simulations:
            for rule in sim["rule_results"]:
                if rule["rule_key"] == key:
                    k = len(rule["candidates"])
                    hits = len(set(rule["candidates"]) & set(sim["target_numbers"]))
                    candidate_counts.append(k)
                    actual_hits.append(hits)
                    any_hits.append(1 if hits else 0)
                    break
        if not candidate_counts:
            continue
        expected_hits = [count * 20 / 80 for count in candidate_counts]
        expected_any = [_random_any_hit_probability(count) for count in candidate_counts]
        actual_mean = mean(actual_hits)
        random_mean = mean(expected_hits)
        any_rate = mean(any_hits)
        random_any_rate = mean(expected_any)
        p_value = _normal_mean_p(actual_hits, expected_hits)
        rows.append(
            {
                "rule_key": key,
                "rule_name_zh": perf["rule_name_zh"],
                "sample_size": len(candidate_counts),
                "average_candidate_count": round(mean(candidate_counts), 4),
                "actual_average_hits": round(actual_mean, 6),
                "expected_random_hits": round(random_mean, 6),
                "excess_hits": round(actual_mean - random_mean, 6),
                "normalized_lift": round((actual_mean - random_mean) / max(random_mean, 1e-9), 6),
                "hit_ratio_vs_random": round(actual_mean / max(random_mean, 1e-9), 6),
                "actual_any_hit_rate": round(any_rate, 6),
                "random_any_hit_rate": round(random_any_rate, 6),
                "percentile_rank": round(_percentile_from_z((actual_mean - random_mean) / max(_std_error(actual_hits), 1e-9)), 6),
                "empirical_p_value": round(p_value, 10),
                "monte_carlo_rounds": MONTE_CARLO_ROUNDS,
                "seed": BOOTSTRAP_SEED,
            }
        )
    return rows


def _rule_enabled_vs_disabled(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, perf in sorted(report["rule_performance"].items()):
        inclusion = perf.get("prediction_inclusion_average", 0)
        enabled = inclusion > 0
        rows.append(
            {
                "rule_key": key,
                "rule_name_zh": perf["rule_name_zh"],
                "enabled_sample_size": perf["usage_count"] if enabled else 0,
                "disabled_sample_size": 0 if enabled else perf["usage_count"],
                "enabled_average_score": perf["average_score"] if enabled else 0,
                "disabled_average_score": 0 if enabled else perf["average_score"],
                "enabled_average_candidate_hits": perf["average_candidate_hits"] if enabled else 0,
                "disabled_average_candidate_hits": 0 if enabled else perf["average_candidate_hits"],
                "bootstrap_confidence_interval_95": _normal_ci(perf["average_candidate_hits"], 0),
                "p_value": None,
            }
        )
    return rows


def _walk_forward(simulations: list[dict[str, Any]], initial_train: int, validation_size: int, step: int) -> dict[str, Any]:
    folds = []
    start = initial_train
    fold_index = 1
    while start < len(simulations):
        end = min(start + validation_size, len(simulations))
        validation = simulations[start:end]
        if not validation:
            break
        train = simulations[:start]
        fold = _fold_summary(fold_index, train, validation)
        folds.append(fold)
        start += step
        fold_index += 1
    lifts = [fold["lift_vs_random_20"] for fold in folds]
    return {
        "folds": folds,
        "summary": {
            "fold_count": len(folds),
            "fold_average_hits": round(mean([fold["average_hits"] for fold in folds]), 6) if folds else 0,
            "fold_standard_deviation": round(pstdev([fold["average_hits"] for fold in folds]), 6) if len(folds) > 1 else 0,
            "positive_lift_folds": sum(1 for lift in lifts if lift > 0),
            "negative_lift_folds": sum(1 for lift in lifts if lift <= 0),
            "best_fold": max(folds, key=lambda item: item["lift_vs_random_20"], default=None),
            "worst_fold": min(folds, key=lambda item: item["lift_vs_random_20"], default=None),
        },
    }


def _candidate_count_analysis(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key, perf in sorted(report["rule_performance"].items()):
        total_candidates = perf.get("candidate_total", 0)
        hits = perf.get("candidate_hit_total", 0)
        precision = hits / total_candidates if total_candidates else 0
        recall = hits / (perf["usage_count"] * 20) if perf["usage_count"] else 0
        rows.append(
            {
                "rule_key": key,
                "rule_name_zh": perf["rule_name_zh"],
                "candidates_per_trigger": round(total_candidates / perf["usage_count"], 6) if perf["usage_count"] else 0,
                "hits_per_candidate": round(precision, 6),
                "precision": round(precision, 6),
                "recall": round(recall, 6),
                "lift_per_candidate": round(precision / 0.25, 6) if precision else 0,
                "information_gain": round(_information_gain(precision, 0.25), 6),
            }
        )
    return rows


def _multiple_testing(rule_rows: list[dict[str, Any]], overall: dict[str, Any], high5: dict[str, Any], super_stats: dict[str, Any]) -> list[dict[str, Any]]:
    tests = [
        {"test_name": "overall_20_hits", "p_value": overall["p_value_vs_baseline"]},
        {"test_name": "high5_hits", "p_value": high5["p_value_vs_baseline"]},
        {"test_name": "super_candidate", "p_value": super_stats["p_value_vs_baseline"]},
    ]
    tests.extend({"test_name": f"rule_{row['rule_key']}", "p_value": row["empirical_p_value"]} for row in rule_rows)
    m = len(tests)
    ordered = sorted(tests, key=lambda item: item["p_value"])
    for rank, item in enumerate(ordered, start=1):
        item["bonferroni_p"] = min(1, item["p_value"] * m)
        item["fdr_bh_threshold"] = round(rank / m * 0.05, 10)
        item["significant_bonferroni_0_05"] = item["bonferroni_p"] < 0.05
        item["significant_bh_0_05"] = item["p_value"] <= item["fdr_bh_threshold"]
    return ordered


def _losing_streaks(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        _streak_row("prediction_20_zero_hits", [item["hits_20"] == 0 for item in report["simulations"]]),
        _streak_row("high5_zero_hits", [item["hits_high5"] == 0 for item in report["simulations"]]),
        _streak_row("below_random_20_hits", [item["hits_20"] < RANDOM_20_EXPECTED for item in report["simulations"]]),
    ]
    for key, perf in sorted(report["rule_performance"].items()):
        rows.append(
            {
                "name": f"rule_{key}",
                "sample_size": perf["usage_count"],
                "average_hits": perf["average_candidate_hits"],
                "lift": perf["average_candidate_hits"] - perf["candidate_total"] / max(1, perf["usage_count"]) * 0.25,
                "failure_rate": round(1 - perf["success_rate"], 6),
                "max_losing_streak": None,
            }
        )
    return rows


def export_phase2_1_validation(csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH), output_dir: str | Path = Path("desktop") / "output" / "phase2_1_validation") -> dict[str, Any]:
    validation = run_phase2_1_validation(csv_path)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / "metric_definition_audit.json", validation["metric_definition_audit"])
    _write_json(target / "overall_significance.json", validation["overall_significance"])
    _write_json(target / "high5_significance.json", validation["high5_significance"])
    _write_json(target / "super_candidate_significance.json", validation["super_candidate_significance"])
    _write_csv(target / "big_small_baselines.csv", validation["big_small_baselines"])
    _write_csv(target / "odd_even_baselines.csv", validation["odd_even_baselines"])
    _write_csv(target / "rule_equal_size_random_baseline.csv", validation["rule_equal_size_random_baseline"])
    _write_csv(target / "rule_enabled_vs_disabled.csv", validation["rule_enabled_vs_disabled"])
    _write_csv(target / "walk_forward_folds.csv", validation["walk_forward"]["folds"])
    _write_json(target / "walk_forward_summary.json", validation["walk_forward"]["summary"])
    _write_csv(target / "rule_candidate_count_analysis.csv", validation["rule_candidate_count_analysis"])
    _write_csv(target / "multiple_testing_results.csv", validation["multiple_testing_results"])
    _write_csv(target / "losing_streak_analysis.csv", validation["losing_streak_analysis"])
    (target / "phase2_1_report.txt").write_text(_text_report(validation), encoding="utf-8")
    return {"output_dir": str(target), "validation": validation}


def _fold_summary(index: int, train: list[dict[str, Any]], validation: list[dict[str, Any]]) -> dict[str, Any]:
    hits = [item["hits_20"] for item in validation]
    high5 = [item["hits_high5"] for item in validation]
    return {
        "fold": index,
        "training_issue_range": f"{train[0]['target_issue']}..{train[-1]['target_issue']}" if train else "",
        "validation_issue_range": f"{validation[0]['target_issue']}..{validation[-1]['target_issue']}",
        "training_count": len(train),
        "validation_count": len(validation),
        "average_hits": round(mean(hits), 6),
        "average_high5_hits": round(mean(high5), 6),
        "super_hit_rate": _bool_rate(item["super_hit"] for item in validation),
        "big_small_accuracy": _bool_rate(item["big_small_hit"] for item in validation),
        "odd_even_accuracy": _bool_rate(item["odd_even_hit"] for item in validation),
        "lift_vs_random_20": round(mean(hits) - RANDOM_20_EXPECTED, 6),
        "best_rule": "",
        "worst_rule": "",
    }


def _classification_row(name: str, actual: list[str | None], predicted: list[str | None], labels: list[str]) -> dict[str, Any]:
    pairs = [(a, p) for a, p in zip(actual, predicted) if a not in (None, "") and p not in (None, "")]
    if not pairs:
        return {"baseline": name, "accuracy": 0, "balanced_accuracy": 0, "macro_f1": 0, "confusion_matrix": "{}", "per_class": "{}"}
    correct = sum(1 for a, p in pairs if a == p)
    per_class = {}
    recalls = []
    f1s = []
    matrix = {label: {inner: 0 for inner in labels} for label in labels}
    for a, p in pairs:
        matrix[a][p] += 1
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in labels if other != label)
        fn = sum(matrix[label][other] for other in labels if other != label)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        per_class[label] = {"precision": round(precision, 6), "recall": round(recall, 6), "f1": round(f1, 6)}
        recalls.append(recall)
        f1s.append(f1)
    return {
        "baseline": name,
        "sample_size": len(pairs),
        "accuracy": round(correct / len(pairs), 6),
        "balanced_accuracy": round(mean(recalls), 6) if recalls else 0,
        "macro_f1": round(mean(f1s), 6) if f1s else 0,
        "confusion_matrix": json.dumps(matrix, ensure_ascii=False, sort_keys=True),
        "per_class": json.dumps(per_class, ensure_ascii=False, sort_keys=True),
    }


def _actual_state(item: dict[str, Any], prediction_key: str) -> str | None:
    numbers = item.get("target_numbers") or []
    if prediction_key == "big_small":
        big = sum(1 for number in numbers if number >= 41)
        small = len(numbers) - big
        return "偏大" if big > small else "偏小" if small > big else "均衡"
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    return "單" if odd > even else "雙" if even > odd else "均衡"


def _majority_predictions(actual: list[str | None]) -> list[str | None]:
    majority = Counter([item for item in actual if item]).most_common(1)
    value = majority[0][0] if majority else None
    return [value for _ in actual]


def _previous_predictions(actual: list[str | None]) -> list[str | None]:
    output = []
    previous = None
    for item in actual:
        output.append(previous or item)
        previous = item
    return output


def _reversal_predictions(actual: list[str | None]) -> list[str | None]:
    reverse = {"偏大": "偏小", "偏小": "偏大", "單": "雙", "雙": "單", "均衡": "均衡"}
    return [reverse.get(item, item) for item in _previous_predictions(actual)]


def _rolling_majority_predictions(actual: list[str | None], window: int) -> list[str | None]:
    output = []
    for index, _ in enumerate(actual):
        source = [item for item in actual[max(0, index - window):index] if item]
        output.append(Counter(source).most_common(1)[0][0] if source else actual[index])
    return output


def _proportional_random_predictions(actual: list[str | None], labels: list[str]) -> list[str | None]:
    rng = random.Random(BOOTSTRAP_SEED)
    counts = Counter([item for item in actual if item])
    total = sum(counts.values())
    if not total:
        return [None for _ in actual]
    population = []
    for label in labels:
        population.extend([label] * counts[label])
    return [rng.choice(population) for _ in actual]


def _normal_ci(avg: float, se: float) -> dict[str, float]:
    return {"lower": round(avg - 1.96 * se, 6), "upper": round(avg + 1.96 * se, 6)}


def _wilson_ci(hits: int, n: int) -> dict[str, float]:
    if n == 0:
        return {"lower": 0, "upper": 0}
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return {"lower": round(center - margin, 6), "upper": round(center + margin, 6)}


def _bootstrap_ci(values: list[float], rounds: int, seed: int) -> dict[str, float]:
    rng = random.Random(seed)
    n = len(values)
    if not n:
        return {"lower": 0, "upper": 0}
    means = []
    for _ in range(rounds):
        total = 0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    return {"lower": round(means[int(rounds * 0.025)], 6), "upper": round(means[int(rounds * 0.975)], 6)}


def _normal_mean_p(actual_values: list[float], expected_values: list[float]) -> float:
    diffs = [a - e for a, e in zip(actual_values, expected_values)]
    if not diffs:
        return 1
    avg = mean(diffs)
    se = _std_error(diffs)
    return _two_sided_normal_p(avg / se) if se else 1


def _std_error(values: list[float]) -> float:
    return (pstdev(values) / math.sqrt(len(values))) if len(values) > 1 else 0


def _two_sided_normal_p(z: float) -> float:
    return 2 * (1 - NormalDist().cdf(abs(z)))


def _percentile_from_z(z: float) -> float:
    return NormalDist().cdf(z)


def _random_any_hit_probability(k: int) -> float:
    if k <= 0:
        return 0
    if k > 60:
        return 1
    miss = 1.0
    for i in range(20):
        miss *= (80 - k - i) / (80 - i)
    return 1 - miss


def _information_gain(precision: float, baseline: float) -> float:
    eps = 1e-9
    p = min(max(precision, eps), 1 - eps)
    q = min(max(baseline, eps), 1 - eps)
    return p * math.log2(p / q) + (1 - p) * math.log2((1 - p) / (1 - q))


def _bool_rate(values: Iterable[bool]) -> float:
    values = list(values)
    return round(sum(1 for value in values if value) / len(values), 6) if values else 0


def _streak_row(name: str, failures: list[bool]) -> dict[str, Any]:
    max_streak = 0
    current = 0
    for failed in failures:
        current = current + 1 if failed else 0
        max_streak = max(max_streak, current)
    return {
        "name": name,
        "sample_size": len(failures),
        "average_hits": None,
        "lift": None,
        "failure_rate": round(sum(1 for item in failures if item) / len(failures), 6) if failures else 0,
        "max_losing_streak": max_streak,
    }


def _empty_metric(expected: float) -> dict[str, Any]:
    return {
        "sample_size": 0,
        "mean": 0,
        "median": 0,
        "standard_deviation": 0,
        "standard_error": 0,
        "confidence_interval_95": {"lower": 0, "upper": 0},
        "expected_baseline": expected,
        "difference_vs_baseline": 0,
        "effect_size": 0,
        "bootstrap_confidence_interval_95": {"lower": 0, "upper": 0},
        "p_value_vs_baseline": 1,
        "ci_excludes_baseline": False,
        "bootstrap_rounds": BOOTSTRAP_ROUNDS,
        "seed": BOOTSTRAP_SEED,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows or [])
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _text_report(validation: dict[str, Any]) -> str:
    phase2 = validation["phase2_report"]
    overall = validation["overall_significance"]
    high5 = validation["high5_significance"]
    super_stats = validation["super_candidate_significance"]
    walk = validation["walk_forward"]["summary"]
    return "\n".join(
        [
            "Phase Desktop 2.1 - Rule Metric Calibration and Walk-forward Validation",
            f"Replay samples: {phase2['valid_simulations']}",
            f"20-number mean: {overall['mean']} CI={overall['confidence_interval_95']} p={overall['p_value_vs_baseline']}",
            f"High5 mean: {high5['mean']} CI={high5['confidence_interval_95']} p={high5['p_value_vs_baseline']}",
            f"Super hit rate: {super_stats['hit_rate']} CI={super_stats['confidence_interval_95']} p={super_stats['p_value_vs_baseline']}",
            f"Walk-forward folds: {walk['fold_count']} positive={walk['positive_lift_folds']} negative={walk['negative_lift_folds']}",
            f"No look-ahead: {phase2['no_look_ahead']}",
            "Conclusion: research_only; do not promote rules unless holdout and walk-forward lift are stable after multiple-testing correction.",
        ]
    )

