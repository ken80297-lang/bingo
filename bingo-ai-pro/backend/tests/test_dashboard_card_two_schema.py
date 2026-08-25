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
        "big_small": "small",
        "odd_even": "even",
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
    monkeypatch.setattr(player_dashboard, "_card_two_analysis_by_issue", lambda issue: {"issue": issue})
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
    assert payload["size_result"] == {
        "predicted": "small",
        "actual": "small",
        "hit": True,
        "status_text": "命中",
    }
    assert payload["odd_even_result"] == {
        "predicted": "even",
        "actual": "even",
        "hit": True,
        "status_text": "命中",
    }
    assert payload["actual_consecutive_groups"]["three_star"] == [[7, 8, 9]]
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


def test_card_two_selector_uses_requested_previous_issue_only():
    records = [
        _record(id=1, prediction_issue="115040901", issue="115040900"),
        _record(id=2, prediction_issue="115040902", issue="115040901"),
        _record(id=3, prediction_issue="115040903", issue="115040902"),
    ]

    selected = player_dashboard.get_latest_finalized_analysis_report(records, {"issue": "115040904"}, "115040902")

    assert selected["id"] == 2
    assert selected["prediction_issue"] == "115040902"
    assert player_dashboard.get_latest_finalized_analysis_report(records, {"issue": "115040904"}, "115040904") is None


def test_card_two_selector_allows_requested_issue_equal_latest_official_issue():
    records = [
        _record(id=1, prediction_issue="115040904", issue="115040903"),
        _record(id=2, prediction_issue="115040905", issue="115040904"),
    ]

    selected = player_dashboard.get_latest_finalized_analysis_report(records, {"issue": "115040904"}, "115040904")

    assert selected["id"] == 1
    assert selected["prediction_issue"] == "115040904"


def test_card_two_accepts_actual_numbers_alias(monkeypatch):
    record = _record(
        prediction_issue="115040904",
        issue="115040903",
        winning_numbers=[],
        actual_numbers=list(range(1, 21)),
        recommend_numbers=list(range(1, 21)),
    )
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "_card_two_analysis_by_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", lambda analysis, prediction: {"rules": []})

    selected = player_dashboard.get_latest_finalized_analysis_report([record], {"issue": "115040904"}, "115040904")
    payload = player_dashboard._card_two_from_record(selected, {"issue": "115040904"}, "115040904")

    assert selected is record
    assert payload["available"] is True
    assert payload["official_numbers"] == list(range(1, 21))
    assert payload["hit_count"] == 20


def test_card_two_skips_official_lookup_when_record_has_actual_and_super(monkeypatch):
    record = _record(
        winning_numbers=list(range(1, 21)),
        actual_super=5,
        official_super_number=None,
        recommend_numbers=list(range(1, 21)),
    )
    monkeypatch.setattr(
        player_dashboard,
        "get_official_draw_by_issue",
        lambda issue: (_ for _ in ()).throw(AssertionError("official draw lookup should be skipped")),
    )
    monkeypatch.setattr(player_dashboard, "_card_two_analysis_by_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", lambda analysis, prediction: {"rules": []})

    payload = player_dashboard._card_two_from_record(record, {"issue": "115040902"})

    assert payload["available"] is True
    assert payload["super_number"] == 5
    assert payload["super_number_hit"] is True
    assert payload["hit_count"] == 20


def test_card_two_empty_payload_keeps_requested_previous_issue():
    payload = player_dashboard._card_two_from_record(None, {"issue": "115040904"}, "115040903")

    assert payload["available"] is False
    assert payload["issue"] == "115040903"
    assert payload["requested_issue"] == "115040903"
    assert payload["status_text"] == "尚無已完成的最終分析報告"


def test_card_two_from_record_rejects_record_for_different_requested_issue():
    payload = player_dashboard._card_two_from_record(
        _record(prediction_issue="115040904", issue="115040903"),
        {"issue": "115040904"},
        "115040903",
    )

    assert payload["available"] is False
    assert payload["issue"] == "115040903"
    assert payload["requested_issue"] == "115040903"


def test_card_two_actual_consecutive_groups_include_real_number_combinations():
    groups = player_dashboard._card_two_actual_consecutive_groups([1, 2, 3, 4, 5, 6, 12, 13, 14])

    assert groups["three_star"] == [[1, 2, 3], [2, 3, 4], [3, 4, 5], [4, 5, 6], [12, 13, 14]]
    assert groups["four_star"] == [[1, 2, 3, 4], [2, 3, 4, 5], [3, 4, 5, 6]]
    assert groups["five_star"] == [[1, 2, 3, 4, 5], [2, 3, 4, 5, 6]]
    assert groups["six_star"] == [[1, 2, 3, 4, 5, 6]]


def test_card_two_super_number_data_insufficient(monkeypatch):
    record = _record()
    monkeypatch.setattr(
        player_dashboard,
        "get_official_draw_by_issue",
        lambda issue: {"issue": issue, "numbers": record["winning_numbers"], "super_number": None},
    )
    monkeypatch.setattr(player_dashboard, "_card_two_analysis_by_issue", lambda issue: None)
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
    monkeypatch.setattr(player_dashboard, "_card_two_analysis_by_issue", lambda issue: {"issue": issue})

    def broken_snapshot(analysis, prediction):
        raise RuntimeError("boom")

    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", broken_snapshot)

    payload = player_dashboard._card_two_from_record(record, {"issue": "115040902"})

    assert payload["available"] is True
    assert payload["rules"] == []
