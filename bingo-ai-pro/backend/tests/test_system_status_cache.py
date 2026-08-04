from __future__ import annotations

import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db
from database import cloud_draws, collector_store, data_quality_store, prediction_history_store
from database import release_store
from services import catch_up_service, collector_runtime, daily_recovery, latest_sync, learning_engine, prediction_service


def _patch_fast_status_dependencies(monkeypatch):
    monkeypatch.setattr(db, "get_statistics", lambda: {"latest_issue": "115000001", "last_update": "2026-07-15T00:00:00+00:00"})
    monkeypatch.setattr(db, "get_latest_draw", lambda: {"issue": "115000001"})
    monkeypatch.setattr(prediction_history_store, "get_prediction_history_count", lambda: 12)
    monkeypatch.setattr(collector_store, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(data_quality_store, "get_data_quality_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_runtime, "_sqlite_status", lambda: "available")
    monkeypatch.setattr(collector_runtime, "_cloud_status", lambda: "available")
    monkeypatch.setattr(release_store, "get_current_release", lambda: {"status": "ok"})
    monkeypatch.setattr(daily_recovery, "get_recovery_status", lambda: {"status": "disabled"})
    monkeypatch.setattr(daily_recovery, "build_health_report", lambda: {"status": "ok"})
    monkeypatch.setattr(prediction_service, "prediction_lock_status", lambda: {"status": "idle"})


def _reset_cache():
    collector_runtime._SYSTEM_STATUS_CACHE = None
    collector_runtime._SYSTEM_STATUS_LAST_REFRESH_ERROR = None
    collector_runtime._SYSTEM_STATUS_LAST_REFRESH_DURATION_MS = None
    collector_runtime._SYSTEM_STATUS_REFRESH_IN_PROGRESS = False
    learning_engine._LEARNING_STATUS_CACHE["payload"] = None
    learning_engine._LEARNING_STATUS_CACHE["expires_at"] = 0.0


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


def test_system_status_cache_uses_learning_snapshot_when_full_status_blocks(monkeypatch):
    _reset_cache()
    calls = {"full_status": 0}

    def blocked_full_learning_status():
        calls["full_status"] += 1
        time.sleep(150)
        return {"status": "ok"}

    _patch_fast_status_dependencies(monkeypatch)
    monkeypatch.setattr(learning_engine, "get_learning_status", blocked_full_learning_status)
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })

    started = time.perf_counter()
    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")
    elapsed = time.perf_counter() - started

    assert elapsed < 10
    assert calls["full_status"] == 0
    assert payload["learning"]["cache"]["status"] == "cache_empty"
    assert payload["learning"]["stale"] is True
    assert "learning_status" not in payload["timeout_steps"]


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


def test_system_status_cache_does_not_submit_second_learning_task_or_grow_threads(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })

    acquired = learning_engine._LEARNING_STATUS_CACHE_LOCK.acquire(blocking=False)
    before_threads = threading.active_count()
    try:
        first = collector_runtime.refresh_system_status_cache(scheduler_status="running")
        second = collector_runtime.refresh_system_status_cache(scheduler_status="running")
    finally:
        if acquired:
            learning_engine._LEARNING_STATUS_CACHE_LOCK.release()

    after_threads = threading.active_count()
    assert first["learning"]["cache"]["status"] == "lock_unavailable"
    assert second["learning"]["cache"]["status"] == "lock_unavailable"
    assert after_threads <= before_threads + 1


def test_status_step_worker_allows_only_one_inflight_task():
    before_threads = threading.active_count()

    try:
        collector_runtime._run_bounded_step(lambda: (time.sleep(1) or {"status": "slow"}), 0.01)
    except collector_runtime._StatusCacheStepTimeout:
        pass
    else:
        raise AssertionError("expected first bounded step to time out")

    try:
        collector_runtime._run_bounded_step(lambda: {"status": "second"}, 0.01)
    except collector_runtime._StatusCacheStepWorkerBusy:
        pass
    else:
        raise AssertionError("expected second bounded step to see busy worker")

    assert threading.active_count() <= before_threads + 1
    time.sleep(1.05)


def test_system_status_cache_learning_failure_does_not_block_other_steps(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })
    monkeypatch.setattr(collector_runtime, "_learning_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")

    assert payload["learning"]["status"] == "unknown"
    assert payload["production_scope"]["production_generation"] == 2
    assert payload["release"]["status"] == "ok"


def test_system_status_cache_timeout_attribution_marks_learning_status(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_STEP_MIN_REMAINING_SECONDS", 0)
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })
    monkeypatch.setattr(collector_runtime, "_learning_status", lambda: (time.sleep(0.12) or {"status": "ok"}))

    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")

    assert payload["timeout"] is True
    assert "learning_status" in payload["timeout_steps"]
    assert "production_scope" not in payload["timeout_steps"]


def test_system_status_cache_release_fast_is_not_misattributed(monkeypatch):
    _reset_cache()
    _patch_fast_status_dependencies(monkeypatch)
    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_CACHE_REFRESH_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(collector_runtime, "SYSTEM_STATUS_STEP_MIN_REMAINING_SECONDS", 0.02)
    monkeypatch.setattr(catch_up_service, "get_catch_up_status", lambda fetch_source=False: {
        "database_latest_issue": "115000001",
        "source_latest_issue": "115000001",
        "lag_count": 0,
        "catch_up_available": True,
    })
    monkeypatch.setattr(data_quality_store, "get_data_quality_status", lambda: (time.sleep(0.04) or {"status": "ok"}))

    payload = collector_runtime.refresh_system_status_cache(scheduler_status="running")

    release_step = next(item for item in payload["status_cache_steps"] if item["step"] == "release")
    assert release_step["result"] == "stale"
    assert release_step["timeout"] is False
    assert "release" not in payload["timeout_steps"]


def test_learning_snapshot_uses_stale_cache_when_available():
    _reset_cache()
    learning_engine._LEARNING_STATUS_CACHE["payload"] = {"status": "ok", "total_records": 10}
    learning_engine._LEARNING_STATUS_CACHE["expires_at"] = time.monotonic() - 1

    payload = learning_engine.get_learning_status_snapshot()

    assert payload["status"] == "ok"
    assert payload["stale"] is True
    assert payload["cache"]["status"] == "snapshot_stale"


def test_learning_status_records_substep_timings(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(learning_engine, "get_learning_status_counts", lambda: {"failed_records": 0, "total_records": 1})
    monkeypatch.setattr(learning_engine, "get_prediction_history_statistics", lambda limit: {
        "pending_learning": 0,
        "verified_waiting_learning": 0,
        "last_learning_time": None,
    })

    payload = learning_engine.get_learning_status()

    steps = {item["step"]: item for item in payload["learning_status_steps"]}
    assert "learning_history_counts_query" in steps
    assert "prediction_history_statistics_query" in steps
    assert all("duration_ms" in item and item["result"] == "ok" for item in payload["learning_status_steps"])


def test_system_status_cache_policy_flags_unchanged():
    assert latest_sync.HISTORICAL_CATCHUP_ENABLED is False
    assert latest_sync.LATEST_ISSUE_PRIORITY is True


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
