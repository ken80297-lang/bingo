from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db
from database import cloud_draws, collector_store, data_quality_store, prediction_history_store
from services import catch_up_service, collector_runtime, learning_engine


def _reset_cache():
    collector_runtime._SYSTEM_STATUS_CACHE = None
    collector_runtime._SYSTEM_STATUS_LAST_REFRESH_ERROR = None
    collector_runtime._SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = None
    collector_runtime._SYSTEM_STATUS_REFRESH_IN_PROGRESS = False


def test_system_status_cache_refresh_and_hit(monkeypatch):
    _reset_cache()
    calls = {"statistics": 0}

    def fake_statistics():
        calls["statistics"] += 1
        return {"latest_issue": "115000001", "last_update": "2026-07-15T00:00:00+00:00"}

    monkeypatch.setattr(db, "get_statistics", fake_statistics)
    monkeypatch.setattr(db, "get_latest_draw", lambda: {"issue": "115000001"})
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "last_successful_collect_time": datetime.now(timezone.utc).isoformat(),
        "last_collect_duration": 0.1,
        "catch_up_available": True,
    })
    monkeypatch.setattr(prediction_history_store, "get_prediction_history_count", lambda: 12)
    monkeypatch.setattr(collector_store, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(data_quality_store, "get_data_quality_status", lambda: {"status": "ok"})
    monkeypatch.setattr(learning_engine, "get_learning_status", lambda: {"status": "ok"})
    monkeypatch.setattr(cloud_draws, "get_cloud_history_draws", lambda limit: [{"issue": "115000001"}])

    refreshed = collector_runtime.refresh_system_status_cache(scheduler_status="running")
    cached = collector_runtime.get_system_status_cache(scheduler_status="running")

    assert refreshed["latest_issue"] == "115000001"
    assert cached["latest_issue"] == "115000001"
    assert cached["cache_source"] == "memory"
    assert cached["cache_state"] == "fresh"
    assert cached["stale"] is False
    assert calls["statistics"] == 1


def test_system_status_cache_minimal_fallback_is_stale():
    _reset_cache()

    payload = collector_runtime.get_system_status_cache(scheduler_status="unknown")

    assert payload["cache_source"] == "minimal"
    assert payload["cache_state"] == "unavailable"
    assert payload["stale"] is True
    assert payload["status"] == "ok"


def test_system_status_cache_stale_detection():
    _reset_cache()
    collector_runtime._SYSTEM_STATUS_CACHE = {
        "status": "ok",
        "scheduler": "running",
        "cache_refreshed_at": (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(),
    }

    payload = collector_runtime.get_system_status_cache(scheduler_status="running")

    assert payload["cache_state"] == "stale"
    assert payload["stale"] is True


def test_official_lock_stale_recovery_clears_runtime(monkeypatch):
    acquired = collector_runtime._OFFICIAL_LOCK.acquire(blocking=False)
    collector_runtime._STATE.update(
        {
            "collector_running": True,
            "catch_up_running": False,
            "official_lock_owner": "official_collector",
            "last_collector_started_at": (datetime.now(timezone.utc) - timedelta(seconds=collector_runtime.OFFICIAL_LOCK_STALE_SECONDS + 1)).isoformat(),
            "last_collector_finished_at": None,
            "last_collector_exit_reason": None,
        }
    )

    try:
        assert collector_runtime._release_stale_official_lock() is True
        status = collector_runtime.collector_runtime_status()
        assert status["collector_running"] is False
        assert status["official_lock_owner"] is None
    finally:
        if acquired:
            try:
                collector_runtime._OFFICIAL_LOCK.release()
            except RuntimeError:
                pass
