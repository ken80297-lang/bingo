from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_production_catch_up_jobs_schedule_when_historical_catchup_disabled(monkeypatch):
    import app as app_module

    calls = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            calls.append({"func": func, "trigger": trigger, **kwargs})

    monkeypatch.setattr(app_module, "scheduler", FakeScheduler())
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "update_collector_runtime",
        lambda **kwargs: calls.append({"runtime": kwargs}),
    )

    app_module._schedule_production_catch_up_jobs()

    job_ids = {call.get("id") for call in calls if call.get("id")}
    runtime = [call["runtime"] for call in calls if "runtime" in call][-1]

    assert "collector_official_catch_up_startup" in job_ids
    assert "collector_official_catch_up" in job_ids
    assert runtime["catch_up_scheduler_enabled"] is True
    assert runtime["catch_up_startup_job_registered"] is True
    assert runtime["catch_up_interval_job_registered"] is True


def test_daily_recovery_runs_production_catch_up(monkeypatch):
    from services import catch_up_service, collector_runtime, daily_recovery
    from services import latest_sync, official_verification, learning_engine

    calls = []
    monkeypatch.setattr(
        catch_up_service,
        "catch_up_missing_issues",
        lambda: calls.append("catch_up") or {
            "status": "ok",
            "catch_count": 43,
            "success_count": 43,
            "failed_count": 0,
        },
    )
    monkeypatch.setattr(
        latest_sync,
        "process_latest_official_draw",
        lambda: {"status": "ok", "source_issue": "115040893", "database_saved": True, "analysis_created": True, "prediction_created": True},
    )
    monkeypatch.setattr(official_verification, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(learning_engine, "backfill_learning_records", lambda limit=20: {"status": "ok"})
    monkeypatch.setattr(learning_engine, "get_learning_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_runtime, "refresh_system_status_cache", lambda scheduler_status="running": {"status": "ok"})
    monkeypatch.setattr(daily_recovery, "save_recovery_report", lambda report: {"status": "ok", "id": 1})
    monkeypatch.setattr(daily_recovery, "build_health_report", lambda report=None: {"status": "healthy"})

    result = daily_recovery.run_daily_recovery(force=True)

    assert calls == ["catch_up"]
    assert result["status"] == "ok"
    assert result["steps"]["production_catch_up"]["success_count"] == 43
    assert result["checked_issue_count"] == 44
    assert result["repaired_issue_count"] == 44
