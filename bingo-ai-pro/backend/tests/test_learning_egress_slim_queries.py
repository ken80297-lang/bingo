from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import learning_store
from services import learning_engine


LARGE_LEARNING_COLUMNS = (
    "prediction_snapshot",
    "analysis_snapshot",
    "predicted_scores",
    "model_weight",
)


def test_learning_summary_records_omit_large_payload_columns(monkeypatch):
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        learning_store,
        "_query_with_fallback",
        lambda sql, params=(), sqlite_sql=None: captured.setdefault("sql", sql) and [],
    )

    assert learning_store.get_learning_summary_records(limit=100) == []

    sql = captured["sql"].lower()
    for column in LARGE_LEARNING_COLUMNS:
        assert column not in sql


def test_learning_history_api_uses_summary_reader(monkeypatch):
    called = {"summary": 0, "full": 0}

    monkeypatch.setattr(
        learning_engine,
        "get_learning_summary_records",
        lambda **filters: called.__setitem__("summary", called["summary"] + 1) or [{"issue": "115040901"}],
    )
    monkeypatch.setattr(
        learning_engine,
        "get_learning_records",
        lambda **filters: called.__setitem__("full", called["full"] + 1) or [],
    )

    payload = learning_engine.get_learning_history(limit=100)

    assert payload["data"] == [{"issue": "115040901"}]
    assert called == {"summary": 1, "full": 0}


def test_learning_summary_history_keeps_large_payload_keys_empty(monkeypatch):
    row = (
        1,
        "115040901",
        "2026-08-10T00:00:00+08:00",
        "laowanjia",
        "v7",
        "live_prediction",
        "[1,2,3]",
        "[1,4,5]",
        "[1]",
        1,
        0.2,
        0.8,
        5,
        "verified",
        "learned",
        "2026-08-10T00:01:00+08:00",
        "2026-08-10T00:00:00+08:00",
        "2026-08-10T00:01:00+08:00",
        None,
        "115040900",
        "115040901",
        "115040900",
        "2026-08-10T00:00:00+08:00",
        3,
        1.0,
        2,
        "115040901",
        "saved",
        "feature-v1",
        "v6",
        "v7",
        False,
        "115040901",
        None,
        None,
    )

    monkeypatch.setattr(learning_store, "_query_with_fallback", lambda sql, params=(), sqlite_sql=None: [row])

    payload = learning_engine.get_learning_history(limit=100)
    record = payload["data"][0]

    assert record["predicted_numbers"] == [1, 2, 3]
    assert record["predicted_scores"] == {}
    assert record["model_weight"] == {}
    assert record["prediction_snapshot"] == {}
    assert record["analysis_snapshot"] == {}


def test_learning_core_snapshot_path_stays_on_full_reader():
    source = pathlib.Path(learning_engine.__file__).read_text(encoding="utf-8")

    start = source.index("def _learning_snapshots_for_issue")
    end = source.index("def _resolve_pending_snapshot")
    helper = source[start:end]
    assert "get_learning_records(" in helper
    assert "get_learning_summary_records(" not in helper


def test_learning_observation_reuses_single_live_summary_read(monkeypatch):
    calls = {"summary": 0, "models": 0}
    records = [
        {
            "issue": "115040901",
            "target_issue": "115040901",
            "source_issue": "115040900",
            "prediction_type": "live_prediction",
            "model_name": "laowanjia",
            "top_n": 5,
            "learned_status": "learned",
            "verification_status": "verified",
            "prediction_created_at": "2026-08-10T00:00:00+08:00",
        }
    ]

    monkeypatch.setattr(learning_engine, "get_learning_status_counts", lambda: {
        "total_records": 1,
        "live_prediction_count": 1,
        "historical_backtest_count": 0,
        "learned_records": 1,
        "pending_records": 0,
        "pending_official_records": 0,
        "pending_target_records": 0,
        "resolved_pending_records": 0,
        "missing_snapshot_records": 0,
        "failed_records": 0,
        "error_records": 0,
        "evaluation_error_records": 0,
    })
    monkeypatch.setattr(
        learning_engine,
        "get_learning_summary_records",
        lambda **filters: calls.__setitem__("summary", calls["summary"] + 1) or records,
    )

    def fake_models_summary(live_records=None):
        calls["models"] += 1
        assert live_records is records
        return {"models": []}

    monkeypatch.setattr(learning_engine, "get_learning_models_summary", fake_models_summary)
    monkeypatch.setattr(learning_engine, "official_statistics", lambda: {})
    monkeypatch.setattr(learning_engine, "get_catch_up_status", lambda fetch_source=False: {})
    monkeypatch.setattr(learning_engine, "analysis_engine_status", lambda: {})

    payload = learning_engine._build_learning_observation()

    assert payload["status"] == "ok"
    assert calls == {"summary": 1, "models": 1}
