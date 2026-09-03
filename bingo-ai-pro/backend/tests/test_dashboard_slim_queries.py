from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store, official_draw_store, prediction_history_store
from services import player_dashboard


def test_prediction_history_summary_query_omits_large_dashboard_columns(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert prediction_history_store.get_prediction_history_summary_records(100) == []

    sql = captured["sql"].lower()
    assert "p.reasons" not in sql
    assert "p.model_scores" not in sql
    assert "git_commit_hash" not in sql
    assert "model_version" not in sql
    assert "feature_version" not in sql


def test_prediction_source_target_summary_query_omits_large_dashboard_columns(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        prediction_history_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert prediction_history_store.get_prediction_summary_for_source_target("115040901", "115040902") is None

    sql = captured["sql"].lower()
    assert "reasons" not in sql
    assert "model_scores" not in sql
    assert "git_commit_hash" not in sql


def test_official_draw_summary_queries_omit_raw_json(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        official_draw_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None, **kwargs: captured.setdefault("sql", sql) and [],
    )

    assert official_draw_store.get_latest_official_draw_summary() is None

    assert "raw_json" not in captured["sql"].lower()


def test_kuaishou_summary_query_omits_raw_payloads(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        collector_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None, **kwargs: captured.setdefault("sql", sql) and [],
    )

    assert collector_store.get_latest_kuaishou_summary() is None

    sql = captured["sql"].lower()
    assert "raw_html" not in sql
    assert "parsed_json" not in sql


def test_player_dashboard_uses_single_shared_prediction_history_query():
    source = pathlib.Path(player_dashboard.__file__).read_text(encoding="utf-8")

    assert "get_prediction_history_records(PLAYER_DASHBOARD_HISTORY_LIMIT)" not in source
    assert "history_records = card_two_history[:PLAYER_DASHBOARD_HISTORY_LIMIT]" in source
