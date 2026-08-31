from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class FakeScheduler:
    running = False

    def shutdown(self, wait: bool = False) -> None:
        raise AssertionError("scheduler shutdown should not run when scheduler is stopped")


def test_shutdown_event_is_db_free(monkeypatch, capsys):
    import app as app_module
    import database
    from database import collector_store, prediction_history_store
    from services import latest_sync, prediction_service

    def fail(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} called during shutdown")

        return _raise

    assert not hasattr(app_module, "get_latest_sync_snapshot")
    monkeypatch.setattr(app_module, "scheduler", FakeScheduler())
    monkeypatch.setattr(latest_sync, "get_latest_sync_snapshot", fail("latest_sync.get_latest_sync_snapshot"))
    monkeypatch.setattr(collector_store, "get_latest_kuaishou_snapshot", fail("collector_store.get_latest_kuaishou_snapshot"))
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_for_source_target",
        fail("prediction_history_store.get_prediction_for_source_target"),
    )
    monkeypatch.setattr(database, "get_connection", fail("database.get_connection"))
    monkeypatch.setattr(latest_sync, "shutdown_latest_sync_background_tasks", lambda *args, **kwargs: {"status": "stopped"})
    monkeypatch.setattr(
        prediction_service,
        "shutdown_prediction_background_tasks",
        lambda *args, **kwargs: {"status": "stopped"},
    )
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


def test_latest_sync_request_path_still_uses_existing_read_model(monkeypatch):
    from services import latest_sync

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
