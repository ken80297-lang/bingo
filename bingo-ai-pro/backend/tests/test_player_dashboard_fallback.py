from __future__ import annotations

import pathlib
import sys
from concurrent.futures import Future

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import player_dashboard


def test_player_dashboard_timeout_uses_last_good_cache(monkeypatch):
    monkeypatch.setattr(player_dashboard, "PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS", 0.01)
    player_dashboard._PLAYER_COMPONENT_CACHE["latest_prediction"] = {
        "issue": "115000001",
        "prediction_issue": "115000002",
        "recommend_numbers": list(range(1, 21)),
    }
    future = Future()
    warnings: list[str] = []

    result = player_dashboard._future_result("latest_prediction", future, warnings)

    assert result["prediction_issue"] == "115000002"
    assert warnings == ["latest_prediction fallback cache"]


def test_player_dashboard_timeout_rejects_non_production_latest_cache(monkeypatch):
    monkeypatch.setattr(player_dashboard, "PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS", 0.01)
    player_dashboard._PLAYER_COMPONENT_CACHE["latest_prediction"] = {
        "issue": "120",
        "prediction_issue": "121",
        "recommend_numbers": list(range(1, 21)),
    }
    future = Future()
    warnings: list[str] = []

    result = player_dashboard._future_result("latest_prediction", future, warnings)

    assert result is None
    assert warnings == ["latest_prediction fallback cache"]
