from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import app as app_module
from api import collector as collector_api


def test_core_status_route_functions(monkeypatch):
    monkeypatch.setattr(collector_api, "get_collector_status", lambda: {"status": "ok"})
    monkeypatch.setattr(collector_api, "collector_runtime_status", lambda: {"collector_running": False})
    monkeypatch.setattr(collector_api, "scan_collector_gaps", lambda: {"status": "ok", "missing_count": 0})

    assert app_module.api_health()["status"] == "ok"
    assert collector_api.api_collector_status()["status"] == "ok"
    assert collector_api.api_collector_gaps()["missing_count"] == 0
    assert app_module.dashboard_page().status_code == 200
    assert app_module.dashboard_head().status_code == 200
