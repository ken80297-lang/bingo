from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


class FakeScheduler:
    def __init__(self):
        self.calls = []
        self.listeners = []
        self.running = False

    def add_job(self, func, trigger, **kwargs):
        self.calls.append({"func": func, "trigger": trigger, **kwargs})

    def add_listener(self, listener, mask):
        self.listeners.append({"listener": listener, "mask": mask})

    def start(self):
        self.running = True


def test_env_bool_parses_disabled_values(monkeypatch):
    import app as app_module

    for raw in ("false", "FALSE", "0", "off", "no", "random"):
        monkeypatch.setenv("BINGO_TEST_FLAG", raw)
        assert app_module._env_bool("BINGO_TEST_FLAG", True) is False


def test_env_bool_parses_enabled_values(monkeypatch):
    import app as app_module

    for raw in ("true", "TRUE", "1", "on", "yes"):
        monkeypatch.setenv("BINGO_TEST_FLAG", raw)
        assert app_module._env_bool("BINGO_TEST_FLAG", False) is True


def test_env_bool_uses_default_for_empty_or_unset(monkeypatch):
    import app as app_module

    monkeypatch.delenv("BINGO_TEST_FLAG", raising=False)
    assert app_module._env_bool("BINGO_TEST_FLAG", True) is True
    assert app_module._env_bool("BINGO_TEST_FLAG", False) is False

    monkeypatch.setenv("BINGO_TEST_FLAG", "")
    assert app_module._env_bool("BINGO_TEST_FLAG", True) is True
    assert app_module._env_bool("BINGO_TEST_FLAG", False) is False


def test_catch_up_false_registers_no_startup_or_interval_job(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    runtime_updates = []
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "LATEST_ISSUE_PRIORITY", True)
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: runtime_updates.append(kwargs))

    app_module._schedule_production_catch_up_jobs()

    assert scheduler.calls == []
    assert runtime_updates[-1] == {
        "catch_up_scheduler_enabled": False,
        "catch_up_startup_job_registered": False,
        "catch_up_interval_job_registered": False,
    }


