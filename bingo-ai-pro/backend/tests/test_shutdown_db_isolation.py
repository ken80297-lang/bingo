from __future__ import annotations

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import latest_sync


def setup_function():
    latest_sync.shutdown_latest_sync_background_tasks(wait=True, timeout_seconds=0.1)
    latest_sync.reset_latest_sync_background_tasks_for_tests()


def teardown_function():
    latest_sync.shutdown_latest_sync_background_tasks(wait=True, timeout_seconds=0.1)
    latest_sync.reset_latest_sync_background_tasks_for_tests()


class FakeScheduler:
    running = False

    def shutdown(self, wait: bool = False) -> None:
        raise AssertionError("scheduler shutdown should not run when scheduler is stopped")


def test_shutdown_event_is_db_free(monkeypatch, capsys):
    import app as app_module
    import database
    from database import collector_store, official_draw_store, prediction_history_store

    def fail(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} called during shutdown")

        return _raise

    assert not hasattr(app_module, "get_latest_sync_snapshot")
    monkeypatch.setattr(app_module, "scheduler", FakeScheduler())
    monkeypatch.setattr(latest_sync, "get_latest_sync_snapshot", fail("latest_sync.get_latest_sync_snapshot"))
    monkeypatch.setattr(
        official_draw_store,
        "get_latest_official_draw_sync_status",
        fail("official_draw_store.get_latest_official_draw_sync_status"),
    )
    monkeypatch.setattr(collector_store, "get_latest_kuaishou_snapshot", fail("collector_store.get_latest_kuaishou_snapshot"))
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_for_source_target",
        fail("prediction_history_store.get_prediction_for_source_target"),
    )
    monkeypatch.setattr(database, "get_connection", fail("database.get_connection"))
    monkeypatch.setattr(app_module.app.state, "last_health_request_at", "2026-08-31T00:00:00+00:00", raising=False)
    monkeypatch.setattr(app_module.app.state, "health_request_count_since_start", 3, raising=False)
    monkeypatch.setattr(app_module.app.state, "last_health_request_method", "GET", raising=False)
    monkeypatch.setattr(app_module.app.state, "wake_source", "test", raising=False)

    app_module.shutdown_event()

    output = capsys.readouterr().out
    assert "WAKE_MONITOR shutting_down" in output
    assert "last_health_request_at=2026-08-31T00:00:00+00:00" in output
    assert "request_count=3" in output
    assert "last_health_request_method=GET" in output
    assert "wake_source=test" in output
    assert "database_latest_issue" not in output
    assert "last_collector_success_at" not in output


def test_shutdown_quiesce_returns_memory_snapshot_without_db(monkeypatch):
    import database
    from database import collector_store, official_draw_store, prediction_history_store

    def fail(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} called after latest-sync quiesce")

        return _raise

    latest_sync.shutdown_latest_sync_background_tasks(timeout_seconds=0)
    monkeypatch.setattr(
        official_draw_store,
        "get_latest_official_draw_sync_status",
        fail("official_draw_store.get_latest_official_draw_sync_status"),
    )
    monkeypatch.setattr(latest_sync, "get_latest_official_draw_sync_status", fail("latest_sync.get_latest_official_draw_sync_status"))
    monkeypatch.setattr(collector_store, "get_latest_kuaishou_snapshot", fail("collector_store.get_latest_kuaishou_snapshot"))
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", fail("latest_sync.get_latest_kuaishou_snapshot"))
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_for_source_target",
        fail("prediction_history_store.get_prediction_for_source_target"),
    )
    monkeypatch.setattr(latest_sync, "get_prediction_for_source_target", fail("latest_sync.get_prediction_for_source_target"))
    monkeypatch.setattr(database, "get_connection", fail("database.get_connection"))

    result = latest_sync.get_latest_sync_snapshot(allow_reconcile=False)

    assert result["read_model"] == "memory"
    assert result["quiescing"] is True
    assert result["quiesce_reason"] == "shutdown_quiescing"


