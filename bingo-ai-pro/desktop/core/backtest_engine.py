from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from desktop.core.data_repository import DataRepository
from desktop.core.rule_order import RULE_NAME_ZH, fixed_rule_keys
from desktop.core.validators import normalize_numbers, validate_prediction


def run_readonly_backtest(repository: DataRepository | None = None, limit: int = 100) -> dict[str, Any]:
    repo = repository or DataRepository()
    predictions = repo.get_prediction_history(limit=limit)
    rows = []
    skipped = Counter()
    rule_hits: dict[str, list[int]] = defaultdict(list)
    for prediction in predictions:
        valid, reason = validate_prediction(prediction)
        if not valid:
            skipped[reason or "invalid_prediction"] += 1
            continue
        source_issue = str(prediction.get("issue"))
        target_issue = str(prediction.get("prediction_issue"))
        if int(target_issue) != int(source_issue) + 1:
            skipped["look_ahead_guard"] += 1
            continue
        actual = repo.get_draw_by_issue(target_issue)
        if not actual:
            skipped["target_draw_missing"] += 1
            continue
        predicted = normalize_numbers(prediction.get("recommend_numbers"))
        actual_numbers = set(normalize_numbers(actual.get("numbers")))
        hits = len(set(predicted) & actual_numbers)
        super_hit = bool(prediction.get("super_number") and prediction.get("super_number") == actual.get("super_number"))
        big_small_hit = _same_value(prediction.get("big_small"), actual.get("big_small"))
        odd_even_hit = _same_value(prediction.get("odd_even"), actual.get("odd_even"))
        rows.append(
            {
                "source_issue": source_issue,
                "target_issue": target_issue,
                "hits": hits,
                "super_hit": super_hit,
                "big_small_hit": big_small_hit,
                "odd_even_hit": odd_even_hit,
            }
        )
        snapshot_rows = repo.get_rule_snapshots_for_issue(source_issue)
        if snapshot_rows:
            snapshot = snapshot_rows[0].get("snapshot_json") or {}
            for rule in snapshot.get("rules") or []:
                key = rule.get("rule_key") or rule.get("key")
                candidates = set(normalize_numbers(rule.get("candidates") or rule.get("numbers")))
                if key in RULE_NAME_ZH and candidates:
                    rule_hits[key].append(len(candidates & actual_numbers))
    hit_values = [row["hits"] for row in rows]
    return {
        "total_predictions": len(predictions),
        "valid_simulations": len(rows),
        "skipped": dict(skipped),
        "average_hits": round(sum(hit_values) / len(hit_values), 2) if hit_values else 0,
        "max_hits": max(hit_values) if hit_values else 0,
        "min_hits": min(hit_values) if hit_values else 0,
        "super_hits": sum(1 for row in rows if row["super_hit"]),
        "big_small_hits": sum(1 for row in rows if row["big_small_hit"]),
        "odd_even_hits": sum(1 for row in rows if row["odd_even_hit"]),
        "rule_performance": {
            key: {
                "rule_name_zh": RULE_NAME_ZH[key],
                "sample_size": len(values),
                "average_hits": round(sum(values) / len(values), 2) if values else 0,
            }
            for key in fixed_rule_keys()
            if (values := rule_hits.get(key))
        },
        "look_ahead_bias": False,
        "rows": rows,
    }


def _same_value(left: Any, right: Any) -> bool:
    return left not in (None, "") and right not in (None, "") and str(left) == str(right)

