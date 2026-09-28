from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import _composite_market_regime, build_analysis_record


def _draw(issue: str, numbers: list[int]) -> dict:
    return {"issue": issue, "numbers": numbers, "source": "taiwan_lottery"}


def test_composite_market_regime_rewards_cross_rule_consensus():
    signals = {
        "a": {"candidate_numbers": [10, 20], "confidence": 100},
        "b": {"candidate_numbers": [10, 30], "confidence": 80},
        "c": {"candidate_numbers": [10, 40], "confidence": 60},
        "d": {"candidate_numbers": [10, 50], "confidence": 40},
    }
    result = _composite_market_regime(signals)
    assert result["candidate_numbers"][0] == 10
    assert result["consensus"][0]["source_count"] == 4
    assert result["regime"] == "strong_consensus"
    assert result["shadow_only"] is True


def test_composite_market_regime_marks_single_source_conflicts():
    result = _composite_market_regime({
        "a": {"candidate_numbers": [1], "confidence": 100},
        "b": {"candidate_numbers": [2], "confidence": 100},
        "c": {"candidate_numbers": [3], "confidence": 100},
    })
    assert result["regime"] == "mixed"
    assert {item["number"] for item in result["conflicts"]} == {1, 2, 3}


def test_analysis_record_exposes_composite_only_under_ai_score():
    current = _draw("115000104", [1, 2, 41, 51])
    recent = [
        _draw("115000103", [80, 1, 2, 41, 51]),
        _draw("115000102", [10, 11, 12, 42, 52]),
        _draw("115000101", [20, 21, 22, 43, 53]),
    ]
    record = build_analysis_record(current, recent_draws=recent)
    composite = record["ai_score"]["composite_market_regime"]
    learning = record["ai_score"]["learning_features"]
    assert composite["shadow_only"] is True
    assert learning["composite_candidates"] == composite["candidate_numbers"]
    assert learning["composite_consensus"] == composite["consensus"]
    assert "composite_market_regime" not in record
