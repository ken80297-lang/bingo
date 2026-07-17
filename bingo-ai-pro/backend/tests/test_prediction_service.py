from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import recommendation_center as recommendation_api
from database import prediction_history_store
from services import prediction_refresh, prediction_service


def _recommendation(numbers=None):
    numbers = numbers or list(range(1, 21))
    return {
        "status": "ok",
        "recommendation": {
            "issue": "115000100",
            "target_issue": "115000101",
            "best_strategy": "unit-test",
            "confidence": 88,
            "super_recommendation": {"recommended": [{"number": 7}]},
            "model_scores": {},
            "winning_model": None,
            "results": [
                {
                    "numbers": numbers,
                    "confidence": 88,
                    "strategy": "unit-test",
                }
            ],
        },
    }


def test_prediction_service_creates_single_entry_snapshot(monkeypatch):
    events = []
    saved = []

    monkeypatch.setattr(prediction_service, "get_prediction_history_records", lambda limit: [])
    monkeypatch.setattr(prediction_service, "calculate_recommendation", lambda *args, **kwargs: _recommendation())
    monkeypatch.setattr(prediction_service, "save_prediction_history", lambda record, caller_context=None: saved.append((record, caller_context)) or {"status": "ok", "id": 42, "storage": "cloud"})
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: events.append(kwargs))

    result = prediction_service.create_for_official_draw(
        "115000100",
        source="official_collector",
        trigger="draw_collected",
    )

    assert result["status"] == "created"
    assert result["target_issue"] == "115000101"
    assert result["recommended_count"] == 20
    assert saved[0][1] == "prediction_service"
    assert saved[0][0]["prediction_status"] == "waiting_draw"
    assert saved[0][0]["learning_used"] is False
    assert [event["event_type"] for event in events] == ["prediction_create_started", "prediction_created"]


