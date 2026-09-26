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
    assert player_dashboard.PLAYER_DASHBOARD_AGGREGATE_TIMEOUT_SECONDS == 2.0
    assert player_dashboard.PLAYER_DASHBOARD_TOTAL_BUDGET_SECONDS == 4.5


def test_player_summary_fast_path_builds_from_isolated_dependencies(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_context", lambda **kwargs: {"draw": _official_draw(), "prediction": _prediction()})
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100, **kwargs: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda **kwargs: {})
    monkeypatch.setattr(player_dashboard, "get_learned_live_target_count", lambda: 0, raising=False)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)
    monkeypatch.setattr(
        player_dashboard,
        "get_previous_verification_summary_snapshot",
        lambda issue, *, include_metadata_lookup=True: {"record": None, "draw": None, "mode": "none"},
    )
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



def test_player_summary_skips_legacy_analysis_when_snapshot_summary_exists(monkeypatch):
    _reset_dashboard_state()
    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_context", lambda **kwargs: {"draw": _official_draw(), "prediction": _prediction()})
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100, **kwargs: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda **kwargs: {})
    monkeypatch.setattr(player_dashboard, "get_learned_live_target_count", lambda: 0, raising=False)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)
    monkeypatch.setattr(
        player_dashboard,
        "get_previous_verification_summary_snapshot",
        lambda issue, *, include_metadata_lookup=True: {"record": None, "draw": None, "mode": "none"},
    )
    monkeypatch.setattr(player_dashboard, "get_current_release", lambda: {})
    monkeypatch.setattr(
        player_dashboard,
        "get_latest_analysis_history",
        lambda: (_ for _ in ()).throw(AssertionError("legacy analysis must not be queried")),
    )
    monkeypatch.setattr(
        player_dashboard,
        "_rule_snapshot_for_dashboard",
        lambda analysis, prediction, **kwargs: {
            "rules": [],
            "aggregate": {},
            "dashboard_analysis_summary": {
                "laowanjia_score": 70,
                "hot_zone": ["01-10"],
                "cold_zone": ["71-80"],
                "three_star": None,
                "four_star": None,
                "five_star": None,
                "six_star": None,
                "super_number_trajectory_recovery": {},
                "cluster_aftershock_recovery": {},
            },
        },
    )

    payload = player_dashboard.build_player_dashboard_summary()

    assert payload["status"] == "ok"
    assert payload["rule_library"]["laowanjia_index"] == 70
    assert payload["rule_library"]["hot_zones"] == ["01-10"]
    assert all(step.get("step") != "analysis" for step in payload["timing"]["steps"])


def test_player_summary_uses_legacy_analysis_when_stored_snapshot_is_missing(monkeypatch):
    _reset_dashboard_state()
    calls = {"analysis": 0, "snapshot": 0}

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_context", lambda **kwargs: {"draw": _official_draw(), "prediction": _prediction()})
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100, **kwargs: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda **kwargs: {})
    monkeypatch.setattr(player_dashboard, "get_learned_live_target_count", lambda: 0, raising=False)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)
    monkeypatch.setattr(
        player_dashboard,
        "get_previous_verification_summary_snapshot",
        lambda issue, *, include_metadata_lookup=True: {"record": None, "draw": None, "mode": "none"},
    )
    monkeypatch.setattr(player_dashboard, "get_current_release", lambda: {})

    def missing_snapshot(**kwargs):
        calls["snapshot"] += 1
        return None

    monkeypatch.setattr(player_dashboard, "get_rule_snapshot", missing_snapshot)

    def legacy_analysis():
        calls["analysis"] += 1
        return {
            "issue": "115040900",
            "laowanjia_score": 63,
            "hot_zone": ["21-30"],
            "cold_zone": ["51-60"],
            "ai_score": {},
        }

    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", legacy_analysis)

    payload = player_dashboard.build_player_dashboard_summary()

    assert payload["status"] == "ok"
    assert calls["analysis"] == 1
    assert calls["snapshot"] == 1
    assert payload["rule_library"]["laowanjia_index"] == 63
    assert payload["rule_library"]["hot_zones"] == ["21-30"]


def test_player_summary_old_stored_snapshot_uses_legacy_analysis_once(monkeypatch):
    _reset_dashboard_state()
    calls = {"analysis": 0, "snapshot": 0}

    monkeypatch.setattr(player_dashboard, "get_latest_official_draw", _official_draw)
    monkeypatch.setattr(player_dashboard, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(player_dashboard, "get_prediction_for_source_target", lambda source, target: _prediction())
    monkeypatch.setattr(player_dashboard, "get_latest_prediction_context", lambda **kwargs: {"draw": _official_draw(), "prediction": _prediction()})
    monkeypatch.setattr(player_dashboard, "get_prediction_history_records", lambda limit=100, **kwargs: [])
    monkeypatch.setattr(player_dashboard, "get_prediction_lifecycle_aggregates", lambda **kwargs: {})
    monkeypatch.setattr(player_dashboard, "get_learned_live_target_count", lambda: 0, raising=False)
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: None)
    monkeypatch.setattr(player_dashboard, "get_latest_verified_prediction_at_or_before", lambda issue: None)
    monkeypatch.setattr(
        player_dashboard,
        "get_previous_verification_summary_snapshot",
        lambda issue, *, include_metadata_lookup=True: {"record": None, "draw": None, "mode": "none"},
    )
    monkeypatch.setattr(player_dashboard, "get_current_release", lambda: {})

    def old_snapshot(**kwargs):
        calls["snapshot"] += 1
        return {
            "snapshot_json": {
                "rules": [{"key": "hot", "label": "熱門", "status": "ready", "score": 80}],
                "aggregate": {"primary_rules": ["hot"]},
            }
        }

    def legacy_analysis():
        calls["analysis"] += 1
        return {
            "issue": "115040900",
            "laowanjia_score": 64,
            "hot_zone": ["31-40"],
            "cold_zone": ["41-50"],
            "ai_score": {},
        }

    monkeypatch.setattr(player_dashboard, "get_rule_snapshot", old_snapshot)
    monkeypatch.setattr(player_dashboard, "get_latest_analysis_history", legacy_analysis)

    payload = player_dashboard.build_player_dashboard_summary()

    assert payload["status"] == "ok"
    assert calls["analysis"] == 1
    assert calls["snapshot"] == 1
    assert payload["rule_library"]["laowanjia_index"] == 64
    assert payload["rule_library"]["primary_rules"] == ["熱門"]


