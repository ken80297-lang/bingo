from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_runtime_diagnostics_returns_process_local_metadata(monkeypatch):
    from api.runtime_diagnostics import api_runtime_diagnostics
    from app import app

    monkeypatch.setenv("RENDER_GIT_COMMIT", "0675b989a05e86d67c415af83e438e6ab4e51b08")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("RENDER_SERVICE_NAME", "bingo-ai-pro")
    monkeypatch.setenv("RENDER_INSTANCE_ID", "inst-test")
    monkeypatch.delenv("DISABLE_PRODUCTION_OFFICIAL_SCHEDULERS", raising=False)
    monkeypatch.setenv("CATCH_UP_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("COLLECTOR_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("LEGACY_REFRESH_SCHEDULER_ENABLED", "off")
    monkeypatch.setenv("DAILY_RECOVERY_ENABLED", "no")
    monkeypatch.setenv("HISTORICAL_CATCHUP_ENABLED", "true")

    request = type("Request", (), {"app": app})()
    payload = api_runtime_diagnostics(request)

    assert payload["status"] == "ok"
    assert payload["instance_started_at"] == app.state.instance_started_at
    assert payload["render"] == {
        "git_commit": "0675b989a05e86d67c415af83e438e6ab4e51b08",
        "service_id": "srv-test",
        "service_name": "bingo-ai-pro",
        "instance_id": "inst-test",
    }
    assert payload["scheduler_flags"] == {
        "catch_up_scheduler_enabled": False,
        "collector_scheduler_enabled": False,
        "legacy_refresh_scheduler_enabled": False,
        "daily_recovery_enabled": False,
        "historical_catchup_enabled": True,
    }


def test_runtime_diagnostics_missing_render_metadata_is_null(monkeypatch):
    from api.runtime_diagnostics import api_runtime_diagnostics
    from app import app

    for name in (
        "RENDER_GIT_COMMIT",
        "RENDER_SERVICE_ID",
        "RENDER_SERVICE_NAME",
        "RENDER_INSTANCE_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    request = type("Request", (), {"app": app})()
    payload = api_runtime_diagnostics(request)

    assert payload["render"] == {
        "git_commit": None,
        "service_id": None,
        "service_name": None,
        "instance_id": None,
    }


def test_runtime_diagnostics_uses_scheduler_flag_defaults(monkeypatch):
    from config.runtime_flags import get_scheduler_runtime_flags

    for name in (
        "CATCH_UP_SCHEDULER_ENABLED",
        "COLLECTOR_SCHEDULER_ENABLED",
        "LEGACY_REFRESH_SCHEDULER_ENABLED",
        "DAILY_RECOVERY_ENABLED",
        "HISTORICAL_CATCHUP_ENABLED",
        "DISABLE_PRODUCTION_OFFICIAL_SCHEDULERS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert get_scheduler_runtime_flags() == {
        "catch_up_scheduler_enabled": False,
        "collector_scheduler_enabled": False,
        "legacy_refresh_scheduler_enabled": False,
        "daily_recovery_enabled": False,
        "historical_catchup_enabled": False,
    }


def test_runtime_diagnostics_scheduler_flags_are_explicit_opt_in(monkeypatch):
    from config.runtime_flags import get_scheduler_runtime_flags

    cases = (
        ({}, False, False),
        ({"CATCH_UP_SCHEDULER_ENABLED": "false", "COLLECTOR_SCHEDULER_ENABLED": "false"}, False, False),
        ({"CATCH_UP_SCHEDULER_ENABLED": "true", "COLLECTOR_SCHEDULER_ENABLED": "true"}, True, True),
        ({"CATCH_UP_SCHEDULER_ENABLED": "true", "COLLECTOR_SCHEDULER_ENABLED": "false"}, True, False),
        ({"CATCH_UP_SCHEDULER_ENABLED": "false", "COLLECTOR_SCHEDULER_ENABLED": "true"}, False, True),
    )

    for env, expected_catch_up, expected_collector in cases:
        for name in (
            "CATCH_UP_SCHEDULER_ENABLED",
            "COLLECTOR_SCHEDULER_ENABLED",
            "DISABLE_PRODUCTION_OFFICIAL_SCHEDULERS",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        flags = get_scheduler_runtime_flags()

        assert flags["catch_up_scheduler_enabled"] is expected_catch_up
        assert flags["collector_scheduler_enabled"] is expected_collector


def test_runtime_diagnostics_env_false_is_not_overridden_by_disable_flag(monkeypatch):
    from config.runtime_flags import get_scheduler_runtime_flags

    monkeypatch.setenv("DISABLE_PRODUCTION_OFFICIAL_SCHEDULERS", "false")
    monkeypatch.setenv("CATCH_UP_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("COLLECTOR_SCHEDULER_ENABLED", "false")

    flags = get_scheduler_runtime_flags()

    assert flags["catch_up_scheduler_enabled"] is False
    assert flags["collector_scheduler_enabled"] is False


def test_runtime_diagnostics_bool_parser_matches_scheduler_semantics(monkeypatch):
    from config.runtime_flags import env_bool

    for raw in ("false", "FALSE", "0", "no", "off", "random"):
        monkeypatch.setenv("BINGO_TEST_FLAG", raw)
        assert env_bool("BINGO_TEST_FLAG", True) is False

    for raw in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("BINGO_TEST_FLAG", raw)
        assert env_bool("BINGO_TEST_FLAG", False) is True

    monkeypatch.delenv("BINGO_TEST_FLAG", raising=False)
    assert env_bool("BINGO_TEST_FLAG", True) is True
    monkeypatch.setenv("BINGO_TEST_FLAG", "")
    assert env_bool("BINGO_TEST_FLAG", False) is False


def test_runtime_diagnostics_does_not_expose_raw_env_or_secrets(monkeypatch):
    from api.runtime_diagnostics import api_runtime_diagnostics
    from app import app

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("SUPABASE_KEY", "secret-key")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.setenv("CATCH_UP_SCHEDULER_ENABLED", "false")

    request = type("Request", (), {"app": app})()
    payload = api_runtime_diagnostics(request)
    text = repr(payload)

    assert "DATABASE_URL" not in text
    assert "SUPABASE_KEY" not in text
    assert "API_TOKEN" not in text
    assert "postgres://secret" not in text
    assert "secret-key" not in text
    assert "secret-token" not in text
    assert "CATCH_UP_SCHEDULER_ENABLED" not in text
    assert "<unset>" not in text


def test_runtime_diagnostics_exposes_collector_db_path_without_db_or_work(monkeypatch):
    import database
    from api import runtime_diagnostics
    from app import app
    from database import collector_store, prediction_history_store
    from services import latest_sync

    def fail(name):
        def _raise(*args, **kwargs):
            raise AssertionError(f"{name} should not run from runtime diagnostics")

        return _raise

    collector_store._record_db_path_status(
        backend="postgres",
        result="success",
        fallback_occurred=False,
        error_type=None,
    )
    prediction_history_store._CARD_TWO_HISTORY_TIMINGS.clear()
    prediction_history_store._record_card_two_history_timing(
        {
            "type": "stage",
            "stage": "main_query",
            "duration_ms": 123.45,
            "result": "success",
            "db_timing": {
                "query_tag": "card_two_history.main_query",
                "pool_acquire_ms": 1.23,
                "execute_ms": 45.67,
                "fetch_ms": 0.89,
                "backend_pid": 12345,
                "connection_hash": "abc123def456",
                "connection_reused": False,
                "transaction_status_before": "IDLE",
                "transaction_status_after": "INTRANS",
                "connection_age_ms": 2.34,
                "result": "success",
                "row_count": 12,
            },
        }
    )
    monkeypatch.setattr(database, "get_connection", fail("database.get_connection"))
    monkeypatch.setattr(collector_store, "_cloud_connection", fail("collector_store._cloud_connection"))
    monkeypatch.setattr(collector_store, "_sqlite_connection", fail("collector_store._sqlite_connection"))
    monkeypatch.setattr(collector_store, "_query_cloud", fail("collector_store._query_cloud"))
    monkeypatch.setattr(collector_store, "_query_sqlite", fail("collector_store._query_sqlite"))
    monkeypatch.setattr(collector_store, "init_collector_tables", fail("collector_store.init_collector_tables"))
    monkeypatch.setattr(collector_store, "save_kuaishou_snapshot", fail("collector_store.save_kuaishou_snapshot"))
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        fail("prediction_history_store._query_with_fallback"),
    )
    monkeypatch.setattr(
        prediction_history_store,
        "_query_cloud",
        fail("prediction_history_store._query_cloud"),
    )
    monkeypatch.setattr(
        prediction_history_store,
        "_query_sqlite",
        fail("prediction_history_store._query_sqlite"),
    )
    monkeypatch.setattr(
        prediction_history_store,
        "init_prediction_history_tables",
        fail("prediction_history_store.init_prediction_history_tables"),
    )
    monkeypatch.setattr(latest_sync, "get_latest_sync_snapshot", fail("latest_sync.get_latest_sync_snapshot"))

    request = type("Request", (), {"app": app})()
    payload = runtime_diagnostics.api_runtime_diagnostics(request)

    assert payload["collector_db_path"] == {
        "backend": "postgres",
        "result": "success",
        "fallback_occurred": False,
        "error_type": None,
    }
    assert payload["card_two_history_timing"]["latest"]["db_timing"] == {
        "query_tag": "card_two_history.main_query",
        "pool_acquire_ms": 1.23,
        "execute_ms": 45.67,
        "fetch_ms": 0.89,
        "backend_pid": 12345,
        "connection_hash": "abc123def456",
        "connection_reused": False,
        "transaction_status_before": "IDLE",
        "transaction_status_after": "INTRANS",
        "connection_age_ms": 2.34,
        "result": "success",
        "row_count": 12,
    }
    assert payload["card_two_history_timing"]["limit"] == 20


def test_runtime_diagnostics_endpoint_registered():
    from app import app

    routes = {getattr(route, "path", None) for route in app.routes}
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.update(getattr(child, "path", None) for child in original_router.routes)
    assert "/api/runtime-diagnostics" in routes
    assert "/api/runtime-diagnostics/card-two-roundtrip" in routes
    assert "/api/runtime-diagnostics/card-two-autocommit-roundtrip" in routes
    assert "/api/runtime-diagnostics/card-two-connection-path-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-connection-path-ab-benchmark" in routes
    assert "/api/runtime-diagnostics/session-pooler-connection-classification" in routes
    assert "/api/runtime-diagnostics/card-two-dashboard-context-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-isolated-dashboard-context-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-concurrency-culprit-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-contention-isolation-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-ordering-benchmark" in routes
    assert "/api/runtime-diagnostics/card-two-stepwise-latency-benchmark" in routes


def test_connection_path_ab_benchmark_reports_unavailable_paths_without_db_work(monkeypatch):
    from database import postgres
    from database import prediction_history_store

    monkeypatch.setattr(postgres, "DATABASE_URL", None)
    for name in (
        "DIRECT_DATABASE_URL",
        "DATABASE_DIRECT_URL",
        "SUPABASE_DIRECT_DATABASE_URL",
        "SESSION_POOLER_DATABASE_URL",
        "DATABASE_SESSION_POOLER_URL",
        "SUPABASE_SESSION_POOLER_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    def fail(*args, **kwargs):
        raise AssertionError("benchmark should not connect when no safe DSN is configured")

    monkeypatch.setattr(prediction_history_store, "_diagnostic_connect_path", fail)
    monkeypatch.setattr(prediction_history_store, "_dashboard_read_connection", fail)

    payload = prediction_history_store.run_card_two_connection_path_ab_benchmark()

    assert payload["status"] == "ok"
    assert payload["sample_count"] == 5
    assert set(payload["paths"]) == {
        "CURRENT_TRANSACTION_POOLER",
        "SESSION_POOLER",
        "DIRECT_DATABASE",
    }
    for path in payload["paths"].values():
        assert path["available"] is False
        assert path["sequences"] == []
        assert path["fresh_connections"] == []
        assert path["reused_connections"] == []
        assert path["summary"] is None
        assert path["not_available_reason"] == "configured safe DSN not available"


def test_connection_path_ab_benchmark_sanitizes_dsn_metadata(monkeypatch):
    from database import postgres
    from database.prediction_history_store import _connection_path_dsn_candidates

    monkeypatch.setattr(
        postgres,
        "DATABASE_URL",
        "postgresql://user:secret-current@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require",
    )
    monkeypatch.setenv(
        "SESSION_POOLER_DATABASE_URL",
        "postgresql://user:secret-session@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres?sslmode=require",
    )
    monkeypatch.setenv(
        "DIRECT_DATABASE_URL",
        "postgresql://user:secret-direct@db.project.supabase.co:5432/postgres?sslmode=require",
    )

    candidates = _connection_path_dsn_candidates()
    text = repr(
        {
            name: {
                "available": path["available"],
                "env_var": path["env_var"],
                "endpoint": path["endpoint"],
            }
            for name, path in candidates.items()
        }
    )

    assert "secret-current" not in text
    assert "secret-session" not in text
    assert "secret-direct" not in text
    assert candidates["CURRENT_TRANSACTION_POOLER"]["endpoint"]["port"] == 6543
    assert candidates["CURRENT_TRANSACTION_POOLER"]["endpoint"]["hostname_classification"] == "transaction pooler"
    assert candidates["SESSION_POOLER"]["endpoint"]["port"] == 5432
    assert candidates["SESSION_POOLER"]["endpoint"]["hostname_classification"] == "session pooler"
    assert candidates["DIRECT_DATABASE"]["endpoint"]["hostname_classification"] == "direct"


def test_session_pooler_classification_missing_env_does_not_connect(monkeypatch):
    from database import prediction_history_store

    for name in (
        "DATABASE_SESSION_POOLER_URL",
        "SESSION_POOLER_DATABASE_URL",
        "SUPABASE_SESSION_POOLER_DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    def fail(*args, **kwargs):
        raise AssertionError("network should not run when session pooler env is missing")

    monkeypatch.setattr(prediction_history_store.socket, "getaddrinfo", fail)
    payload = prediction_history_store.classify_session_pooler_connection_failure()

    assert payload["session_pooler_env_present"] is False
    assert payload["parsed_host"] is None
    assert payload["dns_resolution"] == "FAIL"
    assert payload["resolved_address_family"] == "NONE"
    assert payload["tcp_connect_to_host_5432"] == "FAIL"
    assert payload["psycopg_connect"] == "FAIL"
    assert payload["select_one_result"] == "NOT RUN"


def test_session_pooler_classification_sanitizes_credentials():
    from database.prediction_history_store import _parse_diagnostic_conninfo
    from database.prediction_history_store import _sanitize_connection_error

    dsn = "postgresql://postgres.project-ref:secret-pass@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres"
    parsed = _parse_diagnostic_conninfo(dsn)
    message = f"could not connect using {dsn} for postgres.project-ref with secret-pass"

    sanitized = _sanitize_connection_error(message, dsn, parsed)

    assert parsed["host"] == "aws-0-ap-northeast-1.pooler.supabase.com"
    assert parsed["port"] == 5432
    assert parsed["database"] == "postgres"
    assert parsed["username_format"] == "postgres.<project-ref>"
    assert "secret-pass" not in sanitized
    assert "postgres.project-ref" not in sanitized
    assert dsn not in sanitized


def test_session_pooler_classification_handles_invalid_url_port():
    from database.prediction_history_store import _parse_diagnostic_conninfo

    parsed = _parse_diagnostic_conninfo("postgresql://postgres.project:secret@example.test:notaport/postgres")

    assert parsed["scheme"] == "postgresql"
    assert parsed["host"] == "example.test"
    assert parsed["port"] is None
    assert parsed["database"] == "postgres"
    assert parsed["username_format"] == "postgres.<project-ref>"
    assert parsed["parse_error"] == "ValueError"
