from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any

from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH, ReplayDataset, ReplayDraw, load_replay_dataset
from desktop.core.rule_order import RULE_NAME_ZH
from desktop.core.rule_replay import RuleReplayResult, replay_all_rules


@dataclass(frozen=True)
class ReplayPrediction:
    recommend_numbers: list[int]
    high_probability_numbers: list[int]
    super_candidate: int | None
    big_small: str | None
    odd_even: str | None
    confidence: float
    active_conditions: list[str]


@dataclass(frozen=True)
class ReplaySimulation:
    source_issue: str
    target_issue: str
    target_date: str
    target_time: str
    target_numbers: list[int]
    rule_results: list[RuleReplayResult]
    prediction: ReplayPrediction
    hits_20: int
    hits_high5: int
    super_hit: bool
    big_small_hit: bool
    odd_even_hit: bool
    valid_prediction: bool
    invalid_reason: str | None
    history_count: int
    history_last_issue: str | None
    maximum_feature_issue: str | None
    used_future_rows: int


def run_phase2_backtest(path: str = str(DEFAULT_MASTER_DRAWS_PATH), min_history: int = 100) -> dict[str, Any]:
    dataset = load_replay_dataset(path)
    valid = dataset.valid_draws
    simulations: list[ReplaySimulation] = []
    skipped: list[dict[str, str]] = []
    for target_index, target in enumerate(valid):
        if target_index < min_history:
            skipped.append({"issue": target.issue, "reason": "insufficient_history"})
            continue
        history = valid[:target_index]
        source = history[-1]
        rule_results = replay_all_rules(history, source)
        prediction = build_replay_prediction(rule_results, history)
        simulations.append(_verify(source, target, rule_results, prediction, history))
    return build_phase2_report(dataset, simulations, skipped)