def test_rule_library_empty_prechecked_snapshot_preserves_fallback_rule_semantics(monkeypatch):
    analysis = {
        "issue": "115040900",
        "laowanjia_score": 66,
        "hot_zone": ["21-30"],
        "cold_zone": ["61-70"],
        "ai_score": {},
    }
    prediction = _prediction()
    lookup_calls = {"count": 0}

    def unexpected_lookup(*args, **kwargs):
        lookup_calls["count"] += 1
        raise AssertionError("prechecked empty snapshot must not trigger another stored snapshot lookup")

    monkeypatch.setattr(player_dashboard, "_rule_snapshot_for_dashboard", unexpected_lookup)

    expected_snapshot = player_dashboard.build_rule_snapshot(
        analysis,
        prediction,
        source_issue="115040900",
        target_issue=prediction.get("prediction_issue") or prediction.get("target_issue"),
    )
    payload = player_dashboard._rule_library(analysis, prediction, snapshot={})

    expected_rules = [
        player_dashboard._rule_snapshot_item_to_dashboard(item)
        for item in expected_snapshot.get("rules") or []
    ]
    expected_completed = sum(1 for item in expected_rules if item.get("status") == "ready")
    labels_by_key = {key: label for key, label in player_dashboard.RULE_LIBRARY_NAMES}
    expected_primary = [
        labels_by_key.get(key, key)
        for key in ((expected_snapshot.get("aggregate") or {}).get("primary_rules") or [])
    ]

    assert lookup_calls["count"] == 0
    assert payload["rules"] == expected_rules
    assert payload["completed_count"] == expected_completed
    assert payload["total_count"] == (len(expected_rules) or len(player_dashboard.RULE_LIBRARY_NAMES))
    assert payload["primary_rules"] == expected_primary

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


def test_rule_library_prefers_snapshot_dashboard_analysis_summary(monkeypatch):
    analysis = {
        "laowanjia_score": 1,
        "hot_zone": ["legacy-hot"],
        "cold_zone": ["legacy-cold"],
        "three_star": [["legacy"]],
        "ai_score": {
            "super_number_trajectory_recovery": {"confidence": 1},
            "cluster_aftershock_recovery": {"confidence": 2},
        },
    }
    snapshot_summary = {
        "laowanjia_score": 72.5,
        "hot_zone": ["01-10"],
        "cold_zone": ["71-80"],
        "three_star": [[1, 2, 3]],
        "four_star": [[1, 2, 3, 4]],
        "five_star": None,
        "six_star": None,
        "super_number_trajectory_recovery": {"confidence": 70, "candidate_numbers": [40, 41]},
        "cluster_aftershock_recovery": {"confidence": 66, "candidate_numbers": [15, 16]},
    }
    monkeypatch.setattr(
        player_dashboard,
        "_rule_snapshot_for_dashboard",
        lambda source, prediction: {
            "rules": [],
            "aggregate": {},
            "dashboard_analysis_summary": snapshot_summary,
        },
    )

    result = player_dashboard._rule_library(analysis, _prediction())

    assert result["laowanjia_index"] == 72.5
    assert result["hot_zones"] == ["01-10"]
    assert result["cold_zone"] == ["71-80"]
    assert result["star_prediction"] == {
        "three_star": [[1, 2, 3]],
        "four_star": [[1, 2, 3, 4]],
        "five_star": None,
        "six_star": None,
    }
    assert result["super_trajectory"] == {"confidence": 70, "candidate_numbers": [40, 41]}
    assert result["cluster_recovery"] == {"confidence": 66, "candidate_numbers": [15, 16]}


def test_rule_library_falls_back_to_legacy_analysis_without_snapshot_summary(monkeypatch):
    analysis = {
        "laowanjia_score": 61.5,
        "hot_zone": ["11-20"],
        "cold_zone": ["61-70"],
        "three_star": [[3, 4, 5]],
        "four_star": [[3, 4, 5, 6]],
        "five_star": None,
        "six_star": None,
        "ai_score": {
            "super_number_trajectory_recovery": {"confidence": 55, "candidate_numbers": [30]},
            "cluster_aftershock_recovery": {"confidence": 54, "candidate_numbers": [31]},
        },
    }
    monkeypatch.setattr(
        player_dashboard,
        "_rule_snapshot_for_dashboard",
        lambda source, prediction: {"rules": [], "aggregate": {}},
    )

    result = player_dashboard._rule_library(analysis, _prediction())

    assert result["laowanjia_index"] == analysis["laowanjia_score"]
    assert result["hot_zones"] == analysis["hot_zone"]
    assert result["cold_zone"] == analysis["cold_zone"]
    assert result["star_prediction"] == {
        "three_star": analysis["three_star"],
        "four_star": analysis["four_star"],
        "five_star": None,
        "six_star": None,
    }
    assert result["super_trajectory"] == analysis["ai_score"]["super_number_trajectory_recovery"]
    assert result["cluster_recovery"] == analysis["ai_score"]["cluster_aftershock_recovery"]
