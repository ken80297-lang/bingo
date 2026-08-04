from __future__ import annotations

import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db
from config import production_scope
from database import cloud_draws, collector_store, data_quality_store, prediction_history_store
from database import release_store
from services import catch_up_service, collector_runtime, daily_recovery, learning_engine, prediction_service


def _patch_fast_status_dependencies(monkeypatch):
    monkeypatch.setattr(db, "get_statistics", lambda: {"latest_issue": "115000001", "last_update": "2026-07-15T00:00:00+00:00"})
    monkeypatch.setattr(db, "get_latest_draw", lambda: {"issue": "115000001"})
    monkeypatch.setattr(prediction_history_store, "get_prediction_history_count", lambda: 12)
    monkeypatch.setattr(collector_store, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(data_quality_store, "get_data_quality_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_runtime, "_sqlite_status", lambda: "available")
    monkeypatch.setattr(collector_runtime, "_cloud_status", lambda: "available")
    monkeypatch.setattr(collector_runtime, "_learning_status", lambda: {"status": "ok"})
    monkeypatch.setattr(release_store, "get_current_release", lambda: {"status": "ok"})
    monkeypatch.setattr(daily_recovery, "get_recovery_status", lambda: {"status": "disabled"})
    monkeypatch.setattr(daily_recovery, "build_health_report", lambda: {"status": "ok"})
    monkeypatch.setattr(prediction_service, "prediction_lock_status", lambda: {"status": "idle"})


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


def test_system_status_cache_deadline_returns_partial_cache(monkeypatch):
    _reset_cache()
    collector_runtime._SYSTEM_STATUS_CACHE = {
        "status": "ok",
        "scheduler": "running",
        "latest_issue": "115000009",
        "cache_refreshed_at": datetime.now(timezone.utc).isoformat(),
    }

    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS", 0)

    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")

    assert payload["latest_issue"] == "115000009"
    assert payload["timeout"] is True
    assert payload["partial"] is True
    assert payload["timeout_steps"]
    assert payload["refresh_in_progress"] is False
    assert collector_runtime._SYSTEM_STATUS_REFRESH_LOCK.acquire(blocking=False) is True
    collector_runtime._SYSTEM_STATUS_REFRESH_LOCK.release()


def test_system_status_cache_production_scope_timeout_returns_partial(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    collector_runtime._SYSTEM_STATUS_CACHE = {
        "status": "ok",
        "scheduler": "running",
        "production_scope": {"production_generation": 9, "stale": True},
        "cache_refreshed_at": datetime.now(timezone.utc).isoformat(),
    }

    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })
    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_PRODUCTION_SCOPE_TIMEOUT_SECONDS", 0.01)

    def blocked_production_scope():
        time.sleep(1)
        return {"production_generation": 2}

    monkeypatch.setattr(production_scope, "production_scope_payload", blocked_production_scope)

    started = time.perf_counter()
    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert payload["partial"] is True
    assert payload["timeout"] is False
    assert "production_scope" in payload["timeout_steps"]
    assert payload["production_scope"] == {"production_generation": 9, "stale": True}


def test_system_status_cache_does_not_wait_for_official_source_when_it_hangs(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    catch_up_service.LAST_CATCH_UP_RESULT.update(
        {
            "status": "ok",
            "source_latest_issue": "115000001",
            "last_successful_collect_time": datetime.now(timezone.utc).isoformat(),
            "last_collect_duration": 0.1,
        }
    )
    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "115000001")

    def blocked_source_latest_issue():
        time.sleep(1)
        return "115000999"

    monkeypatch.setattr(catch_up_service, "get_source_latest_issue", blocked_source_latest_issue)

    started = time.perf_counter()
    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert payload["source_latest_issue"] == "115000001"
    assert payload["lag_count"] == 0
    assert payload["cache_source"] == "refresh"


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