def test_collector_false_registers_no_startup_or_interval_job(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    runtime_updates = []
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "COLLECTOR_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: runtime_updates.append(kwargs))

    app_module._schedule_collector_jobs()

    assert scheduler.calls == []
    assert runtime_updates[-1] == {
        "collector_scheduler_enabled": False,
        "collector_startup_job_registered": False,
        "collector_interval_job_registered": False,
        "official_collector_interval_job_registered": False,
    }


def test_legacy_refresh_false_registers_no_startup_or_interval_job(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "LEGACY_REFRESH_SCHEDULER_ENABLED", False)

    app_module._schedule_legacy_refresh_jobs()

    assert scheduler.calls == []


def test_enabled_schedulers_register_expected_job_ids(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    runtime_updates = []
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(app_module, "COLLECTOR_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(app_module, "LEGACY_REFRESH_SCHEDULER_ENABLED", True)
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: runtime_updates.append(kwargs))

    app_module._schedule_production_catch_up_jobs()
    app_module._schedule_collector_jobs()
    app_module._schedule_legacy_refresh_jobs()

    job_ids = {call["id"] for call in scheduler.calls}
    assert "collector_official_catch_up_startup" in job_ids
    assert "collector_official_catch_up" in job_ids
    assert "collector_pilio_startup" in job_ids
    assert "collector_kuaishou_snapshot" in job_ids
    assert "collector_pilio_today" in job_ids
    assert "collector_official_today" in job_ids
    assert "first_refresh" in job_ids
    assert "refresh_job" in job_ids
    assert any(update.get("catch_up_scheduler_enabled") is True for update in runtime_updates)
    assert any(update.get("collector_scheduler_enabled") is True for update in runtime_updates)


def test_disabled_schedulers_do_not_call_outbound_collectors(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "COLLECTOR_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "LEGACY_REFRESH_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "catch_up_missing_issues", lambda: (_ for _ in ()).throw(AssertionError("catch-up called")))
    monkeypatch.setattr(app_module, "collect_pilio_today", lambda: (_ for _ in ()).throw(AssertionError("pilio called")))
    monkeypatch.setattr(app_module, "collect_kuaishou_snapshot", lambda: (_ for _ in ()).throw(AssertionError("kuaishou called")))
    monkeypatch.setattr(app_module, "collect_official_today", lambda: (_ for _ in ()).throw(AssertionError("official called")))
    monkeypatch.setattr(app_module, "refresh_data", lambda: (_ for _ in ()).throw(AssertionError("legacy refresh called")))
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: None)

    app_module._schedule_production_catch_up_jobs()
    app_module._schedule_collector_jobs()
    app_module._schedule_legacy_refresh_jobs()

    assert scheduler.calls == []


def test_startup_with_disabled_schedulers_registers_no_outbound_jobs(monkeypatch):
    import app as app_module

    scheduler = FakeScheduler()
    init_calls = []
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "COLLECTOR_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "LEGACY_REFRESH_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "DAILY_RECOVERY_ENABLED", False)
    monkeypatch.setattr(app_module, "STARTUP_DB_INIT_ENABLED", False)
    monkeypatch.setattr(app_module, "BACKGROUND_CACHE_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "DATA_QUALITY_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module.app.state, "scheduler_listener_registered", False, raising=False)
    monkeypatch.setattr(app_module.app.state, "scheduler", scheduler, raising=False)

    for name in (
        "init_db",
        "init_collector_tables",
        "init_analysis_tables",
        "init_data_quality_tables",
        "init_simulation_tables",
        "init_simulation_evaluation_tables",
        "init_adaptive_weight_tables",
        "init_strategy_ranking_tables",
        "init_strategy_evolution_tables",
        "init_system_health_tables",
        "init_operations_tables",
        "init_production_scope_tables",
        "init_official_draw_tables",
        "init_prediction_history_tables",
        "init_learning_tables",
        "init_recommendation_center_tables",
        "init_recovery_tables",
        "init_release_tables",
        "init_rule_snapshot_tables",
        "init_laowanjia_feature_tables",
        "init_prediction_tracker_tables",
    ):
        monkeypatch.setattr(
            app_module,
            name,
            lambda name=name: init_calls.append(name),
            raising=False,
        )
    monkeypatch.setattr(app_module, "warm_health_cache", lambda: init_calls.append("warm_health_cache") or {"status": "ok"})
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: None)

    app_module.startup_event()

    job_ids = {call["id"] for call in scheduler.calls}
    assert "collector_official_catch_up_startup" not in job_ids
    assert "collector_official_catch_up" not in job_ids
    assert "collector_pilio_startup" not in job_ids
    assert "collector_kuaishou_snapshot" not in job_ids
    assert "collector_pilio_today" not in job_ids
    assert "collector_official_today" not in job_ids
    assert "first_refresh" not in job_ids
    assert "refresh_job" not in job_ids
    assert "system_health_cache_startup" not in job_ids
    assert "system_health_cache_refresh" not in job_ids
    assert "system_status_runtime_cache_startup" not in job_ids
    assert "system_status_runtime_cache_refresh" not in job_ids
    assert "data_quality_startup" not in job_ids
    assert "data_quality_daily" not in job_ids
    assert scheduler.calls == []
    assert scheduler.running is False
    assert init_calls == []


def test_operations_db_init_disabled_does_not_initialize_operations(monkeypatch, capsys):
    import app as app_module

    monkeypatch.setattr(app_module, "OPERATIONS_DB_INIT_ENABLED", False)
    monkeypatch.setattr(
        app_module,
        "init_operations_tables",
        lambda: (_ for _ in ()).throw(AssertionError("operations init called")),
    )

    app_module._run_operations_db_init()

    output = capsys.readouterr().out
    assert "operations_db_init_disabled" in output


