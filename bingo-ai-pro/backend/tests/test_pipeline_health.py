from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api import pipeline as pipeline_api
from services import pipeline_health


def test_prediction_coverage_calculates_missing_between_observed(monkeypatch):
    monkeypatch.setattr(pipeline_health, "_today_taipei", lambda: "2026-07-16")
    monkeypatch.setattr(
        pipeline_health,
        "_query",
        lambda *_args, **_kwargs: [("115000001",), ("115000003",), ("115000004",)],
    )

    result = pipeline_health.prediction_coverage()

    assert result["prediction_expected_today"] == 204
    assert result["prediction_created_today"] == 3
    assert result["missing_prediction_count"] == 201
    assert result["missing_target_issues"] == ["115000002"]


def test_pipeline_alerts_marks_recovery_pending_as_critical():
    alerts = pipeline_health.pipeline_alerts(
        {"prediction_coverage": 98.5},
        {"verification_pending": 0, "learning_pending": 0},
        {"target_unconfirmed_today": 0, "null_target_today": 0},
        {"verification": {"would_verify": 1}, "learning_sync": {"would_sync": 0}},
    )

    assert {"type": "recovery_pending", "severity": "critical", "message": "recovery dry-run is not clean"} in alerts
    assert pipeline_health._status_from_alerts(alerts) == "critical"


def test_build_pipeline_health_is_json_safe(monkeypatch):
    monkeypatch.setenv("PIPELINE_HEALTH_FULL_MODE", "1")
    monkeypatch.setattr(pipeline_health, "prediction_coverage", lambda: {
        "date": "2026-07-16",
        "prediction_expected_today": 204,
        "prediction_created_today": 204,
        "prediction_coverage": 100.0,
        "missing_prediction_count": 0,
        "missing_target_issues": [],
    })
    monkeypatch.setattr(pipeline_health, "lifecycle_pending_counts", lambda: {
        "verification_pending": 0,
        "learning_pending": 0,
    })
    monkeypatch.setattr(pipeline_health, "target_unconfirmed_counts", lambda: {
        "target_unconfirmed_today": 0,
        "null_target_today": 0,
    })
    monkeypatch.setattr(pipeline_health, "recovery_dry_run_health", lambda: {
        "verification": {"would_verify": 0},
        "learning_sync": {"would_sync": 0},
    })
    monkeypatch.setattr(pipeline_health, "get_prediction_lifecycle_aggregates", lambda: {
        "total_prediction_count": 154,
        "valid_prediction_count": 127,
        "completed_verified_count": 112,
        "null_target_count": 27,
        "has_official_result_count": 112,
    })
    monkeypatch.setattr(pipeline_health, "get_learned_live_target_count", lambda: 63)
    monkeypatch.setattr(pipeline_health, "latest_pipeline_times", lambda: {
        "prediction_last_created": "2026-07-16T00:00:00",
        "prediction_last_verified": "2026-07-16T00:05:00",
        "prediction_last_learning": "2026-07-16T00:06:00",
    })
    monkeypatch.setattr(pipeline_health, "scheduler_status", lambda scheduler=None: {
        "status": "running",
        "jobs": [],
        "runtime": {},
    })
    monkeypatch.setattr(pipeline_health, "prediction_pipeline_validation", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "operation_event_health", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "prediction_trigger_event_counts", lambda: {
        "prediction_trigger_count_today": 1,
        "prediction_service_call_count_today": 1,
        "prediction_create_started_count_today": 1,
        "prediction_created_count_today": 1,
        "prediction_skipped_count_today": 0,
    })
    monkeypatch.setattr(pipeline_health, "official_draw_time_health", lambda: {"missing_draw_time_count": 0})
    monkeypatch.setattr(pipeline_health, "verification_delay", lambda: {"sample_size": 1, "average_delay_minutes": 1, "p95_delay_minutes": 1, "status": "ok"})
    monkeypatch.setattr(pipeline_health, "learning_delay", lambda: {"sample_size": 1, "average_delay_minutes": 1, "p95_delay_minutes": 1, "status": "ok"})

    payload = pipeline_health.build_pipeline_health()

    assert payload["pipeline_status"] == "healthy"
    assert payload["dashboard_statistics"]["learning_sample_count"] == 63
    assert payload["prediction_service_call_count_today"] == 1
    json.dumps(payload, allow_nan=False, default=str)


