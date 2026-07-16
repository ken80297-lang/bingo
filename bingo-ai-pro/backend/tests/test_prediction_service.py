from __future__ import annotations

import pathlib
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
    monkeypatch.setattr(prediction_refresh, "_existing_prediction", lambda source_issue, target_issue: None)
    monkeypatch.setattr(prediction_refresh, "_record_refresh_event", lambda payload, start: None)

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