def build_replay_prediction(rule_results: list[RuleReplayResult], history: list[ReplayDraw]) -> ReplayPrediction:
    scores: dict[int, float] = defaultdict(float)
    for rule in rule_results:
        weight = max(0.05, rule.score * 0.55 + rule.confidence * 0.45)
        for rank, number in enumerate(rule.candidates[:20]):
            scores[number] += weight * (1 - rank / 25)
    ranked = [number for number, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
    recommend = _fill_numbers(ranked, history)
    high5 = recommend[:5]
    super_candidate = _super_candidate(rule_results, recommend)
    big_small = _big_small(recommend)
    odd_even = _odd_even(recommend)
    conditions = _active_conditions(rule_results)
    condition_keys = _active_condition_keys(rule_results)
    confidence = min(1.0, sum(rule.confidence for rule in rule_results if rule.rule_key in condition_keys) / max(1, len(condition_keys)))
    return ReplayPrediction(recommend, high5, super_candidate, big_small, odd_even, round(confidence, 4), conditions)


def build_phase2_report(dataset: ReplayDataset, simulations: list[ReplaySimulation], skipped: list[dict[str, str]]) -> dict[str, Any]:
    hits = [item.hits_20 for item in simulations]
    high_hits = [item.hits_high5 for item in simulations]
    valid_predictions = [item for item in simulations if item.valid_prediction]
    rule_performance = _rule_performance(simulations)
    best_rule = max(rule_performance.values(), key=lambda item: (item["success_rate"], item["average_score"]), default=None)
    worst_rule = min(rule_performance.values(), key=lambda item: (item["success_rate"], item["average_score"]), default=None)
    high_confidence = _high_confidence_report(simulations)
    holdout = _holdout_report(simulations)
    baseline = _baseline_comparison(simulations, rule_performance)
    return {
        "dataset": {
            "path": str(dataset.path),
            "total_rows": dataset.summary.total_rows,
            "valid_rows": dataset.summary.valid_rows,
            "warmup_rows": len(skipped),
            "replay_target_rows": len(simulations),
            "first_issue": dataset.summary.first_issue,
            "last_issue": dataset.summary.last_issue,
            "missing_issues": dataset.summary.missing_issues,
            "duplicate_issues": dataset.summary.duplicate_issues,
            "invalid_rows": dataset.summary.invalid_rows,
        },
        "total_simulations": len(simulations) + len(skipped),
        "valid_simulations": len(simulations),
        "valid_prediction_count": len(valid_predictions),
        "invalid_prediction_count": len(simulations) - len(valid_predictions),
        "skipped": skipped,
        "average_hits": round(mean(hits), 4) if hits else 0,
        "max_hits": max(hits) if hits else 0,
        "min_hits": min(hits) if hits else 0,
        "average_high5_hits": round(mean(high_hits), 4) if high_hits else 0,
        "super_hit_rate": _rate(item.super_hit for item in simulations),
        "big_small_hit_rate": _rate(item.big_small_hit for item in simulations),
        "odd_even_hit_rate": _rate(item.odd_even_hit for item in simulations),
        "rule_performance": rule_performance,
        "best_rule": best_rule,
        "worst_rule": worst_rule,
        "high_confidence": high_confidence,
        "baseline_comparison": baseline,
        "holdout": holdout,
        "daily_summary": _daily_summary(simulations),
        "hit_distribution": dict(sorted(Counter(hits).items())),
        "high5_distribution": dict(sorted(Counter(high_hits).items())),
        "no_look_ahead": all(item.used_future_rows == 0 and int(item.source_issue) < int(item.target_issue) for item in simulations),
        "look_ahead_audit": [_audit_dict(item) for item in simulations],
        "simulations": [_simulation_dict(item) for item in simulations],
    }


def _verify(source: ReplayDraw, target: ReplayDraw, rules: list[RuleReplayResult], prediction: ReplayPrediction, history: list[ReplayDraw]) -> ReplaySimulation:
    actual = set(target.numbers)
    valid_prediction, invalid_reason = _validate_prediction(prediction)
    maximum_feature_issue = history[-1].issue if history else None
    return ReplaySimulation(
        source_issue=source.issue,
        target_issue=target.issue,
        target_date=target.date,
        target_time=target.time,
        target_numbers=target.numbers,
        rule_results=rules,
        prediction=prediction,
        hits_20=len(set(prediction.recommend_numbers) & actual),
        hits_high5=len(set(prediction.high_probability_numbers) & actual),
        super_hit=bool(prediction.super_candidate and prediction.super_candidate == target.super_number),
        big_small_hit=prediction.big_small == target.big_small,
        odd_even_hit=prediction.odd_even == target.odd_even,
        valid_prediction=valid_prediction,
        invalid_reason=invalid_reason,
        history_count=len(history),
        history_last_issue=history[-1].issue if history else None,
        maximum_feature_issue=maximum_feature_issue,
        used_future_rows=0,
    )


def _rule_performance(simulations: list[ReplaySimulation]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"scores": [], "successes": [], "candidate_counts": [], "candidate_hits": [], "prediction_inclusions": []}
    )
    for simulation in simulations:
        actual = set(simulation.target_numbers)
        prediction_set = set(simulation.prediction.recommend_numbers)
        for rule in simulation.rule_results:
            buckets[rule.rule_key]["scores"].append(rule.score)
            candidates = set(rule.candidates[:20])
            hits = len(candidates & actual)
            buckets[rule.rule_key]["successes"].append(1 if hits else 0)
            buckets[rule.rule_key]["candidate_counts"].append(len(candidates))
            buckets[rule.rule_key]["candidate_hits"].append(hits)
            buckets[rule.rule_key]["prediction_inclusions"].append(len(candidates & prediction_set))
    output = {}
    for key, values in buckets.items():
        successes = values["successes"]
        output[key] = {
            "rule_key": key,
            "rule_name_zh": RULE_NAME_ZH.get(key, key),
            "usage_count": len(values["scores"]),
            "average_score": round(mean(values["scores"]), 4) if values["scores"] else 0,
            "success_rate": round(sum(successes) / len(successes), 4) if successes else 0,
            "candidate_total": int(sum(values["candidate_counts"])),
            "candidate_hit_total": int(sum(values["candidate_hits"])),
            "average_candidate_hits": round(mean(values["candidate_hits"]), 4) if values["candidate_hits"] else 0,
            "prediction_inclusion_average": round(mean(values["prediction_inclusions"]), 4) if values["prediction_inclusions"] else 0,
        }
    return output


