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
    ):
        monkeypatch.delenv(name, raising=False)

    assert get_scheduler_runtime_flags() == {
        "catch_up_scheduler_enabled": True,
        "collector_scheduler_enabled": True,
        "legacy_refresh_scheduler_enabled": False,
        "daily_recovery_enabled": False,
        "historical_catchup_enabled": False,
    }


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


def test_runtime_diagnostics_endpoint_registered():
    from app import app

    routes = {getattr(route, "path", None) for route in app.routes}
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.update(getattr(child, "path", None) for child in original_router.routes)
    assert "/api/runtime-diagnostics" in routes
