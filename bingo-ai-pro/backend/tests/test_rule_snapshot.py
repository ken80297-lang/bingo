from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import rule_snapshot
from services.rule_snapshot import (
    build_rule_snapshot,
    build_rule_snapshot_health,
    generate_rule_snapshot_for_issue,
    get_rule_registry,
)
from services import player_dashboard


def _analysis() -> dict:
    return {
        "issue": "115000100",
        "numbers": [3, 4, 5, 6, 7, 8],
        "hot_numbers": [1, 2, 3],
        "cold_numbers": [78, 79, 80],
        "missing_numbers": list(range(11, 31)),
        "repeated_numbers": [5, 6],
        "difference_values": {"1": [7, 8], "-1": [9]},
        "diagonal_pattern": [[10, 19], [20, 31], [32, 43], [44, 55], [56, 67], [68, 79], [70, 80]],
        "diagonal_score": 24,
        "gap_score": 6,
        "cluster_level": "中型群聚",
        "cluster_score": 64,
        "tail_distribution": {"1": 3, "2": 2},
        "hot_zone": ["01-10"],
        "cold_zone": ["71-80"],
        "patch_numbers": [13, 14],
        "twins": [[22, 24]],
        "consecutive": [[30, 31]],
        "three_star": [[1, 2, 3]],
        "four_star": [[1, 2, 3, 4]],
        "laowanjia_score": 72.5,
        "laowanjia_score_detail": {"hot": 2},
        "ai_score": {
            "super_number_trajectory_recovery": {
                "confidence": 70,
                "candidate_numbers": [40, 41],
            },
            "cluster_aftershock_recovery": {
                "confidence": 66,
                "candidate_numbers": [15, 16],
            },
        },
    }


def test_build_rule_snapshot_normalizes_analysis_rules():
    snapshot = build_rule_snapshot(
        _analysis(),
        {"prediction_issue": "115000101", "recommend_numbers": [1, 4, 8, 72, 79]},
        generated_at="2026-07-22T00:00:00",
        rule_library_version="test",
    )

    assert snapshot["source_issue"] == "115000100"
    assert snapshot["target_issue"] == "115000101"
    assert snapshot["rule_library_version"] == "test"
    assert snapshot["aggregate"]["total_count"] == len(get_rule_registry())
    assert snapshot["aggregate"]["completed_count"] > 15
    assert snapshot["fast_path_sources"]["missing_numbers"] == list(range(11, 31))
    assert snapshot["fast_path_sources"]["diagonal_pattern"] == [
        10,
        19,
        20,
        31,
        32,
        43,
        44,
        55,
        56,
        67,
        68,
        79,
        70,
        80,
    ]
    assert snapshot["fast_path_sources"]["latest_draw_numbers"] == [3, 4, 5, 6, 7, 8]

    rules = {item["key"]: item for item in snapshot["rules"]}
    assert rules["diagonal"]["score"] == 24
    assert rules["diagonal"]["candidate_numbers"] == [10, 19, 20, 31, 32, 43, 44, 55, 56, 67, 68, 79]
    assert rules["missing"]["candidate_numbers"] == list(range(11, 23))
    assert rules["hot_zone"]["candidate_numbers"] == [1, 4, 8]
    assert rules["cold_zone"]["candidate_numbers"] == [72, 79]
    assert rules["three_star"]["candidate_numbers"] == [1, 2, 3]
    assert rules["super"]["warnings"]


def test_dashboard_only_rules_are_marked_experimental():
    snapshot = build_rule_snapshot(_analysis(), generated_at="2026-07-22T00:00:00")
    rules = {item["key"]: item for item in snapshot["rules"]}

    assert rules["ladder"]["status"] == "experimental"
    assert rules["momentum"]["source_fields"] == []
    assert rules["momentum"]["candidate_numbers"] == []


def test_player_dashboard_rule_library_uses_snapshot_shape():
    payload = player_dashboard._rule_library(
        _analysis(),
        {"prediction_issue": "115000101", "main_numbers": [1, 4, 8, 72, 79]},
    )

    assert set(payload) >= {
        "title",
        "completed_count",
        "total_count",
        "summary",
        "primary_rules",
        "rules",
        "laowanjia_index",
        "hot_zones",
        "cold_zone",
        "star_prediction",
        "super_trajectory",
        "cluster_recovery",
    }
    rules = {item["key"]: item for item in payload["rules"]}
    assert set(rules["hot"]) == {
        "key",
        "name",
        "status",
        "score",
        "confidence",
        "reason",
        "impact",
        "candidate_numbers",
    }
    assert rules["ladder"]["status"] == "experimental"
    assert rules["ladder"]["impact"] == "資料不足"
    assert rules["hot_zone"]["candidate_numbers"] == [1, 4, 8]
    assert payload["completed_count"] == sum(1 for item in payload["rules"] if item["status"] == "ready")