def _high_confidence_report(simulations: list[ReplaySimulation]) -> dict[str, Any]:
    condition_hits: dict[str, list[int]] = defaultdict(list)
    combo_hits: dict[int, list[int]] = defaultdict(list)
    for simulation in simulations:
        for condition in simulation.prediction.active_conditions:
            condition_hits[condition].append(simulation.hits_20)
        combo_hits[len(simulation.prediction.active_conditions)].append(simulation.hits_20)
    overall_average = mean([item.hits_20 for item in simulations]) if simulations else 0
    conditions = {
        key: {
            "sample_size": len(values),
            "average_hits": round(mean(values), 4) if values else 0,
            "lift_vs_overall": round((mean(values) - overall_average), 4) if values else 0,
            "decision": "\u51fa\u624b" if values and mean(values) > overall_average else "\u8df3\u904e",
        }
        for key, values in sorted(condition_hits.items())
    }
    combo = {
        str(key): {
            "sample_size": len(values),
            "average_hits": round(mean(values), 4) if values else 0,
            "lift_vs_overall": round((mean(values) - overall_average), 4) if values else 0,
            "decision": "\u51fa\u624b" if values and mean(values) > overall_average else "\u8df3\u904e",
        }
        for key, values in sorted(combo_hits.items())
    }
    playable = [key for key, item in conditions.items() if item["decision"] == "\u51fa\u624b"]
    return {"conditions": conditions, "condition_count_lift": combo, "high_confidence_strategy": playable}


def _simulation_dict(item: ReplaySimulation) -> dict[str, Any]:
    payload = asdict(item)
    payload["rule_results"] = [asdict(rule) for rule in item.rule_results]
    payload["prediction"] = asdict(item.prediction)
    return payload


def _audit_dict(item: ReplaySimulation) -> dict[str, Any]:
    return {
        "target_issue": item.target_issue,
        "history_last_issue": item.history_last_issue,
        "history_count": item.history_count,
        "maximum_feature_issue": item.maximum_feature_issue,
        "passed": bool(item.maximum_feature_issue and int(item.maximum_feature_issue) < int(item.target_issue)),
    }


def _daily_summary(simulations: list[ReplaySimulation]) -> list[dict[str, Any]]:
    buckets: dict[str, list[ReplaySimulation]] = defaultdict(list)
    for item in simulations:
        buckets[item.target_date].append(item)
    rows = []
    for date, items in sorted(buckets.items()):
        rows.append(
            {
                "date": date,
                "simulations": len(items),
                "average_hits": round(mean([item.hits_20 for item in items]), 4),
                "average_high5_hits": round(mean([item.hits_high5 for item in items]), 4),
                "super_hit_rate": _rate(item.super_hit for item in items),
                "big_small_hit_rate": _rate(item.big_small_hit for item in items),
                "odd_even_hit_rate": _rate(item.odd_even_hit for item in items),
            }
        )
    return rows


def _holdout_report(simulations: list[ReplaySimulation]) -> dict[str, Any]:
    if not simulations:
        return {"training_count": 0, "holdout_count": 0, "holdout_average_hits": 0, "holdout_average_high5_hits": 0}
    split = int(len(simulations) * 0.7)
    training = simulations[:split]
    holdout = simulations[split:]
    return {
        "training_count": len(training),
        "holdout_count": len(holdout),
        "holdout_average_hits": round(mean([item.hits_20 for item in holdout]), 4) if holdout else 0,
        "holdout_average_high5_hits": round(mean([item.hits_high5 for item in holdout]), 4) if holdout else 0,
        "holdout_super_hit_rate": _rate(item.super_hit for item in holdout),
        "holdout_big_small_hit_rate": _rate(item.big_small_hit for item in holdout),
        "holdout_odd_even_hit_rate": _rate(item.odd_even_hit for item in holdout),
    }


