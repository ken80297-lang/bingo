from __future__ import annotations

import sqlite3

from database import shadow_dynamic_prediction_store as store
from services import shadow_dynamic_observer as observer


def _analysis(issue: str, candidates: dict[str, list[int]]) -> dict:
    return {
        "issue": issue,
        "ai_score": {
            key: {"candidate_numbers": numbers}
            for key, numbers in candidates.items()
        },
    }


def _learning_row(issue: str, candidates: dict[str, list[int]], official: list[int]) -> dict:
    return {
        "issue": issue,
        "analysis_snapshot": _analysis(str(int(issue) - 1), candidates),
        "official_numbers": official,
    }


def test_rule_samples_exclude_future_and_test_rows(monkeypatch):
    calls = []
    rows = [
        _learning_row("115000104", {"multi_window_hot_cold": [1, 2, 3]}, list(range(1, 21))),
        _learning_row("115000103", {"multi_window_hot_cold": [1, 2, 3]}, list(range(1, 21))),
        _learning_row("991000001", {"multi_window_hot_cold": [1, 2, 3]}, list(range(1, 21))),
        _learning_row("115000102", {"multi_window_hot_cold": [40, 41]}, list(range(40, 60))),
    ]

    def fake_get_learning_records(**kwargs):
        calls.append(kwargs)
        return rows if kwargs.get("offset") == 0 else []

    monkeypatch.setattr("database.learning_store.get_learning_records", fake_get_learning_records)

    samples = observer._query_rule_samples("115000103", limit=10)

    assert [item["issue"] for item in samples] == ["115000102", "115000103"]
    assert all(int(item["issue"]) <= 115000103 for item in samples)
    assert calls[0]["prediction_type"] == "live_prediction"


def test_b_and_bc_weight_calculation_negative_lift_zero_and_caps():
    metrics = {
        "multi_window_hot_cold": {
            "sample_size": 56,
            "long_term_lift": 0.3929,
            "recent20_lift": 0.5,
            "volatility": 1.8193,
            "confidence": 0.1939,
        },
        "long_dragon": {
            "sample_size": 86,
            "long_term_lift": 0.0262,
            "recent20_lift": 0.2125,
            "volatility": 0.9158,
            "confidence": 0.2676,
        },
        "tail_trend_strength": {
            "sample_size": 86,
            "long_term_lift": 0.1047,
            "recent20_lift": -0.1,
            "volatility": 1.6356,
            "confidence": 0.177,
        },
        "omission_strength": {
            "sample_size": 86,
            "long_term_lift": -0.0233,
            "recent20_lift": -0.2,
            "volatility": 1.7048,
            "confidence": 0.1561,
        },
    }

    b = observer._rule_weights(metrics, "long_term_lift")
    bc = observer._rule_weights(metrics, "long_term_conf_vol")

    assert b["omission_strength"] == 0
    assert bc["omission_strength"] == 0
    assert b["multi_window_hot_cold"] > b["long_dragon"]
    assert bc["multi_window_hot_cold"] > 0
    assert bc["multi_window_hot_cold"] < b["multi_window_hot_cold"]
    assert bc["long_dragon"] < 0.01
    assert bc["tail_trend_strength"] < b["tail_trend_strength"]


def test_shadow_recommendation_does_not_mutate_production_numbers(monkeypatch):
    monkeypatch.setattr(
        observer,
        "_query_rule_samples",
        lambda based_on: [
            {
                "issue": "115000101",
                "analysis": _analysis("115000100", {"multi_window_hot_cold": [21, 22, 23, 24, 25]}),
                "official_numbers": [21, 22, 23, 24, 25] + list(range(40, 55)),
            }
            for _ in range(12)
        ],
    )
    production = list(range(1, 21))
    original = list(production)
    payloads = observer.build_shadow_dynamic_predictions(
        based_on_issue="115000103",
        prediction_issue="115000104",
        production_numbers=production,
        analysis=_analysis("115000103", {"multi_window_hot_cold": [21, 22, 23, 24, 25]}),
        generated_at="2026-09-29T00:00:00+00:00",
    )

    assert production == original
    assert {item["strategy"] for item in payloads} == {"long_term_lift", "long_term_conf_vol"}
    assert all(len(item["recommend_numbers"]) == 20 for item in payloads)


def _connect_factory(path):
    def connect():
        return sqlite3.connect(path)

    return connect


def test_idempotent_insert_and_pending_to_verified(monkeypatch, tmp_path):
    db_path = tmp_path / "shadow.db"
    monkeypatch.setattr(store, "_cloud_enabled", lambda: False)
    monkeypatch.setattr(store, "_sqlite_connection", _connect_factory(db_path))
    store._INITIALIZED = False

    item = {
        "based_on_issue": "115000100",
        "prediction_issue": "115000101",
        "strategy": "long_term_lift",
        "recommend_numbers": list(range(1, 21)),
        "production_numbers": list(range(11, 31)),
        "rule_weights": {"multi_window_hot_cold": 0.3},
        "rule_metrics": {"multi_window_hot_cold": {"sample_size": 50}},
        "generated_at": "2026-09-29T00:00:00+00:00",
        "algorithm_version": "test-v1",
        "git_commit": "abc",
    }

    first = store.save_shadow_dynamic_prediction(item)
    second = store.save_shadow_dynamic_prediction(item)
    verified = store.verify_shadow_dynamic_predictions("115000101", list(range(1, 21)), list(range(11, 31)))
    item["recommend_numbers"] = list(range(40, 60))
    third = store.save_shadow_dynamic_prediction(item)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select recommend_numbers, hit_count, production_hit_count, delta_vs_production, status from shadow_dynamic_predictions"
        ).fetchall()

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert verified["updated"] == 1
    assert third["status"] == "ok"
    assert len(rows) == 1
    assert rows[0][1:] == (20, 10, 10, "verified")
    assert rows[0][0].startswith("[1,")