def test_player_dashboard_rule_library_prefers_stored_snapshot(monkeypatch):
    stored = build_rule_snapshot(
        {"issue": "115040800", "hot_numbers": [70], "missing_numbers": [71]},
        {"prediction_issue": "115040801", "main_numbers": [70, 71]},
        generated_at="2026-07-22T00:00:00+00:00",
        rule_library_version="stored-test",
    )
    calls = []

    def fake_get_rule_snapshot(**filters):
        calls.append(filters)
        return {"snapshot_json": stored}

    monkeypatch.setattr(player_dashboard, "get_rule_snapshot", fake_get_rule_snapshot)
    monkeypatch.setattr(
        player_dashboard,
        "build_rule_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback should not build")),
    )

    payload = player_dashboard._rule_library(
        {"issue": "115040800", "hot_numbers": [1]},
        {"prediction_issue": "115040801", "main_numbers": [1]},
    )
    rules = {item["key"]: item for item in payload["rules"]}

    assert calls == [{"source_issue": "115040800", "target_issue": "115040801"}]
    assert rules["hot"]["candidate_numbers"] == [70]
    assert rules["ladder"]["status"] == "experimental"


def test_player_dashboard_rule_library_falls_back_to_live_snapshot(monkeypatch):
    calls = []

    def fake_build_rule_snapshot(analysis, prediction, **kwargs):
        calls.append({"analysis": analysis, "prediction": prediction, "kwargs": kwargs})
        return build_rule_snapshot(
            analysis,
            prediction,
            generated_at="2026-07-22T00:00:00+00:00",
            rule_library_version="fallback-test",
            **kwargs,
        )

    monkeypatch.setattr(player_dashboard, "get_rule_snapshot", lambda **filters: None)
    monkeypatch.setattr(player_dashboard, "build_rule_snapshot", fake_build_rule_snapshot)

    payload = player_dashboard._rule_library(
        {"issue": "115040800", "hot_numbers": [2], "missing_numbers": [3]},
        {"prediction_issue": "115040801", "main_numbers": [2, 3]},
    )
    rules = {item["key"]: item for item in payload["rules"]}

    assert calls
    assert calls[0]["kwargs"] == {"source_issue": "115040800", "target_issue": "115040801"}
    assert rules["hot"]["candidate_numbers"] == [2]
    assert rules["ladder"]["status"] == "experimental"


def test_generate_rule_snapshot_for_issue_persist_false_does_not_save(monkeypatch):
    saves = []
    monkeypatch.setattr(rule_snapshot, "get_analysis_history_by_issue", lambda issue: _analysis())
    monkeypatch.setattr(rule_snapshot, "get_prediction_for_source_target", lambda source, target: {"prediction_issue": target, "recommend_numbers": [1, 4, 8]})
    monkeypatch.setattr(rule_snapshot, "save_rule_snapshot", lambda snapshot: saves.append(snapshot) or {"status": "ok"})

    result = generate_rule_snapshot_for_issue("115000100", "115000101", persist=False)

    assert result["status"] == "ok"
    assert result["persisted"] is False
    assert result["saved"]["reason"] == "persist_false"
    assert result["prediction_found"] is True
    assert result["snapshot"]["source_issue"] == "115000100"
    assert result["snapshot"]["target_issue"] == "115000101"
    assert saves == []


def test_generate_rule_snapshot_for_issue_persist_true_saves(monkeypatch):
    saves = []
    monkeypatch.setattr(rule_snapshot, "get_analysis_history_by_issue", lambda issue: {**_analysis(), "issue": issue})
    monkeypatch.setattr(rule_snapshot, "get_prediction_for_source_target", lambda source, target: {"prediction_issue": target, "recommend_numbers": [1, 4, 8]})
    monkeypatch.setattr(rule_snapshot, "save_rule_snapshot", lambda snapshot: saves.append(snapshot) or {"status": "ok", "storage": "sqlite", "id": 7})

    result = generate_rule_snapshot_for_issue("115000100", "115000101")

    assert result["status"] == "ok"
    assert result["persisted"] is True
    assert result["saved"]["id"] == 7
    assert len(saves) == 1
    assert saves[0]["source_issue"] == "115000100"


def test_generate_rule_snapshot_for_issue_skips_when_analysis_missing(monkeypatch):
    monkeypatch.setattr(rule_snapshot, "get_analysis_history_by_issue", lambda issue: None)
    monkeypatch.setattr(
        rule_snapshot,
        "save_rule_snapshot",
        lambda snapshot: (_ for _ in ()).throw(AssertionError("missing analysis should not save")),
    )

    result = generate_rule_snapshot_for_issue("115000100", "115000101")

    assert result["status"] == "skipped"
    assert result["reason"] == "analysis_not_found"
    assert result["snapshot"] is None
    assert result["persisted"] is False


