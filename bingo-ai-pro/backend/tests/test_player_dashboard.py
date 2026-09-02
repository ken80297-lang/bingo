from __future__ import annotations

import pathlib
import sys
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import player_dashboard


def _reset_dashboard_state() -> None:
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    for key, value in list(player_dashboard._PLAYER_COMPONENT_CACHE.items()):
        player_dashboard._PLAYER_COMPONENT_CACHE[key] = [] if isinstance(value, list) else None
    player_dashboard._PLAYER_COMPONENT_CACHE["prediction_aggregates"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["analysis"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["kuaishou"] = {}
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()
    for key in player_dashboard._PLAYER_RUNTIME_METRICS:
        player_dashboard._PLAYER_RUNTIME_METRICS[key] = 0


@pytest.fixture(autouse=True)
def reset_dashboard_state_fixture():
    _reset_dashboard_state()
    yield
    _reset_dashboard_state()


def _official_draw() -> dict:
    return {
        "issue": "115040900",
        "draw_time": None,
        "numbers": list(range(1, 21)),
        "super_number": 7,
        "verified": False,
        "verification_status": "validated",
        "source_scope": "production",
    }


def _prediction() -> dict:
    return {
        "issue": "115040900",
        "prediction_issue": "115040901",
        "recommend_numbers": list(range(1, 21)),
        "confidence_percent": 75,
        "prediction_status": "pending",
        "production_generation": 2,
        "production_valid": True,
        "strategy": "production",
    }


def test_player_dashboard_component_timeout_defaults():
    assert player_dashboard.PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS == 2.0
    assert player_dashboard.PLAYER_DASHBOARD_OPTIONAL_TIMEOUT_SECONDS == 1.0
    assert player_dashboard.PLAYER_DASHBOARD_TOTAL_BUDGET_SECONDS == 4.5


def test_player_summary_fast_path_builds_from_isolated_dependencies(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_learned_live_target_count", lambda: 0, raising=False)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_current_release", lambda: {})
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", lambda: {})

    start = time.perf_counter()
    payload = player_dashboard.build_player_dashboard_summary()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 5000
    assert payload["status"] == "ok"
    assert payload["partial"] is False
    assert payload["current_draw"]["issue"] == "115040900"
    assert payload["latest_official_draw"]["draw_time"] is None
    assert payload["latest_official_draw"]["verification_status"] == "unknown"
    assert payload["next_prediction"]["prediction_issue"] == "115040901"
    assert len(payload["next_prediction"]["recommend_numbers"]) == 20
    assert payload["stale_steps"] == []


def test_player_summary_returns_fast_when_official_future_is_blocked(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS", 0.01)
    blocked = Future()
    submitted = []

    def fake_submit(name, fn):
        submitted.append(name)
        return blocked, "submitted"

    monkeypatch.setattr(player_dashboard, "_submit_component", fake_submit)

    start = time.perf_counter()
    payload = player_dashboard.build_player_dashboard_summary()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 5000
    assert submitted == ["official_draw", "kuaishou"]
    assert payload["status"] == "ok"
    assert payload["current_draw"] is None
    assert payload["timeout_steps"] == ["official_draw", "kuaishou"]
    assert player_dashboard._PLAYER_SUMMARY_CACHE["payload"] is None


def test_player_summary_does_not_submit_second_task_when_component_busy(monkeypatch):
    _reset_dashboard_state()
    busy = Future()
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT["official_draw"] = busy
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: (_ for _ in ()).throw(AssertionError("must not submit")))
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)

    payload = player_dashboard.build_player_dashboard_summary()

    assert payload["status"] == "ok"
    assert payload["skipped_busy_steps"] == ["official_draw"]
    assert player_dashboard.player_dashboard_runtime_metrics()["in_flight_count"] >= 1


def test_player_summary_repeated_busy_refreshes_do_not_grow_in_flight(monkeypatch):
    _reset_dashboard_state()
    busy = Future()
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT["official_draw"] = busy
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", lambda: (_ for _ in ()).throw(AssertionError("must not submit")))

    for _ in range(20):
        player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
        player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
        payload = player_dashboard.build_player_dashboard_summary()
        assert payload["status"] == "ok"

    metrics = player_dashboard.player_dashboard_runtime_metrics()
    assert 1 <= metrics["in_flight_count"] <= 3
    assert metrics["skipped_busy_count"] >= 20


def test_player_summary_prediction_timeout_uses_waiting_schema(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    blocked = Future()
    original_submit = player_dashboard._submit_component

    def fake_submit(name, fn):
        if name == "next_prediction_snapshot":
            return blocked, "submitted"
        return original_submit(name, fn)

    monkeypatch.setattr(player_dashboard, "_submit_component", fake_submit)

    payload = player_dashboard.build_player_dashboard_summary()

    assert payload["status"] == "ok"
    assert payload["next_prediction"]["status"] == "prediction_pending"
    assert payload["next_prediction"]["prediction_issue"] == "115040901"
    assert "next_prediction_snapshot" in payload["timeout_steps"]


def test_player_summary_late_component_result_populates_cache(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS", 0.01)

    def slow_official_draw():
        time.sleep(0.05)
        return _official_draw()

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", slow_official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: {"issue": "115040900"})
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)

    payload = player_dashboard.build_player_dashboard_summary()
    assert payload["current_draw"] is None
    assert player_dashboard._PLAYER_SUMMARY_CACHE["payload"] is None

    deadline = time.time() + 1
    while time.time() < deadline and player_dashboard._PLAYER_COMPONENT_CACHE.get("official_draw") is None:
        time.sleep(0.01)

    cached = player_dashboard._PLAYER_COMPONENT_CACHE.get("official_draw")
    assert cached["issue"] == "115040900"
