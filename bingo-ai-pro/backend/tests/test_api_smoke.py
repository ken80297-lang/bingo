from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app as app_module
from api import collector as collector_api
from fastapi import HTTPException


class DummyRequest:
    method = "GET"
    headers = {"user-agent": "bingo-ai-pro-github-actions-keep-awake"}


def test_core_status_route_functions(monkeypatch):
    monkeypatch.setattr(collector_api, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_api, "collector_runtime_status", lambda: {"collector_running": False})
    monkeypatch.setattr(collector_api, "get_cached_collector_gaps", lambda: {"status": "ok", "missing_count": 0})
    monkeypatch.setattr(collector_api, "get_catch_up_status", lambda fetch_source=False: {"status": "ok"})
    monkeypatch.setattr(
        collector_api,
        "catch_up_missing_issues",
        lambda force=False: {"status": "ok", "forced": force},
    )

    health = app_module.api_health(DummyRequest())
    assert health["status"] == "ok"
    assert health["service"] == "bingo-ai-pro"
    assert "instance_started_at" in health
    assert collector_api.api_collector_status()["status"] == "ok"
    assert collector_api.api_collector_gaps()["missing_count"] == 0
    catch_up_status = collector_api.api_collector_catch_up()
    assert catch_up_status["read_only"] is True
    assert catch_up_status["execution_triggered"] is False
    assert collector_api.api_collector_catch_up_run(force=True)["forced"] is True
    assert app_module.dashboard_page().status_code == 200
    assert app_module.dashboard_head().status_code == 200
    assert app_module.root_page().status_code == 200
    assert app_module.root_head().status_code == 200


def test_collector_catch_up_post_requires_admin_token(monkeypatch):
    monkeypatch.setenv("COLLECTOR_ADMIN_TOKEN", "secret-test-token")

    try:
        collector_api.require_collector_admin(None)
        raise AssertionError("missing token should be rejected")
    except HTTPException as exc:
        assert exc.status_code == 403

    try:
        collector_api.require_collector_admin("wrong-token")
        raise AssertionError("wrong token should be rejected")
    except HTTPException as exc:
        assert exc.status_code == 403

    assert collector_api.require_collector_admin("secret-test-token") is None


def test_health_records_wake_status():
    app_module.app.state.health_request_count_since_start = 0
    app_module.app.state.last_health_request_at = None

    health = app_module.api_health(DummyRequest())
    app_module.api_health_head(DummyRequest())
    app_module.api_health(DummyRequest())
    wake = app_module.api_health_wake_status()

    assert health["status"] == "ok"
    assert wake["health_request_count_since_start"] == 3
    assert wake["wake_source"] == "github-actions"
    assert wake["wake_status"] == "healthy"
