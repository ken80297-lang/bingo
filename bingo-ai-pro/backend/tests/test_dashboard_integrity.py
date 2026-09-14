from __future__ import annotations

import pathlib
import sqlite3
import sys
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import prediction_history_store
from services import player_dashboard


@pytest.fixture(autouse=True)
def reset_dashboard_caches():
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    for key, value in list(player_dashboard._PLAYER_COMPONENT_CACHE.items()):
        player_dashboard._PLAYER_COMPONENT_CACHE[key] = [] if isinstance(value, list) else None
    player_dashboard._PLAYER_COMPONENT_CACHE["prediction_aggregates"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["analysis"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["kuaishou"] = {}
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()
    yield
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()


def test_component_metadata_marks_cache_source_on_timeout():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
        "recommend_numbers": list(range(1, 21)),
        "generated_at": "2026-09-14T00:00:00+00:00",
    }
    future = Future()
    timings: list[dict] = []
    warnings: list[str] = []
    metadata: dict[str, dict] = {}

    result = player_dashboard._component_result(
        "next_prediction_snapshot",
        future,
        deadline=time.monotonic() + 1,
        timeout_seconds=0.001,
        timings=timings,
        warnings=warnings,
        component_metadata=metadata,
        dashboard_generation_id="20260914-101100-115051",
    )

    assert result["source"] == "cache"
    assert result["_component_metadata"]["source"] == "cache"
    assert result["_component_metadata"]["timed_out"] is True
    assert timings[0]["timed_out"] is True


def test_old_late_future_cannot_overwrite_newer_cache():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
    }

    updated = player_dashboard._store_component_cache(
        "next_prediction_snapshot",
        {"issue": "115051969", "prediction_issue": "115051970"},
    )

    assert updated is False
    assert player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"]["issue"] == "115051970"


def test_newer_issue_can_update_cache():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
    }

    updated = player_dashboard._store_component_cache(
        "next_prediction_snapshot",
        {"issue": "115051971", "prediction_issue": "115051972"},
    )

    assert updated is True
    assert player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"]["issue"] == "115051971"


def test_ambiguous_issue_cannot_overwrite_known_cache():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
    }

    updated = player_dashboard._store_component_cache(
        "next_prediction_snapshot",
        {"issue": None, "prediction_issue": None, "generated_at": "2026-09-14T00:01:00+00:00"},
    )

    assert updated is False
    assert player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"]["issue"] == "115051970"


def test_same_issue_older_generated_at_cannot_overwrite_cache():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
        "generated_at": "2026-09-14T00:02:00+00:00",
        "recommend_numbers": [1],
    }

    updated = player_dashboard._store_component_cache(
        "next_prediction_snapshot",
        {
            "issue": "115051970",
            "prediction_issue": "115051971",
            "generated_at": "2026-09-14T00:01:00+00:00",
            "recommend_numbers": [2],
        },
    )

    assert updated is False
    assert player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"]["recommend_numbers"] == [1]


def test_same_issue_newer_generated_at_can_update_cache():
    player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"] = {
        "issue": "115051970",
        "prediction_issue": "115051971",
        "generated_at": "2026-09-14T00:01:00+00:00",
        "recommend_numbers": [1],
    }

    updated = player_dashboard._store_component_cache(
        "next_prediction_snapshot",
        {
            "issue": "115051970",
            "prediction_issue": "115051971",
            "generated_at": "2026-09-14T00:02:00+00:00",
            "recommend_numbers": [2],
        },
    )

    assert updated is True
    assert player_dashboard._PLAYER_COMPONENT_CACHE["next_prediction_snapshot"]["recommend_numbers"] == [2]


def test_mixed_issue_health_is_degraded():
    health = player_dashboard._dashboard_health(
        {
            "next_prediction_snapshot": {"source": "live", "stale": False, "result": "ok"},
            "previous_verification": {"source": "live", "stale": False, "result": "ok"},
            "prediction_aggregates": {"source": "live", "stale": False, "result": "ok"},
        },
        official_issue="115051970",
        next_prediction={"based_on_issue": "115051969", "prediction_issue": "115051970"},
        previous_verification={"target_issue": "115051968"},
        aggregates={"latest_issue": "115051968"},
        card_two_history=[{"prediction_issue": "115051968"}],
        generation_id="20260914-101100-115051",
    )

    assert health["status"] == "degraded"
    assert health["issue_consistent"] is False


