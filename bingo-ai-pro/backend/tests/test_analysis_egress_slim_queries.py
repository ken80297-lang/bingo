from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import analysis_history as analysis_history_api
from database import analysis_store


LARGE_ANALYSIS_COLUMNS = (
    "consecutive_numbers",
    "repeated_numbers",
    "hot_numbers",
    "cold_numbers",
    "missing_numbers",
    "difference_values",
    "diagonal_pattern",
    "laowanjia_score",
    "ai_score",
    "tail_distribution",
    "hot_zone",
    "cold_zone",
    "patch_numbers",
)


def test_analysis_summary_records_omit_large_payload_columns(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        analysis_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert analysis_store.get_analysis_summary_records(limit=20) == []

    sql = captured["sql"].lower()
    for column in LARGE_ANALYSIS_COLUMNS:
        assert column not in sql


def test_analysis_summary_shape_keeps_large_keys_empty(monkeypatch):
    row = (
        "115040901",
        "2026-08-10T00:00:00+08:00",
        "[1,2,3]",
        1,
        "small",
        "odd",
        "2026-08-10T00:00:00+08:00",
        "2026-08-10T00:01:00+08:00",
        "cluster",
        80.0,
        12.0,
        4.0,
        "pattern",
        "ai-pattern",
    )
    monkeypatch.setattr(analysis_store, "_query_with_fallback", lambda sql, params=(), sqlite_sql=None: [row])

    record = analysis_store.get_analysis_summary_records(limit=20)[0]

    assert record["numbers"] == [1, 2, 3]
    assert record["hot_numbers"] == []
    assert record["difference_values"] == {}
    assert record["ai_score"] == {}
    assert record["patch_numbers"] == []


def test_analysis_statistics_uses_sql_aggregate_not_full_history(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        analysis_store,
        "get_analysis_history",
        lambda limit=100: (_ for _ in ()).throw(AssertionError("full history should not be used")),
    )

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append(sql.lower())
        if "avg(score)" in sql.lower():
            return [(2, "115040902", "2026-08-10T00:01:00+08:00", 55.5)]
        return [("cluster", 2)]

    monkeypatch.setattr(analysis_store, "_query_with_fallback", fake_query)

    payload = analysis_store.get_analysis_statistics(limit=100)

    assert payload["analysis_count"] == 2
    assert payload["average_laowanjia_score"] == 55.5
    assert payload["cluster_distribution"] == {"cluster": 2}
    assert len(calls) == 2


def test_analysis_history_api_defaults_to_full_reader(monkeypatch):
    called = {"full": 0, "summary": 0}

    monkeypatch.setattr(
        analysis_history_api,
        "get_analysis_history",
        lambda limit: called.__setitem__("full", called["full"] + 1) or [{"issue": "115040901", "hot_numbers": [1]}],
    )
    monkeypatch.setattr(
        analysis_history_api,
        "get_analysis_summary_records",
        lambda limit: called.__setitem__("summary", called["summary"] + 1) or [],
    )

    payload = analysis_history_api.api_analysis_history()

    assert payload == {"status": "ok", "view": "full", "data": [{"issue": "115040901", "hot_numbers": [1]}]}
    assert called == {"full": 1, "summary": 0}


def test_analysis_history_api_summary_view_uses_summary_reader_and_clamps_limit(monkeypatch):
    captured: dict[str, int] = {}

    def fake_summary(limit):
        captured["limit"] = limit
        return [{"issue": "115040901"}]

    monkeypatch.setattr(analysis_history_api, "get_analysis_summary_records", fake_summary)

    payload = analysis_history_api.api_analysis_history(limit=500, view="summary")

    assert captured["limit"] == 100
    assert payload == {"status": "ok", "view": "summary", "data": [{"issue": "115040901"}]}


def test_analysis_core_full_reader_stays_available():
    source = pathlib.Path(analysis_store.__file__).read_text(encoding="utf-8")

    assert "def get_analysis_history(limit: int = 100)" in source
    assert "def get_analysis_summary_records(limit: int = 20)" in source
