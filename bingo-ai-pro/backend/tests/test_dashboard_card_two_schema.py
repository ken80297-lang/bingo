from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import player_dashboard


def _record(**overrides):
    payload = {
        "id": 1,
        "issue": "115040900",
        "prediction_issue": "115040901",
        "prediction_status": "verified",
        "recommend_numbers": [5, 1, 5, 8, 12, 17, 21, 24, 30, 33, 38, 42, 46, 51, 57, 61, 65, 69, 72, 76, 80, 99],
        "winning_numbers": [1, 3, 5, 7, 8, 9, 13, 16, 22, 24, 31, 36, 40, 44, 50, 52, 61, 64, 71, 80],
        "matched_numbers": [1, 5, 8, 24, 61, 80],
        "prediction_count": 20,
        "super_number": 7,
        "super_number_hit": False,
        "learning_used": True,
        "verified_at": "2026-07-30T01:00:00+00:00",
        "updated_at": "2026-07-30T01:05:00+00:00",
        "production_valid": True,
        "strategy": "v7",
        "source": "production_history",
    }
    payload.update(overrides)
    return payload


def test_card_two_schema_builds_finalized_report(monkeypatch):
    record = _record()

    monkeypatch.setattr(
        player_dashboard,
        "get_official_draw_by_issue",
        lambda issue: {"issue": issue, "numbers": record["winning_numbers"], "super_number": 8},
    )
    monkeypatch.setattr(player_dashboard, "get_analysis_history_by_issue", lambda issue: {"issue": issue})
    monkeypatch.setattr(
        player_dashboard,
        "_rule_snapshot_for_dashboard",
        lambda analysis, prediction: {
            "rules": [
                {"key": "cold", "label": "冷門", "candidate_numbers": [77]},
                {"key": "hot", "label": "熱門", "candidate_numbers": [5, 17, 24], "score": 0.85},
                {"key": "cluster_aftershock_recovery", "label": "群聚後連號回補", "candidate_numbers": []},
                {"key": "super_number_trajectory_recovery", "label": "超獎軌跡回補", "candidate_numbers": [8, 9]},
            ]
        },
    )

    payload = player_dashboard._card_two_from_record(record, {"issue": "115040902"})

    assert payload["title"] == "📖 AI 驗證與分析報告"
    assert payload["available"] is True
    assert payload["report_status"] == "finalized"
    assert payload["status_text"] == "最終分析"
    assert payload["issue"] == "115040901"
    assert payload["prediction_numbers"] == [1, 5, 8, 12, 17, 21, 24, 30, 33, 38, 42, 46, 51, 57, 61, 65, 69, 72, 76, 80]
    assert payload["matched_numbers"] == [1, 5, 8, 24, 61, 80]
    assert payload["hit_count"] == 6
    assert payload["prediction_count"] == 20
    assert payload["super_number"] == 8
    assert payload["super_number_hit"] is True
    assert payload["super_number_status_text"] == "命中"
    assert [rule["rule_name_zh"] for rule in payload["rules"]] == ["熱門", "冷門", "超獎軌跡回補"]
    assert payload["rules"][0]["matched_numbers"] == [5, 24]
    assert payload["rules"][0]["status_text"] == "成功"
    assert payload["rules"][0]["score_text"] == "85%"


def test_card_two_selector_only_uses_latest_valid_finalized_report():
    records = [
        _record(id=1, prediction_issue="115040901", issue="115040900"),
        _record(id=2, prediction_issue="115040902", issue="115040901", prediction_status="waiting_draw"),
        _record(id=3, prediction_issue="115040903", issue="115040902", strategy="test finalized"),
        _record(id=4, prediction_issue="115040904", issue="115040903", learning_used=False),
        _record(id=5, prediction_issue="115040905", issue="115040904"),
    ]

    selected = player_dashboard.get_latest_finalized_analysis_report(records, {"issue": "115040906"})

    assert selected["id"] == 5
    assert selected["prediction_issue"] == "115040905"


def test_card_two_super_number_data_insufficient(monkeypatch):
    record = _record()
    monkeypatch.setattr(
        player_dashboard,
        "get_official_draw_by_issue",
        lambda issue: {"issue": issue, "numbers": record["winning_numbers"], "super_number": None},
    )
    monkeypatch.setattr(player_dashboard, "get_analysis_history_by_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", lambda analysis, prediction: {"rules": []})

    payload = player_dashboard._card_two_from_record(record, {"issue": "115040902"})

    assert payload["super_number_hit"] is None
    assert payload["super_number_status_text"] == "資料不足"


def test_card_two_rule_snapshot_failure_keeps_report_available(monkeypatch):
    record = _record()
    monkeypatch.setattr(
        player_dashboard,
        "get_official_draw_by_issue",
        lambda issue: {"issue": issue, "numbers": record["winning_numbers"], "super_number": 8},
    )
    monkeypatch.setattr(player_dashboard, "get_analysis_history_by_issue", lambda issue: {"issue": issue})

    def broken_snapshot(analysis, prediction):
        raise RuntimeError("boom")

    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", broken_snapshot)

    payload = player_dashboard._card_two_from_record(record, {"issue": "115040902"})

    assert payload["available"] is True
    assert payload["rules"] == []