def test_sqlite_missing_table_does_not_raise_when_postgres_path_empty(monkeypatch):
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)
    monkeypatch.setattr(prediction_history_store, "_query_cloud", lambda sql, params=(): [])

    def missing_table(sql, params=()):
        raise sqlite3.OperationalError("no such table: prediction_history")

    monkeypatch.setattr(prediction_history_store, "_query_sqlite", missing_table)

    assert prediction_history_store._query_with_fallback("select * from prediction_history") == []


def test_latest_prediction_does_not_query_sqlite_sidecar_unless_enabled(monkeypatch):
    row = (
        1,
        "115051970",
        "115051971",
        "2026-09-14T00:00:00+00:00",
        "production",
        0.8,
        list(range(1, 21)),
        7,
        [1, 2, 3],
        [1, 2, 3, 4],
        [],
        [],
        [],
        [],
        "small",
        "odd",
        [],
        [],
        list(range(10, 30)),
        0,
        False,
        False,
        False,
        0,
        "2026-09-14T00:00:00+00:00",
        "2026-09-14T00:01:00+00:00",
        {},
        None,
        "waiting_draw",
        None,
        None,
        [],
        [],
        20,
        0,
        False,
        None,
        False,
        None,
        2,
        True,
        "v28",
        "abc123",
        "V7",
        "features",
    )
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: True)
    monkeypatch.setattr(prediction_history_store, "_sqlite_sidecar_enabled", lambda: False)
    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", lambda sql, params=(), sqlite_sql=None: [row])
    monkeypatch.setattr(
        prediction_history_store,
        "_query_sqlite",
        lambda *args, **kwargs: pytest.fail("sqlite sidecar should be explicit opt-in"),
    )
    monkeypatch.setattr(prediction_history_store, "_prediction_event_metadata", lambda record: {})

    result = prediction_history_store.get_latest_prediction_history()

    assert result["prediction_issue"] == "115051971"


def test_prediction_aggregates_combined_query_count(monkeypatch):
    calls = []

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append(sql)
        assert "with prediction_counts" in sql
        assert "learned_counts" in sql
        return [(10, 9, 1, 8, 6, 6, 5, 6, 7, "115051970")]

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    result = prediction_history_store.get_prediction_lifecycle_aggregates()

    assert len(calls) == 1
    assert result["query_count"] == 1
    assert result["total_prediction_count"] == 10
    assert result["learned_distinct_target_count"] == 7
    assert result["latest_issue"] == "115051970"


def test_prediction_aggregates_combined_semantic_equivalence(monkeypatch):
    old_flow = {
        "total_prediction_count": 10,
        "valid_target_count": 9,
        "null_target_count": 1,
        "valid_prediction_count": 8,
        "completed_verified_count": 6,
        "stored_official_result_count": 6,
        "has_official_result_count": 5,
        "valid_sample_count": 6,
        "learned_distinct_target_count": 7,
    }
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: [(
            old_flow["total_prediction_count"],
            old_flow["valid_target_count"],
            old_flow["null_target_count"],
            old_flow["valid_prediction_count"],
            old_flow["completed_verified_count"],
            old_flow["stored_official_result_count"],
            old_flow["has_official_result_count"],
            old_flow["valid_sample_count"],
            old_flow["learned_distinct_target_count"],
            "115051970",
        )],
    )

    result = prediction_history_store.get_prediction_lifecycle_aggregates()

    for key, value in old_flow.items():
        assert result[key] == value


