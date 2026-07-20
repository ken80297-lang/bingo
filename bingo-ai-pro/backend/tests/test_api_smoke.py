from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app as app_module
from api import collector as collector_api


class DummyRequest:
    method = "GET"
    headers = {"user-agent": "bingo-ai-pro-github-actions-keep-awake"}


def test_core_status_route_functions(monkeypatch):
    monkeypatch.setattr(collector_api, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_api, "collector_runtime_status", lambda: {"collector_running": False})
    monkeypatch.setattr(collector_api, "scan_collector_gaps", lambda: {"status": "ok", "missing_count": 0})

    health = app_module.api_health(DummyRequest())
    assert health["status"] == "ok"
    assert health["service"] == "bingo-ai-pro"
    assert "instance_started_at" in health
    assert collector_api.api_collector_status()["status"] == "ok"
    assert collector_api.api_collector_gaps()["missing_count"] == 0
    assert app_module.dashboard_page().status_code == 200
    assert app_module.dashboard_head().status_code == 200


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
