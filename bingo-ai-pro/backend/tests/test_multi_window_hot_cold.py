from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import _multi_window_hot_cold, build_analysis_record
from database import analysis_store


def _draw(issue: str, numbers: list[int]) -> dict:
    return {"issue": issue, "numbers": numbers, "source": "taiwan_lottery"}


def test_multi_window_hot_cold_builds_10_20_50_100_windows():
    current = _draw("115000121", [1, 2, 3])
    recent = []
    for index in range(120, 20, -1):
        numbers = [1, 2] if index >= 111 else [3, 4]
        recent.append(_draw(f"115000{index:03d}", numbers))

    result = _multi_window_hot_cold(current, recent)

    assert list(result["windows"]) == ["10", "20", "50", "100"]
    assert result["windows"]["10"]["available_draws"] == 10
    assert result["windows"]["100"]["available_draws"] == 100
    assert result["windows"]["10"]["hot_numbers"][:2] == [1, 2]
    assert result["windows"]["10"]["counts"]["80"] == 0
    assert all(result["windows"]["10"]["counts"][str(number)] == 0 for number in result["windows"]["10"]["cold_numbers"])
    assert result["short_window"] == 10
    assert result["long_window"] == 100
    assert result["shadow_only"] is True


def test_multi_window_hot_cold_detects_short_term_rising_numbers():
    current = _draw("115000101", [1, 2])
    recent = []
    for offset in range(100):
        numbers = [1] if offset < 10 else [2]
        recent.append(_draw(str(115000100 - offset), numbers))

    result = _multi_window_hot_cold(current, recent)
    rising = {item["number"]: item["delta"] for item in result["rising_numbers"]}

    assert rising[1] > 0
    assert 1 in result["candidate_numbers"]
    assert result["windows"]["10"]["counts"]["1"] == 10
    assert result["windows"]["100"]["counts"]["1"] == 10


def test_multi_window_excludes_current_issue_if_history_contains_it():
    current = _draw("115000101", [9])
    recent = [
        _draw("115000101", [9]),
        _draw("115000100", [8]),
    ]

    result = _multi_window_hot_cold(current, recent)

    assert result["windows"]["10"]["available_draws"] == 1
    assert result["windows"]["10"]["counts"]["9"] == 0
    assert result["windows"]["10"]["counts"]["8"] == 1


def test_analysis_record_exposes_multi_window_as_learning_feature_only():
    current = _draw("115000103", [5, 6, 20])
    recent = [
        _draw("115000102", [5, 6, 30]),
        _draw("115000101", [5, 7, 40]),
        _draw("115000100", [8, 9, 60]),
    ]

    record = build_analysis_record(current, recent_draws=recent)
    data = record["ai_score"]["multi_window_hot_cold"]
    learning = record["ai_score"]["learning_features"]

    assert set(data["windows"]) == {"10", "20", "50", "100"}
    assert data["shadow_only"] is True
    assert learning["multi_window_hot_candidates"] == data["candidate_numbers"]
    assert learning["multi_window_rising_numbers"] == data["rising_numbers"]
    assert "multi_window_hot_cold" not in record


def test_analysis_record_filters_test_current_and_future_recent_draws():
    current = _draw("115000103", [5, 6, 20])
    recent = [
        {"issue": "991000001", "numbers": [1, 2], "source": "phase-test"},
        _draw("115000104", [70, 71]),
        _draw("115000103", [5, 6]),
        _draw("115000102", [5, 6, 30]),
        _draw("115000101", [5, 7, 40]),
    ]

    record = build_analysis_record(current, recent_draws=recent)
    data = record["ai_score"]["multi_window_hot_cold"]

    assert data["reference_issues"] == ["115000102", "115000101"]
    assert data["windows"]["10"]["available_draws"] == 2


def test_recent_draws_uses_official_history(monkeypatch):
    calls = []

    def fake_official_history(limit):
        calls.append(limit)
        return [_draw("115000102", [1, 2])]

    monkeypatch.setattr(
        "database.official_draw_store.get_official_draw_history",
        fake_official_history,
    )

    assert analysis_store._recent_draws(120) == [_draw("115000102", [1, 2])]
    assert calls == [120]