def test_operations_db_init_enabled_initializes_only_operations(monkeypatch, capsys):
    import app as app_module

    calls = []
    monkeypatch.setattr(app_module, "OPERATIONS_DB_INIT_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "init_operations_tables",
        lambda: calls.append("init_operations_tables") or {"cloud": "skipped", "sqlite": "ok"},
    )
    for name in (
        "_run_startup_db_init",
        "_schedule_background_cache_jobs",
        "_schedule_production_catch_up_jobs",
        "_schedule_collector_jobs",
        "_schedule_data_quality_jobs",
        "_schedule_legacy_refresh_jobs",
    ):
        monkeypatch.setattr(app_module, name, lambda name=name: calls.append(name))

    app_module._run_operations_db_init()

    output = capsys.readouterr().out
    assert calls == ["init_operations_tables"]
    assert "operations_db_init_completed cloud=skipped sqlite=ok" in output


def test_operations_db_init_failure_is_fail_soft(monkeypatch, capsys):
    import app as app_module

    monkeypatch.setattr(app_module, "OPERATIONS_DB_INIT_ENABLED", True)
    monkeypatch.setattr(
        app_module,
        "init_operations_tables",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    app_module._run_operations_db_init()

    output = capsys.readouterr().out
    assert "operations_db_init_failed error_type=RuntimeError" in output


def test_startup_operations_db_init_enabled_does_not_start_other_jobs(monkeypatch, capsys):
    import app as app_module

    scheduler = FakeScheduler()
    init_calls = []
    monkeypatch.setattr(app_module, "scheduler", scheduler)
    monkeypatch.setattr(app_module.app.state, "scheduler_listener_registered", False, raising=False)
    monkeypatch.setattr(app_module.app.state, "scheduler", scheduler, raising=False)
    monkeypatch.setattr(app_module, "STARTUP_DB_INIT_ENABLED", False)
    monkeypatch.setattr(app_module, "OPERATIONS_DB_INIT_ENABLED", True)
    monkeypatch.setattr(app_module, "CATCH_UP_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "COLLECTOR_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "LEGACY_REFRESH_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "DAILY_RECOVERY_ENABLED", False)
    monkeypatch.setattr(app_module, "BACKGROUND_CACHE_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "DATA_QUALITY_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(app_module, "_run_startup_db_init", lambda: init_calls.append("_run_startup_db_init"))
    monkeypatch.setattr(
        app_module,
        "init_operations_tables",
        lambda: init_calls.append("init_operations_tables") or {"cloud": "skipped", "sqlite": "ok"},
    )
    monkeypatch.setattr(app_module, "catch_up_missing_issues", lambda: (_ for _ in ()).throw(AssertionError("catch-up called")))
    monkeypatch.setattr(app_module, "collect_pilio_today", lambda: (_ for _ in ()).throw(AssertionError("pilio called")))
    monkeypatch.setattr(app_module, "collect_kuaishou_snapshot", lambda: (_ for _ in ()).throw(AssertionError("kuaishou called")))
    monkeypatch.setattr(app_module, "collect_official_today", lambda: (_ for _ in ()).throw(AssertionError("official called")))
    monkeypatch.setattr(app_module, "refresh_data", lambda: (_ for _ in ()).throw(AssertionError("legacy refresh called")))
    monkeypatch.setattr(app_module, "run_daily_recovery", lambda: (_ for _ in ()).throw(AssertionError("daily recovery called")))
    monkeypatch.setattr(app_module, "update_collector_runtime", lambda **kwargs: None)

    app_module.startup_event()

    output = capsys.readouterr().out
    assert init_calls == ["init_operations_tables"]
    assert "operations_db_init_completed cloud=skipped sqlite=ok" in output
    assert scheduler.calls == []
    assert scheduler.running is False