def test_shutdown_waits_for_active_latest_sync_snapshot_read(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    shutdown_returned = threading.Event()

    def active_snapshot(*, allow_reconcile=True):
        entered.set()
        release.wait(timeout=1)
        return {"status": "ok", "allow_reconcile": allow_reconcile}

    monkeypatch.setattr(latest_sync, "_build_latest_sync_snapshot", active_snapshot)
    worker = threading.Thread(target=lambda: latest_sync.get_latest_sync_snapshot(allow_reconcile=False))
    worker.start()
    assert entered.wait(timeout=1)

    shutdown_worker = threading.Thread(
        target=lambda: (latest_sync.shutdown_latest_sync_background_tasks(timeout_seconds=1), shutdown_returned.set())
    )
    shutdown_worker.start()
    time.sleep(0.02)

    assert not shutdown_returned.is_set()
    release.set()
    worker.join(timeout=1)
    shutdown_worker.join(timeout=1)
    assert shutdown_returned.is_set()


def test_pending_latest_sync_background_work_is_cancelled_before_db(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    pending_started = []

    def blocker():
        started.set()
        release.wait(timeout=1)

    def should_not_start():
        pending_started.append(True)
        raise AssertionError("pending latest-sync work reached DB after shutdown")

    first = latest_sync._submit_reconcile_background(blocker)
    assert started.wait(timeout=1)
    second = latest_sync._submit_reconcile_background(should_not_start)

    result = latest_sync.shutdown_latest_sync_background_tasks(timeout_seconds=0.01)
    release.set()
    if first is not None:
        first.result(timeout=1)

    assert pending_started == []
    assert second is not None
    assert second.cancelled()
    assert result["cancelled_futures"] >= 1


def test_running_latest_sync_background_work_is_drained_before_shutdown_returns():
    started = threading.Event()
    release = threading.Event()
    finished = []

    def running_work():
        started.set()
        release.wait(timeout=1)
        finished.append(True)

    future = latest_sync._submit_reconcile_background(running_work)
    assert started.wait(timeout=1)
    release.set()

    result = latest_sync.shutdown_latest_sync_background_tasks(wait=True, timeout_seconds=1)

    assert future is not None
    assert future.done()
    assert finished == [True]
    assert result["pending_futures"] == 0
    assert result["drained_futures"] >= 1


def test_new_latest_sync_background_submit_rejected_after_shutdown():
    latest_sync.shutdown_latest_sync_background_tasks(timeout_seconds=0)

    future = latest_sync._submit_reconcile_background(lambda: None)

    assert future is None


def test_shutdown_drain_is_bounded_for_stuck_background_work():
    started = threading.Event()
    release = threading.Event()

    def stuck_work():
        started.set()
        release.wait(timeout=1)

    started_at = time.perf_counter()
    future = latest_sync._submit_reconcile_background(stuck_work)
    assert started.wait(timeout=1)

    result = latest_sync.shutdown_latest_sync_background_tasks(wait=True, timeout_seconds=0.01)
    elapsed = time.perf_counter() - started_at
    release.set()
    if future is not None:
        future.result(timeout=1)

    assert elapsed < 0.5
    assert result["pending_futures"] == 1
    assert result["timeout_seconds"] == 0.01


def test_latest_sync_request_path_still_uses_existing_read_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: calls.append("official_sync_status")
        or {
            "draw": {"issue": "115040900", "numbers": list(range(1, 21))},
            "target_issue": "115040901",
            "analysis_exists": True,
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: calls.append("kuaishou") or {"issue": "115040900"})
    monkeypatch.setattr(
        latest_sync,
        "get_prediction_for_source_target",
        lambda source, target: calls.append(("prediction", source, target))
        or {"issue": source, "prediction_issue": target, "recommend_numbers": [1, 2, 3]},
    )
    monkeypatch.setattr(latest_sync, "fast_path_prediction_is_current", lambda prediction: bool(prediction))

    result = latest_sync.get_latest_sync_snapshot(allow_reconcile=False)

    assert result["database_latest_issue"] == "115040900"
    assert "official_sync_status" in calls
    assert "kuaishou" in calls
    assert ("prediction", "115040900", "115040901") in calls