def test_generate_rule_snapshot_for_issue_without_prediction_still_builds(monkeypatch):
    saves = []
    monkeypatch.setattr(rule_snapshot, "get_analysis_history_by_issue", lambda issue: {**_analysis(), "issue": issue})
    monkeypatch.setattr(rule_snapshot, "get_prediction_for_source_target", lambda source, target: None)
    monkeypatch.setattr(rule_snapshot, "save_rule_snapshot", lambda snapshot: saves.append(snapshot) or {"status": "ok", "storage": "sqlite"})

    result = generate_rule_snapshot_for_issue("115000100", "115000101")

    assert result["status"] == "ok"
    assert result["prediction_found"] is False
    assert result["snapshot"]["source_issue"] == "115000100"
    assert result["snapshot"]["target_issue"] == "115000101"
    assert result["persisted"] is True
    assert saves


def _health_snapshot(
    source_issue: str = "115000100",
    target_issue: str = "115000101",
    version: str = "test",
    *,
    complete: bool = True,
) -> dict:
    rules = [
        {"key": "patch", "status": "ready", "candidate_numbers": [1]},
        {"key": "missing", "status": "ready", "candidate_numbers": [2]},
        {"key": "cold", "status": "ready", "candidate_numbers": [3]},
        {"key": "hot", "status": "ready", "candidate_numbers": [4]},
        {"key": "diagonal", "status": "ready", "candidate_numbers": [5]},
        {"key": "repeat", "status": "ready", "candidate_numbers": [6]},
        {"key": "latest_draw_numbers", "status": "ready", "candidate_numbers": [7]},
        {"key": "ladder", "status": "experimental", "candidate_numbers": []},
    ]
    snapshot = {
        "source_issue": source_issue,
        "target_issue": target_issue,
        "rule_library_version": version,
        "rules": rules,
        "aggregate": {"total_count": len(rules)},
    }
    if not complete:
        snapshot["aggregate"] = {"total_count": len(rules) + 1}
    return snapshot


def test_rule_snapshot_health_without_snapshots_is_critical(monkeypatch):
    monkeypatch.setattr(rule_snapshot, "get_analysis_history", lambda limit: [{"issue": "115000100"}])
    monkeypatch.setattr(rule_snapshot, "get_rule_snapshots", lambda limit: [])
    monkeypatch.setattr(rule_snapshot, "get_prediction_history_records", lambda limit: [])
    monkeypatch.setattr(
        rule_snapshot,
        "save_rule_snapshot",
        lambda snapshot: (_ for _ in ()).throw(AssertionError("health must not write")),
    )

    health = build_rule_snapshot_health(limit=10)

    assert health["status"] == "critical"
    assert health["analysis_count"] == 1
    assert health["snapshot_count"] == 0
    assert health["coverage_rate"] == 0
    assert health["missing_snapshot_issues"] == ["115000100"]
    assert "no_rule_snapshots" in health["warnings"]


def test_rule_snapshot_health_counts_complete_ready_snapshots(monkeypatch):
    monkeypatch.setattr(rule_snapshot, "get_analysis_history", lambda limit: [{"issue": "115000100"}])
    monkeypatch.setattr(
        rule_snapshot,
        "get_rule_snapshots",
        lambda limit: [{"source_issue": "115000100", "target_issue": "115000101", "rule_library_version": "test", "snapshot_json": _health_snapshot()}],
    )
    monkeypatch.setattr(rule_snapshot, "get_prediction_history_records", lambda limit: [{"issue": "115000100", "prediction_issue": "115000101"}])

    health = build_rule_snapshot_health(limit=10)

    assert health["status"] == "ok"
    assert health["coverage_rate"] == 100
    assert health["incomplete_snapshot_count"] == 0
    assert health["recommendation_ready_count"] == 1
    assert health["dashboard_ready_count"] == 1
    assert health["rule_library_versions"] == ["test"]


def test_rule_snapshot_health_counts_incomplete_snapshot_and_versions(monkeypatch):
    monkeypatch.setattr(rule_snapshot, "get_analysis_history", lambda limit: [{"issue": "115000100"}, {"issue": "115000101"}])
    monkeypatch.setattr(
        rule_snapshot,
        "get_rule_snapshots",
        lambda limit: [
            {"source_issue": "115000100", "target_issue": "115000101", "rule_library_version": "v1", "snapshot_json": _health_snapshot(version="v1", complete=False)},
            {"source_issue": "115000101", "target_issue": "115000102", "rule_library_version": "v2", "snapshot_json": _health_snapshot("115000101", "115000102", "v2")},
        ],
    )
    monkeypatch.setattr(rule_snapshot, "get_prediction_history_records", lambda limit: [])

    health = build_rule_snapshot_health(limit=10)

    assert health["status"] == "warning"
    assert health["coverage_rate"] == 100
    assert health["incomplete_snapshot_count"] == 1
    assert health["rule_library_versions"] == ["v1", "v2"]
    assert "incomplete_rule_snapshots" in health["warnings"]
    assert "mixed_rule_library_versions" in health["warnings"]
