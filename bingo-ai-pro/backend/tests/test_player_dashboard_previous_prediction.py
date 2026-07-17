from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import player_dashboard


def test_dashboard_previous_prediction_uses_based_on_direct_lookup(monkeypatch):
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0

    latest_draw = {
        "issue": "115000101",
        "draw_time": "2026-07-17T15:35:08+08:00",
        "numbers": list(range(21, 41)),
        "super_number": 7,
        "created_at": "2026-07-17T15:35:15+08:00",
    }
    next_prediction = {
        "issue": "115000101",
        "prediction_issue": "115000102",
        "predict_time": "2026-07-17T15:35:20+08:00",
        "recommend_numbers": list(range(41, 61)),
        "prediction_status": "waiting_draw",
        "source": "official_collector",
        "trigger": "official_draw_saved",
    }
    previous_prediction = {
        "issue": "115000100",
        "prediction_issue": "115000101",
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
        if str(issue) == "115000101":
            return latest_draw
        return None

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: latest_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_history", lambda: next_prediction)
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_history_statistics", lambda limit=100: {"status": "ok", "sample_size": 0})
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", official_by_issue)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: previous_prediction if str(issue) == "115000101" else None)

    payload = player_dashboard.build_player_dashboard_summary()
    next_payload = payload["next_prediction"]
    previous = payload["previous_verification"]

    assert next_payload["based_on_draw_time"] == "2026/07/17 15:35:08"
    assert next_payload["status"] == "waiting_draw"
    assert next_payload["stale_status"] == "normal"
    assert next_payload["lag_issues"] == 0
    assert len(next_payload["recommend_numbers"]) == 20

    assert previous["target_issue"] == "115000101"
    assert len(previous["predicted_numbers"]) == 20
    assert len(previous["official_numbers"]) == 20
    assert previous["matched_numbers"] == list(range(11, 21))
    assert previous["missed_numbers"] == list(range(1, 11))
    assert previous["prediction_status"] == "verified"
    assert previous["verification_status"] == "verified"
    assert previous["learning_used"] is True