def _baseline_comparison(simulations: list[ReplaySimulation], rule_performance: dict[str, dict[str, Any]]) -> dict[str, Any]:
    random_20 = 5.0
    random_high5 = 1.25
    average_hits = mean([item.hits_20 for item in simulations]) if simulations else 0
    average_high5 = mean([item.hits_high5 for item in simulations]) if simulations else 0
    return {
        "random_expected_20_hits": random_20,
        "random_expected_high5_hits": random_high5,
        "desktop_ai_average_hits": round(average_hits, 4),
        "desktop_ai_average_high5_hits": round(average_high5, 4),
        "ai_lift_vs_random_20": round(average_hits - random_20, 4),
        "ai_lift_vs_random_high5": round(average_high5 - random_high5, 4),
        "hot_rule_average_candidate_hits": (rule_performance.get("hot") or {}).get("average_candidate_hits", 0),
        "cold_rule_average_candidate_hits": (rule_performance.get("cold") or {}).get("average_candidate_hits", 0),
    }


def _fill_numbers(ranked: list[int], history: list[ReplayDraw]) -> list[int]:
    output = []
    for number in ranked + [number for draw in reversed(history[-5:]) for number in draw.numbers] + list(range(1, 81)):
        if 1 <= number <= 80 and number not in output:
            output.append(number)
        if len(output) == 20:
            return output
    return output


def _super_candidate(rules: list[RuleReplayResult], recommend: list[int]) -> int | None:
    for rule in rules:
        if rule.rule_key in {"super", "super_number_trajectory_recovery"} and rule.candidates:
            return rule.candidates[0]
    return recommend[0] if recommend else None


def _big_small(numbers: list[int]) -> str:
    return "\u504f\u5927" if sum(1 for number in numbers if number >= 41) >= 10 else "\u504f\u5c0f"


def _odd_even(numbers: list[int]) -> str:
    odd = sum(1 for number in numbers if number % 2)
    return "\u55ae" if odd > 10 else "\u96d9" if odd < 10 else "\u5747\u8861"


def _active_conditions(rules: list[RuleReplayResult]) -> list[str]:
    mapping = {
        "cluster": "\u7fa4\u805a",
        "consecutive": "\u5927\u9023\u865f",
        "missing": "\u7f3a\u865f",
        "tail": "\u5c3e\u6578",
        "super": "\u8d85\u734e",
        "momentum": "\u76e4\u52e2",
    }
    return [mapping[key] for key in _active_condition_keys(rules)]


def _active_condition_keys(rules: list[RuleReplayResult]) -> list[str]:
    keys = []
    for rule in rules:
        if rule.rule_key in {"cluster", "missing", "tail", "super", "momentum"} and rule.score >= 0.55:
            keys.append(rule.rule_key)
    return keys


def _validate_prediction(prediction: ReplayPrediction) -> tuple[bool, str | None]:
    if len(prediction.recommend_numbers) != 20:
        return False, "invalid_prediction"
    if len(set(prediction.recommend_numbers)) != 20:
        return False, "invalid_prediction"
    if any(number < 1 or number > 80 for number in prediction.recommend_numbers):
        return False, "invalid_prediction"
    if len(prediction.high_probability_numbers) != 5:
        return False, "invalid_prediction"
    if not set(prediction.high_probability_numbers).issubset(set(prediction.recommend_numbers)):
        return False, "invalid_prediction"
    if prediction.super_candidate is None or not 1 <= prediction.super_candidate <= 80:
        return False, "invalid_prediction"
    return True, None


def _rate(values) -> float:
    values = list(values)
    return round(sum(1 for value in values if value) / len(values), 4) if values else 0
