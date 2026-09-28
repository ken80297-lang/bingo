from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import _long_dragon_tracking, build_analysis_record


def _draw(issue: str, numbers: list[int]) -> dict:
    return {"issue": issue, "numbers": numbers, "source": "taiwan_lottery"}


def test_long_dragon_counts_consecutive_appearance_and_stops_at_break():
    current = _draw("115000104", [5, 6, 20])
    recent = [
        _draw("115000103", [5, 6, 30]),
        _draw("115000102", [5, 6, 40]),
        _draw("115000101", [5, 50]),
        _draw("115000100", [5, 6, 60]),
    ]

    result = _long_dragon_tracking(current, recent)

    streaks = {item["number"]: item["streak"] for item in result["streaks"]}
    assert streaks == {5: 5, 6: 3}
    assert result["candidate_numbers"] == [5, 6]
    assert result["max_streak"] == 5
    assert result["active_count"] == 2
    assert result["shadow_only"] is True


def test_long_dragon_ignores_numbers_without_active_streak():
    result = _long_dragon_tracking(
        _draw("115000102", [7, 8]),
        [_draw("115000101", [1, 2]), _draw("115000100", [7, 8])],
    )

    assert result["streaks"] == []
    assert result["candidate_numbers"] == []
    assert result["max_streak"] == 0
    assert result["confidence"] == 0


def test_analysis_record_exposes_long_dragon_as_learning_feature_only():
    current = _draw("115000103", [5, 6, 20])
    recent = [
        _draw("115000102", [5, 6, 30]),
        _draw("115000101", [5, 6, 40]),
        _draw("115000100", [5, 50, 60]),
    ]

    record = build_analysis_record(current, recent_draws=recent)
    long_dragon = record["ai_score"]["long_dragon"]
    learning = record["ai_score"]["learning_features"]

    assert long_dragon["candidate_numbers"] == [5, 6]
    assert long_dragon["shadow_only"] is True
    assert learning["long_dragon_candidates"] == [5, 6]
    assert learning["long_dragon_max_streak"] == 4
    assert "long_dragon" not in record