def test_build_pipeline_health_returns_partial_when_component_fails(monkeypatch):
    monkeypatch.setenv("PIPELINE_HEALTH_FULL_MODE", "1")
    monkeypatch.setattr(pipeline_health, "prediction_coverage", lambda: {
        "date": "2026-07-16",
        "schedule_expected_count": 1,
        "schedule_coverage": 100.0,
        "prediction_expected_today": 204,
        "prediction_created_today": 1,
        "prediction_coverage": 1.0,
        "missing_prediction_count": 0,
        "missing_target_issues": [],
    })
    monkeypatch.setattr(pipeline_health, "lifecycle_pending_counts", lambda: {
        "verification_pending": 0,
        "learning_pending": 0,
        "live_verification_pending": 0,
        "live_learning_pending": 0,
    })
    monkeypatch.setattr(pipeline_health, "target_unconfirmed_counts", lambda: {
        "target_unconfirmed_today": 0,
        "null_target_today": 0,
    })
    monkeypatch.setattr(pipeline_health, "recovery_dry_run_health", lambda: (_ for _ in ()).throw(RuntimeError("database password leaked in traceback")))
    monkeypatch.setattr(pipeline_health, "get_prediction_lifecycle_aggregates", lambda: {})
    monkeypatch.setattr(pipeline_health, "get_learned_live_target_count", lambda: 63)
    monkeypatch.setattr(pipeline_health, "latest_pipeline_times", lambda: {})
    monkeypatch.setattr(pipeline_health, "scheduler_status", lambda scheduler=None: (_ for _ in ()).throw(RuntimeError("scheduler boom")))
    monkeypatch.setattr(pipeline_health, "prediction_pipeline_validation", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "operation_event_health", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "prediction_trigger_event_counts", lambda: {})
    monkeypatch.setattr(pipeline_health, "official_draw_time_health", lambda: {})
    monkeypatch.setattr(pipeline_health, "verification_delay", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "learning_delay", lambda: {"status": "ok"})

    payload = pipeline_health.build_pipeline_health()
    serialized = json.dumps(payload, allow_nan=False, default=str)

    assert payload["status"] == "partial"
    assert payload["pipeline_status"] == "warning"
    assert payload["components"]["recovery_dry_run"]["status"] == "unavailable"
    assert payload["components"]["recovery_dry_run"]["error_code"] == "recovery_dry_run_unavailable"
    assert payload["components"]["scheduler"]["status"] == "unavailable"
    assert "password leaked" not in serialized
    assert "Traceback" not in serialized


def test_build_pipeline_health_fast_mode_skips_heavy_components(monkeypatch):
    monkeypatch.delenv("PIPELINE_HEALTH_FULL_MODE", raising=False)
    monkeypatch.setattr(pipeline_health, "scheduler_status", lambda scheduler=None: {
        "status": "running",
        "jobs": [],
        "prediction_job_registered": False,
        "runtime": {},
    })
    monkeypatch.setattr(pipeline_health, "prediction_pipeline_validation", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline_health, "prediction_trigger_event_counts", lambda: {
        "prediction_trigger_count_today": 1,
        "prediction_service_call_count_today": 1,
        "prediction_create_started_count_today": 1,
        "prediction_created_count_today": 1,
        "prediction_skipped_count_today": 0,
    })
    monkeypatch.setattr(pipeline_health, "recovery_dry_run_health", lambda: {
        "verification": {"would_verify": None},
        "learning_sync": {"would_sync": None},
        "status": "skipped",
    })
    monkeypatch.setattr(pipeline_health, "official_draw_time_health", lambda: {"status": "skipped"})

    payload = pipeline_health.build_pipeline_health()

    assert payload["status"] == "partial"
    assert payload["pipeline_status"] == "warning"
    assert payload["health_mode"] == "fast"
    assert payload["components"]["coverage"]["status"] == "skipped"
    assert payload["prediction_service_call_count_today"] == 1
    json.dumps(payload, allow_nan=False, default=str)


def test_pipeline_health_api_uses_app_scheduler(monkeypatch):
    captured = {}

    def fake_build(scheduler=None):
        captured["scheduler"] = scheduler
        return {"status": "ok", "pipeline_status": "healthy"}

    class AppState:
        scheduler = object()

    class App:
        state = AppState()

    class Request:
        app = App()

    monkeypatch.setattr(pipeline_api, "build_pipeline_health", fake_build)

    result = pipeline_api.api_pipeline_health(Request())

    assert result["pipeline_status"] == "healthy"
    assert captured["scheduler"] is AppState.scheduler
