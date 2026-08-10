from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import collector, learning, next_prediction, official_verification
from database import collector_store, official_draw_store, prediction_history_store


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "static" / "dashboard.html"


def test_prediction_history_summary_view_uses_slim_reader(monkeypatch):
    called = {"full": 0, "summary": 0}

    monkeypatch.setattr(
        next_prediction,
        "get_prediction_history_records",
        lambda limit: called.__setitem__("full", called["full"] + 1) or [],
    )
    monkeypatch.setattr(
        next_prediction,
        "get_prediction_history_summary_records",
        lambda limit: called.__setitem__("summary", called["summary"] + 1) or [{"model_scores": {}}],
    )

    payload = next_prediction.api_prediction_history(limit=500, view="summary")

    assert payload["view"] == "summary"
    assert payload["data"] == [{"model_scores": {}}]
    assert called == {"full": 0, "summary": 1}


def test_prediction_history_full_view_stays_default(monkeypatch):
    called = {"full": 0, "summary": 0}

    monkeypatch.setattr(
        next_prediction,
        "get_prediction_history_records",
        lambda limit: called.__setitem__("full", called["full"] + 1) or [{"model_scores": {"full": True}}],
    )
    monkeypatch.setattr(
        next_prediction,
        "get_prediction_history_summary_records",
        lambda limit: called.__setitem__("summary", called["summary"] + 1) or [],
    )

    payload = next_prediction.api_prediction_history()

    assert payload["view"] == "full"
    assert payload["data"] == [{"model_scores": {"full": True}}]
    assert called == {"full": 1, "summary": 0}


def test_official_history_summary_omits_raw_json(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        official_draw_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert official_draw_store.get_official_draw_summary_history(limit=20) == []

    assert "raw_json" not in captured["sql"].lower()


def test_kuaishou_history_summary_omits_raw_payloads(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        collector_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert collector_store.get_kuaishou_summary_history(limit=20) == []

    sql = captured["sql"].lower()
    assert "raw_html" not in sql
    assert "parsed_json" not in sql


def test_kuaishou_history_full_view_stays_default(monkeypatch):
    called = {"full": 0, "summary": 0}

    monkeypatch.setattr(
        collector,
        "get_kuaishou_history",
        lambda limit: called.__setitem__("full", called["full"] + 1) or [{"raw_html": "<html>"}],
    )
    monkeypatch.setattr(
        collector,
        "get_kuaishou_summary_history",
        lambda limit: called.__setitem__("summary", called["summary"] + 1) or [],
    )

    payload = collector.api_kuaishou_history()

    assert payload["view"] == "full"
    assert payload["data"] == [{"raw_html": "<html>"}]
    assert called == {"full": 1, "summary": 0}


def test_kuaishou_history_full_view_keeps_requested_limit(monkeypatch):
    captured: dict[str, int] = {}

    monkeypatch.setattr(
        collector,
        "get_kuaishou_history",
        lambda limit: captured.setdefault("limit", limit) or [],
    )

    payload = collector.api_kuaishou_history(limit=500, view="full")

    assert payload["view"] == "full"
    assert captured["limit"] == 500


def test_kuaishou_history_summary_view_clamps_limit(monkeypatch):
    captured: dict[str, int] = {}

    monkeypatch.setattr(
        collector,
        "get_kuaishou_summary_history",
        lambda limit: captured.setdefault("limit", limit) or [{"raw_html": None, "parsed_json": {}}],
    )

    payload = collector.api_kuaishou_history(limit=500, view="summary")

    assert captured["limit"] == 100
    assert payload["view"] == "summary"


def test_official_history_summary_view_clamps_limit(monkeypatch):
    captured: dict[str, int] = {}

    def fake_summary(limit):
        captured["limit"] = limit
        return {"status": "ok", "view": "summary", "data": []}

    monkeypatch.setattr(official_verification, "official_summary_history", fake_summary)

    payload = official_verification.api_official_history(limit=500, view="summary")

    assert captured["limit"] == 100
    assert payload["view"] == "summary"


def test_learning_history_adds_full_view_without_changing_summary_default(monkeypatch):
    called = {"full": 0, "summary": 0}

    monkeypatch.setattr(
        learning,
        "get_learning_history",
        lambda **filters: called.__setitem__("summary", called["summary"] + 1) or {"status": "ok", "data": []},
    )
    monkeypatch.setattr(
        learning,
        "get_learning_records",
        lambda **filters: called.__setitem__("full", called["full"] + 1) or [{"model_weight": {"full": True}}],
    )

    summary = learning.api_learning_history()
    full = learning.api_learning_history(view="full")

    assert summary["view"] == "summary"
    assert full["view"] == "full"
    assert called == {"full": 1, "summary": 1}


def test_prediction_summary_store_omits_large_payload_columns(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert prediction_history_store.get_prediction_history_summary_records(limit=20) == []

    sql = captured["sql"].lower()
    assert "p.reasons" not in sql
    assert "p.model_scores" not in sql
    assert "git_commit_hash" not in sql


def test_prediction_statistics_uses_summary_records(monkeypatch):
    called = {"full": 0, "summary": 0}
    prediction_history_store._PREDICTION_STATS_CACHE["payload"] = {}
    prediction_history_store._PREDICTION_STATS_CACHE["expires_at"] = {}

    monkeypatch.setattr(prediction_history_store, "_learned_prediction_issues", lambda: set())
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_history_records",
        lambda limit: called.__setitem__("full", called["full"] + 1) or [],
    )
    monkeypatch.setattr(
        prediction_history_store,
        "get_prediction_history_summary_records",
        lambda limit: called.__setitem__("summary", called["summary"] + 1) or [],
    )

    payload = prediction_history_store.get_prediction_history_statistics(limit=7)

    assert payload["status"] == "empty"
    assert called == {"full": 0, "summary": 1}


def test_dashboard_does_not_poll_full_history_endpoints():
    html = DASHBOARD_HTML.read_text(encoding="utf-8")

    assert "/api/prediction-history/history" not in html
    assert "/api/kuaishou/history" not in html
    assert "/api/official/history" not in html