def test_prediction_service_skips_invalid_target(monkeypatch):
    monkeypatch.setattr(prediction_service, "calculate_recommendation", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not calculate")))
    monkeypatch.setattr(prediction_service, "save_prediction_history", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not save")))
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: None)

    result = prediction_service.create_for_official_draw(
        "115000100",
        source="unit",
        trigger="test",
        target_issue="115000200",
    )

    assert result["status"] == "skipped"
    assert result["skip_reason"] == "target_unconfirmed"


def test_prediction_service_skips_insufficient_recommendations(monkeypatch):
    monkeypatch.setattr(prediction_service, "get_prediction_history_records", lambda limit: [])
    monkeypatch.setattr(prediction_service, "calculate_recommendation", lambda *args, **kwargs: _recommendation([1, 2, 3]))
    monkeypatch.setattr(prediction_service, "save_prediction_history", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not save")))
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: None)

    result = prediction_service.create_for_official_draw("115000100", source="unit", trigger="test")

    assert result["status"] == "skipped"
    assert result["skip_reason"] == "insufficient_recommendations"
    assert result["recommended_count"] == 3


def test_prediction_service_duplicate_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        prediction_service,
        "get_prediction_history_records",
        lambda limit: [{"id": 9, "issue": "115000100", "prediction_issue": "115000101", "recommend_numbers": list(range(1, 21)), "prediction_status": "waiting_draw"}],
    )
    monkeypatch.setattr(prediction_service, "calculate_recommendation", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not calculate")))
    monkeypatch.setattr(prediction_service, "save_prediction_history", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not save")))
    monkeypatch.setattr(prediction_service, "_record_event", lambda **kwargs: None)

    result = prediction_service.create_for_official_draw("115000100", source="unit", trigger="test")

    assert result["status"] == "already_exists"
    assert result["prediction_id"] == 9


def test_writer_guard_rejects_direct_prediction_history_write(monkeypatch):
    events = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_record_prediction_write_rejected", lambda item, reason: events.append((item, reason)))

    result = prediction_history_store.save_prediction_history(
        {
            "issue": "115000100",
            "prediction_issue": "115000101",
            "recommend_numbers": list(range(1, 21)),
            "strategy": "unit-test",
        }
    )

    assert result["status"] == "rejected"
    assert result["skip_reason"] == "unauthorized_writer"
    assert events[0][1] == "unauthorized_writer"


def test_latest_prediction_history_uses_numeric_production_order(monkeypatch):
    captured = {}

    def fake_query(sql, params=(), sqlite_sql=None):
        captured["sql"] = sql
        captured["sqlite_sql"] = sqlite_sql
        return []

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    assert prediction_history_store.get_latest_prediction_history() is None
    assert "join official_draw_history" in captured["sql"]
    assert "length(p.prediction_issue) >=" in captured["sql"]
    assert "p.prediction_issue::bigint desc" in captured["sql"]
    assert "cast(p.prediction_issue as integer) desc" in captured["sqlite_sql"]


def test_latest_prediction_history_filters_test_records_with_sqlite(monkeypatch, tmp_path):
    db_path = tmp_path / "prediction_latest.db"

    def connect():
        return sqlite3.connect(db_path)

    with connect() as conn:
        conn.executescript(
            """
            create table prediction_history (
                id integer primary key,
                issue text,
                prediction_issue text,
                predict_time text,
                strategy text,
                confidence real,
                recommend_numbers text,
                super_number integer,
                three_star text,
                four_star text,
                twins text,
                consecutive text,
                patch_numbers text,
                tails text,
                big_small text,
                odd_even text,
                reasons text,
                winning_numbers text,
                hit_count integer,
                super_hit integer,
                three_star_hit integer,
                four_star_hit integer,
                accuracy real,
                created_at text,
                updated_at text,
                model_scores text,
                winning_model text,
                prediction_status text,
                verified_issue text,
                verified_at text,
                matched_numbers text,
                missed_numbers text,
                prediction_count integer,
                hit_rate real,
                super_number_hit integer,
                verification_version text,
                learning_used integer,
                model_score real
            );
            create table official_draw_history (issue text primary key);
            create table operation_events (
                id integer primary key,
                issue text,
                component text,
                event_type text,
                status text,
                message text,
                duration_ms real,
                created_at text
            );
            """
        )
        numbers = "[" + ",".join(str(n) for n in range(1, 21)) + "]"
        rows = [
            (1, "120", "121", "unit", "2026-07-17T10:00:00"),
            (2, "115039899", "115039900", "V7", "2026-07-17T09:00:00"),
            (3, "215039999", "215040000", "simulation", "2026-07-17T11:00:00"),
        ]
        for row_id, issue, target, strategy, created_at in rows:
            conn.execute(
                """
                insert into prediction_history (
                    id, issue, prediction_issue, predict_time, strategy, confidence,
                    recommend_numbers, super_number, three_star, four_star, twins,
                    consecutive, patch_numbers, tails, big_small, odd_even, reasons,
                    winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
                    accuracy, created_at, updated_at, model_scores, winning_model,
                    prediction_status, verified_issue, verified_at, matched_numbers,
                    missed_numbers, prediction_count, hit_rate, super_number_hit,
                    verification_version, learning_used, model_score
                ) values (
                    ?, ?, ?, ?, ?, 88,
                    ?, 7, '[1,2,3]', '[1,2,3,4]', '[]',
                    '[]', '[]', '[]', 'balanced', 'balanced', '[]',
                    '[]', 0, 0, 0, 0,
                    0, ?, ?, '{}', null,
                    'waiting_draw', null, null, '[]',
                    '[]', 20, 0, 0,
                    null, 0, 0
                )
                """,
                (row_id, issue, target, created_at, strategy, numbers, created_at, created_at),
            )
            conn.execute("insert into official_draw_history (issue) values (?)", (target,))
        conn.execute(
            """
            insert into operation_events (
                id, issue, component, event_type, status, message, duration_ms, created_at
            ) values (
                1, '115039899', 'prediction', 'prediction_created', 'ok',
                '{"event_type":"prediction_created","based_on_issue":"115039899","target_issue":"115039900","source":"official_collector","trigger":"official_draw_saved","recommended_count":20}',
                1, '2026-07-17T09:01:00'
            )
            """
        )

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: False)
    monkeypatch.setattr(prediction_history_store, "_sqlite_connection", connect)

    latest = prediction_history_store.get_latest_prediction_history()
    records = prediction_history_store.get_prediction_history_records(10)

    assert latest["issue"] == "115039899"
    assert latest["prediction_issue"] == "115039900"
    assert latest["source"] == "official_collector"
    assert latest["trigger"] == "official_draw_saved"
    assert latest["read_layer"]["production_filtered"] is True
    assert [record["prediction_issue"] for record in records] == ["115039900"]


def test_is_production_prediction_allows_legacy_source_null_with_official_shape():
    assert prediction_history_store.is_production_prediction({
        "issue": "115039899",
        "prediction_issue": "115039900",
        "recommend_numbers": list(range(1, 21)),
        "source": None,
        "trigger": None,
    })
    assert not prediction_history_store.is_production_prediction({
        "issue": "120",
        "prediction_issue": "121",
        "recommend_numbers": list(range(1, 21)),
    })


def test_recommendation_api_preview_does_not_persist_prediction(monkeypatch):
    monkeypatch.setattr(
        recommendation_api,
        "generate_recommendation_center",
        lambda **kwargs: {"status": "ok", "recommendation": {"issue": "115000100"}, "persisted": kwargs.get("persist", True)},
    )

    result = recommendation_api.api_recommendation_center_generate()

    assert result["status"] == "ok"
    assert result["persisted"] is False


def test_prediction_refresh_routes_through_prediction_service(monkeypatch):
    calls = []
    events = []
    monkeypatch.setattr(prediction_refresh, "_existing_prediction", lambda source_issue, target_issue: None)
    monkeypatch.setattr(prediction_refresh, "_record_refresh_event", lambda payload, start: None)
    monkeypatch.setattr(prediction_refresh, "_record_trigger_event", lambda event_type, **kwargs: events.append((event_type, kwargs)))

    def fake_create(based_on_issue, **kwargs):
        calls.append((based_on_issue, kwargs))
        return {
            "status": "created",
            "prediction_id": 77,
            "recommended_count": 20,
            "target_issue": kwargs.get("target_issue"),
        }

    import services.prediction_service as prediction_service_module

    monkeypatch.setattr(prediction_service_module, "create_for_official_draw", fake_create)

    result = prediction_refresh.refresh_next_prediction_for_draw(
        {"issue": "115000100", "numbers": list(range(1, 21))}
    )

    assert result["status"] == "created"
    assert result["refresh_status"] == "ready"
    assert calls[0][0] == "115000100"
    assert calls[0][1]["target_issue"] == "115000101"
    assert calls[0][1]["source"] == "official_collector"
    assert calls[0][1]["trigger"] == "official_draw_saved"
    assert "prediction_service_called" in [event[0] for event in events]
