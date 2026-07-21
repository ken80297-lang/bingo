from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import player_dashboard


def test_dashboard_previous_prediction_uses_based_on_direct_lookup(monkeypatch):
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0

    latest_draw = {
        "issue": "115040801",
        "draw_time": "2026-07-17T15:35:08+08:00",
        "numbers": list(range(21, 41)),
        "super_number": 7,
        "created_at": "2026-07-17T15:35:15+08:00",
    }
    next_prediction = {
        "issue": "115040801",
        "prediction_issue": "115040802",
        "predict_time": "2026-07-17T15:35:20+08:00",
        "recommend_numbers": list(range(41, 61)),
        "prediction_status": "waiting_draw",
        "source": "official_collector",
        "trigger": "official_draw_saved",
    }
    previous_prediction = {
        "issue": "115040800",
        "prediction_issue": "115040801",
        "predict_time": "2026-07-17T15:30:20+08:00",
        "recommend_numbers": list(range(1, 21)),
        "winning_numbers": list(range(11, 31)),
        "matched_numbers": list(range(11, 21)),
        "missed_numbers": list(range(1, 11)),
        "hit_count": 10,
        "prediction_count": 20,
        "super_number": 7,
        "super_number_hit": True,
        "prediction_status": "verified",
        "verified_at": "2026-07-17T15:36:00+08:00",
        "learning_used": True,
        "learned_at": "2026-07-17T15:36:10+08:00",
        "source": "official_collector",
        "trigger": "official_draw_saved",
    }

    def official_by_issue(issue):
        if str(issue) == "115040801":
            return latest_draw
        return None

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: latest_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_history", lambda: next_prediction)
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_history_statistics", lambda limit=100: {"status": "ok", "sample_size": 0})
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", official_by_issue)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: previous_prediction if str(issue) == "115040801" else None)

    payload = player_dashboard.build_player_dashboard_summary()
    next_payload = payload["next_prediction"]
    previous = payload["previous_verification"]

    assert next_payload["based_on_draw_time"] == "2026/07/17 15:35:08"
    assert next_payload["status"] == "waiting_draw"
    assert next_payload["stale_status"] == "normal"
    assert next_payload["lag_issues"] == 0
    assert len(next_payload["recommend_numbers"]) == 20

    assert previous["target_issue"] == "115040801"
    assert len(previous["predicted_numbers"]) == 20
    assert len(previous["official_numbers"]) == 20
    assert previous["matched_numbers"] == list(range(11, 21))
    assert previous["missed_numbers"] == list(range(1, 11))
    assert previous["prediction_status"] == "verified"
    assert previous["verification_status"] == "verified"
    assert previous["learning_used"] is True


def test_dashboard_uses_official_draw_saved_event_time_fallback(monkeypatch):
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0

    latest_draw = {
        "issue": "115040821",
        "draw_time": None,
        "numbers": list(range(21, 41)),
        "super_number": 7,
        "created_at": "2026-07-17T15:35:15+08:00",
    }
    next_prediction = {
        "issue": "115040821",
        "prediction_issue": "115040822",
        "predict_time": "2026-07-17T15:35:20+08:00",
        "recommend_numbers": list(range(41, 61)),
        "prediction_status": "waiting_draw",
    }

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: latest_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_history", lambda: next_prediction)
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_history_statistics", lambda limit=100: {"status": "ok", "sample_size": 0})
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", lambda issue: latest_draw if str(issue) == "115040821" else None)
    monkeypatch.setattr(
        player_dashboard,
        "get_latest_operation_event",
        lambda event_type, issue=None: {"created_at": "2026-07-17T15:35:12+08:00"}
        if event_type == "official_draw_saved" and str(issue) == "115040821"
        else None,
    )
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)

    payload = player_dashboard.build_player_dashboard_summary()
    next_payload = payload["next_prediction"]

    assert next_payload["based_on_draw_time"] == "2026/07/17 15:35:12"
    assert next_payload["based_on_time_source"] == "official_draw_saved_event"
    assert next_payload["based_on_draw_exists"] is True


def test_dashboard_previous_prediction_falls_back_to_latest_available_verified(monkeypatch):
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0

    latest_draw = {
        "issue": "115040841",
        "draw_time": "2026-07-17T15:35:08+08:00",
        "numbers": list(range(21, 41)),
        "super_number": 7,
    }
    fallback_draw = {
        "issue": "115040839",
        "draw_time": "2026-07-17T15:25:08+08:00",
        "numbers": list(range(11, 31)),
        "super_number": 9,
    }
    next_prediction = {
        "issue": "115040841",
        "prediction_issue": "115040842",
        "predict_time": "2026-07-17T15:35:20+08:00",
        "recommend_numbers": list(range(41, 61)),
        "prediction_status": "waiting_draw",
    }
    fallback_prediction = {
        "issue": "115040838",
        "prediction_issue": "115040839",
        "predict_time": "2026-07-17T15:20:20+08:00",
        "recommend_numbers": list(range(1, 21)),
        "winning_numbers": list(range(11, 31)),
        "matched_numbers": list(range(11, 21)),
        "missed_numbers": list(range(1, 11)),
        "prediction_status": "verified",
        "verified_at": "2026-07-17T15:26:00+08:00",
        "learning_used": True,
    }

    def official_by_issue(issue):
        if str(issue) == "115040841":
            return latest_draw
        if str(issue) == "115040839":
            return fallback_draw
        return None

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: latest_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_history", lambda: next_prediction)
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_history_statistics", lambda limit=100: {"status": "ok", "sample_size": 0})
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", official_by_issue)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: fallback_prediction)

    previous = player_dashboard.build_player_dashboard_summary()["previous_verification"]

    assert previous["previous_result_mode"] == "latest_available_verified"
    assert previous["requested_target_issue"] == "115040841"
    assert previous["displayed_target_issue"] == "115040839"
    assert len(previous["predicted_numbers"]) == 20
    assert len(previous["official_numbers"]) == 20
    assert previous["verification_status"] == "verified"
    assert previous["learning_used"] is True