def test_previous_verification_combined_reader_shape(monkeypatch):
    prediction_width = len(prediction_history_store.PREDICTION_SUMMARY_COLUMNS)
    row = (
        1,
        "115051969",
        "115051970",
        "2026-09-14T00:00:00+00:00",
        "production",
        0.8,
        list(range(1, 21)),
        7,
        [1, 2, 3],
        [1, 2, 3, 4],
        [],
        [],
        [],
        [],
        "small",
        "odd",
        list(range(10, 30)),
        10,
        True,
        False,
        False,
        0.5,
        "2026-09-14T00:00:00+00:00",
        "2026-09-14T00:01:00+00:00",
        "model",
        "verified",
        "115051970",
        "2026-09-14T00:02:00+00:00",
        list(range(10, 20)),
        list(range(1, 10)),
        20,
        0.5,
        True,
        True,
        0.9,
        2,
        True,
        "v28",
        0,
        "exact_previous",
        55,
        "115051970",
        "2026-09-14",
        "2026-09-14T00:05:00+00:00",
        list(range(10, 30)),
        list(range(10, 30)),
        7,
        False,
        "official",
        "verified",
        None,
        True,
        {},
        "2026-09-14T00:05:00+00:00",
        "2026-09-14T00:05:00+00:00",
    )
    assert len(row[:prediction_width]) == prediction_width
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", lambda sql, params=(), sqlite_sql=None: [row])
    monkeypatch.setattr(prediction_history_store, "_prediction_event_metadata", lambda record: {})

    result = prediction_history_store.get_previous_verification_summary_snapshot("115051970")

    assert result["mode"] == "exact_previous"
    assert result["record"]["prediction_issue"] == "115051970"
    assert result["draw"]["issue"] == "115051970"
    assert result["db_timing"]["target_issue"] == "115051970"


def test_previous_verification_old_new_semantic_equivalence(monkeypatch):
    record = {
        "issue": "115051969",
        "prediction_issue": "115051970",
        "predict_time": "2026-09-14T00:00:00+00:00",
        "recommend_numbers": list(range(1, 21)),
        "winning_numbers": list(range(10, 30)),
        "matched_numbers": list(range(10, 21)),
        "missed_numbers": list(range(1, 10)),
        "hit_count": 11,
        "prediction_count": 20,
        "super_number": 12,
        "super_number_hit": True,
        "prediction_status": "verified",
        "verified_at": "2026-09-14T00:02:00+00:00",
        "learning_used": True,
    }
    draw = {
        "issue": "115051970",
        "draw_time": "2026-09-14T00:05:00+00:00",
        "numbers": list(range(10, 30)),
        "super_number": 12,
    }
    monkeypatch.setattr(player_dashboard, "_prediction_by_target_issue", lambda issue: record)
    monkeypatch.setattr(player_dashboard, "get_official_draw_by_issue", lambda issue: draw)
    old_record, old_mode = player_dashboard._previous_result_for_based_on("115051970")
    old_payload = player_dashboard._verification(old_record, draw)
    old_payload["previous_result_mode"] = old_mode
    old_payload["requested_target_issue"] = "115051970"
    old_payload["displayed_target_issue"] = old_record["prediction_issue"]
    monkeypatch.setattr(
        player_dashboard,
        "get_previous_verification_summary_snapshot",
        lambda issue: {"record": record, "draw": draw, "mode": "exact_previous", "db_timing": {"query_count": 1}},
    )

    new_payload = player_dashboard._build_previous_verification_snapshot("115051970")

    for key in (
        "target_issue",
        "predicted_numbers",
        "official_numbers",
        "matched_numbers",
        "missed_numbers",
        "hit_count",
        "super_number_hit",
        "previous_result_mode",
        "requested_target_issue",
        "displayed_target_issue",
    ):
        assert new_payload[key] == old_payload[key]


def test_latest_prediction_context_can_skip_fallback_lookup(monkeypatch):
    row = (
        55,
        "115051970",
        "2026-09-14",
        "2026-09-14T00:05:00+00:00",
        list(range(1, 21)),
        list(range(1, 21)),
        7,
        False,
        "official",
        "verified",
        None,
        True,
        {},
        "2026-09-14T00:05:00+00:00",
        "2026-09-14T00:05:00+00:00",
    ) + (None,) * len(prediction_history_store.PREDICTION_SELECT_COLUMNS.split(","))
    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", lambda sql, params=(), sqlite_sql=None: [row])
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_for_source_target",
        lambda source, target: pytest.fail("fallback lookup should be disabled"),
    )

    result = prediction_history_store.get_latest_prediction_context(allow_fallback_lookup=False)

    assert result["draw"]["issue"] == "115051970"
    assert result["prediction"] is None
    assert result["target_issue"] == "115051971"
