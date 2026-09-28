from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import learning_engine


def _row(issue: str, rule_key: str, candidates: list[int], official: list[int]) -> dict:
    return {
        "issue": issue,
        "official_numbers": official,
        "analysis_snapshot": {
            "ai_score": {
                rule_key: {"candidate_numbers": candidates, "shadow_only": True},
            }
        },
    }


def test_rule_is_retained_while_learning_and_not_recommendation_eligible():
    rows = [_row(str(i), "long_dragon", list(range(1, 21)), list(range(1, 21))) for i in range(10)]
    result = learning_engine.evaluate_shadow_rule_promotions(rows, "10")
    rule = result["rules"]["long_dragon"]
    assert rule["state"] == "learning"
    assert rule["retained"] is True
    assert rule["eligible_for_recommendation"] is False


def test_rule_promotes_only_after_100_verified_samples_with_positive_lift():
    rows = [_row(str(i), "long_dragon", list(range(1, 21)), list(range(1, 21))) for i in range(100)]
    result = learning_engine.evaluate_shadow_rule_promotions(rows, "100")
    rule = result["rules"]["long_dragon"]
    assert rule["sample_size"] == 100
    assert rule["average_lift_vs_random"] > 0.25
    assert rule["state"] == "mature"
    assert rule["eligible_for_recommendation"] is True


def test_weak_rule_is_downgraded_not_deleted():
    candidates = list(range(1, 21))
    official = list(range(21, 41))
    rows = [_row(str(i), "long_dragon", candidates, official) for i in range(100)]
    result = learning_engine.evaluate_shadow_rule_promotions(rows, "100")
    rule = result["rules"]["long_dragon"]
    assert rule["state"] == "observing"
    assert rule["eligible_for_recommendation"] is False
    assert rule["retained"] is True


def test_recent_decline_prevents_mature_status():
    rows = []
    for i in range(100):
        official = list(range(21, 41)) if i < 20 else list(range(1, 21))
        rows.append(_row(str(100 - i), "long_dragon", list(range(1, 21)), official))
    result = learning_engine.evaluate_shadow_rule_promotions(rows, "100")
    rule = result["rules"]["long_dragon"]
    assert rule["average_lift_vs_random"] > 0.25
    assert rule["recent_20_lift_vs_random"] < 0
    assert rule["state"] == "observing"
    assert rule["retained"] is True
