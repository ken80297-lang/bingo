from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.analysis_store import (
    _consecutive_extension,
    _neighbor_extension,
    _parity_size_trend,
    _tail_trend_strength,
    _zone_cluster_strength,
    build_analysis_record,
)


def _draw(issue: str, numbers: list[int]) -> dict:
    return {"issue": issue, "numbers": numbers, "source": "taiwan_lottery"}


def test_neighbor_extension_wraps_80_and_1():
    current = _draw("115000102", [40])
    recent = [_draw("115000101", [80, 1])]
    result = _neighbor_extension(current, recent, lookback=1)
    assert result["circular"] is True
    assert 1 in result["candidate_numbers"]
    assert 80 in result["candidate_numbers"]
    assert result["shadow_only"] is True


def test_neighbor_extension_excludes_current_issue():
    current = _draw("115000102", [40])
    recent = [_draw("115000102", [40]), _draw("115000101", [10])]
    result = _neighbor_extension(current, recent, lookback=10)
    evidence = [e for values in result["evidence"].values() for e in values]
    assert all(e["source"] != 40 for e in evidence)


def test_parity_size_trend_builds_state_and_candidates():
    current = _draw("115000103", [1])
    recent = [_draw("115000102", [41, 43, 45, 47]), _draw("115000101", [42, 44, 46, 48])]
    result = _parity_size_trend(current, recent)
    assert result["trend_state"]["size"] == "big"
    assert all(number >= 41 for number in result["candidate_numbers"])
    assert result["shadow_only"] is True


def test_zone_cluster_strength_uses_eight_decade_zones():
    current = _draw("115000103", [1])
    recent = [_draw("115000102", [71, 72, 73, 74]), _draw("115000101", [1, 2])]
    result = _zone_cluster_strength(current, recent)
    assert len(result["zone_counts"]) == 8
    assert result["hot_zones"][0] == "71-80"
    assert set(range(71, 81)).issubset(set(result["candidate_numbers"]))


def test_consecutive_extension_wraps_boundary_candidates():
    current = _draw("115000103", [1])
    recent = [_draw("115000102", [1, 2, 3]), _draw("115000101", [78, 79, 80])]
    result = _consecutive_extension(current, recent)
    assert result["circular"] is True
    assert 80 in result["candidate_numbers"]
    assert 1 in result["candidate_numbers"]


def test_tail_trend_strength_detects_hot_tail():
    current = _draw("115000103", [1])
    recent = [_draw("115000102", [1, 11, 21, 31]), _draw("115000101", [2, 12])]
    result = _tail_trend_strength(current, recent)
    assert result["hot_tails"][0] == 1
    assert all(1 <= number <= 80 for number in result["candidate_numbers"])
    assert result["shadow_only"] is True


def test_analysis_record_exposes_modules_4_to_8_only_under_ai_score():
    current = _draw("115000104", [1, 2, 41, 51])
    recent = [_draw("115000103", [80, 1, 2, 41]), _draw("115000102", [10, 11, 12, 42])]
    record = build_analysis_record(current, recent_draws=recent)
    ai = record["ai_score"]
    learning = ai["learning_features"]
    for key in ("neighbor_extension", "parity_size_trend", "zone_cluster_strength", "consecutive_extension", "tail_trend_strength"):
        assert ai[key]["shadow_only"] is True
        assert key not in record
    assert learning["neighbor_extension_candidates"] == ai["neighbor_extension"]["candidate_numbers"]
    assert learning["zone_cluster_candidates"] == ai["zone_cluster_strength"]["candidate_numbers"]
