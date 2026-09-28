from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import _omission_strength, build_analysis_record


def _draw(issue: str, numbers: list[int]) -> dict:
    return {"issue": issue, "numbers": numbers, "source": "taiwan_lottery"}


def test_omission_strength_tracks_current_average_and_maximum_gap():
    current = _draw("115000111", [1, 2])
    recent = []
    for offset in range(10):
        issue = str(115000110 - offset)
        numbers = [1] if offset in {2, 7} else [3]
        recent.append(_draw(issue, numbers))

    result = _omission_strength(current, recent, lookback=10)
    one = next(item for item in result["metrics"] if item["number"] == 1)

    assert one["current_omission"] == 2
    assert one["average_omission"] >= 0
    assert one["max_omission"] >= one["current_omission"]
    assert result["available_draws"] == 10
    assert result["shadow_only"] is True


def test_omission_strength_excludes_current_issue_from_history():
    current = _draw("115000103", [9])
    recent = [
        _draw("115000103", [9]),
        _draw("115000102", [8]),
        _draw("115000101", [9]),
    ]

    result = _omission_strength(current, recent, lookback=10)
    nine = next(item for item in result["metrics"] if item["number"] == 9)

    assert result["available_draws"] == 2
    assert nine["current_omission"] == 1
    assert nine["appearance_count"] == 1


def test_omission_strength_identifies_overdue_and_recovery_numbers():
    current = _draw("115000111", [5, 20])
    recent = []
    for offset in range(10):
        numbers = [5] if offset == 9 else [20]
        recent.append(_draw(str(115000110 - offset), numbers))

    result = _omission_strength(current, recent, lookback=10)
    overdue_numbers = [item["number"] for item in result["overdue_numbers"]]
    recovery_numbers = [item["number"] for item in result["recovery_numbers"]]

    assert 5 in overdue_numbers
    assert 5 in recovery_numbers
    assert 5 in result["candidate_numbers"]


def test_analysis_record_exposes_omission_as_learning_feature_only():
    current = _draw("115000104", [5, 6, 20])
    recent = [
        _draw("115000103", [6, 30]),
        _draw("115000102", [7, 40]),
        _draw("115000101", [5, 60]),
    ]

    record = build_analysis_record(current, recent_draws=recent)
    data = record["ai_score"]["omission_strength"]
    learning = record["ai_score"]["learning_features"]

    assert data["shadow_only"] is True
    assert learning["omission_candidates"] == data["candidate_numbers"]
    assert learning["omission_overdue_numbers"] == data["overdue_numbers"]
    assert learning["omission_recovery_numbers"] == data["recovery_numbers"]
    assert "omission_strength" not in record
