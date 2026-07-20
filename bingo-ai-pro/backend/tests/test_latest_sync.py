from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import latest_sync


def _draw(issue: str = "115040550", numbers=None):
    return {
        "issue": issue,
        "draw_date": "2026-07-20",
        "draw_time": "2026-07-20T01:00:00+00:00",
        "numbers": numbers or list(range(1, 21)),
        "open_order_numbers": numbers or list(range(1, 21)),
        "super_number": 8,
        "source": "taiwan_lottery",
    }


def _patch_downstream(monkeypatch):
    monkeypatch.setattr(latest_sync, "_analysis_exists", lambda issue: True)
    monkeypatch.setattr(latest_sync, "_prediction_exists_for_latest", lambda issue: True)
    monkeypatch.setattr(latest_sync, "save_analysis_history", lambda draw: {"status": "ok", "issue": draw["issue"]})

    def lifecycle(draw, **kwargs):
        return {
            "status": "ok",
            "verification": {"status": "ok"},
            "learning": {"status": "ok"},
            "prediction": {"status": "already_exists"},
        }

    import services.prediction_lifecycle_orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "process_official_draw_lifecycle", lifecycle)


def test_latest_sync_existing_draw_does_not_save_duplicate(monkeypatch):
    saved_calls = []
    draw = _draw()
    monkeypatch.setattr(latest_sync, "_latest_draw_from_source", lambda: draw)
    monkeypatch.setattr(latest_sync, "get_official_draw_by_issue", lambda issue: draw)
    monkeypatch.setattr(latest_sync, "save_official_draws", lambda draws: saved_calls.append(draws) or {"status": "ok", "saved": 1})
    _patch_downstream(monkeypatch)

    result = latest_sync.process_latest_official_draw()

    assert result["database_saved"] is True
    assert result["saved"]["storage"] == "existing"
    assert saved_calls == []


def test_latest_sync_rejects_invalid_numbers_before_save(monkeypatch):
    saved_calls = []
    monkeypatch.setattr(latest_sync, "_latest_draw_from_source", lambda: _draw(numbers=list(range(0, 20))))
    monkeypatch.setattr(latest_sync, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(latest_sync, "save_official_draws", lambda draws: saved_calls.append(draws) or {"status": "ok", "saved": 1})

    result = latest_sync.process_latest_official_draw()

    assert result["database_saved"] is False
    assert result["failure_stage"] == "validated"
    assert saved_calls == []


def test_latest_sync_database_failure_does_not_mark_saved(monkeypatch):
    draw = _draw()
    monkeypatch.setattr(latest_sync, "_latest_draw_from_source", lambda: draw)
    monkeypatch.setattr(latest_sync, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(latest_sync, "save_official_draws", lambda draws: {"status": "error", "saved": 0, "error": "boom"})

    result = latest_sync.process_latest_official_draw()

    assert result["database_saved"] is False
    assert result["sync_status"] == "error"
    assert result["failure_stage"] == "database_saved"


def test_latest_sync_snapshot_queues_missing_prediction_without_blocking(monkeypatch):
    draw = _draw("115040625")
    submitted = []

    monkeypatch.setattr(latest_sync, "get_latest_official_draw", lambda: draw)
    monkeypatch.setattr(latest_sync, "_analysis_exists", lambda issue: True)
    monkeypatch.setattr(latest_sync, "_prediction_exists_for_latest", lambda issue: False)
    latest_sync._RECONCILE_IN_FLIGHT.clear()

    class Executor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(latest_sync, "_RECONCILE_EXECUTOR", Executor())
    result = latest_sync.get_latest_sync_snapshot()

    assert len(submitted) == 1
    assert result["source_issue"] == "115040625"
    assert result["target_issue"] == "115040626"
    assert result["database_saved"] is True
    assert result["analysis_created"] is True
    assert result["prediction_created"] is False
    assert result["dashboard_ready"] is False
    assert result["sync_status"] == "prediction_pending"
    assert result["prediction_reconcile"]["refresh_status"] == "queued"
    assert result["stages"]["prediction"]["status"] == "pending"
    assert result["timings_ms"]["total_ms"] >= 0


def test_latest_sync_snapshot_rebuilds_from_database_after_memory_reset(monkeypatch):
    draw = _draw("115040625")
    prediction = {
        "id": 7,
        "issue": "115040625",
        "prediction_issue": "115040626",
        "recommend_numbers": list(range(1, 21)),
    }
    latest_sync._LATEST_SYNC_STATE.update(
        {
            "official_detected_issue": None,
            "source_issue": None,
            "database_latest_issue": None,
            "dashboard_latest_issue": None,
            "database_saved": False,
            "analysis_created": False,
            "prediction_created": False,
            "dashboard_ready": False,
            "target_issue": None,
            "detected_at": None,
            "last_attempt_at": None,
            "attempt_count": 0,
            "failure_stage": None,
            "failure_reason": None,
            "next_retry_expected_at": None,
            "stages": {},
        }
    )

    monkeypatch.setattr(latest_sync, "get_latest_official_draw", lambda: draw)
    monkeypatch.setattr(latest_sync, "_analysis_exists", lambda issue: True)
    monkeypatch.setattr(latest_sync, "get_prediction_for_source_target", lambda source_issue, target_issue: prediction)

    import services.prediction_refresh as prediction_refresh

    monkeypatch.setattr(
        prediction_refresh,
        "ensure_next_prediction",
        lambda latest_draw: (_ for _ in ()).throw(AssertionError("should not reconcile existing prediction")),
    )

    result = latest_sync.get_latest_sync_snapshot()

    assert result["official_detected_issue"] == "115040625"
    assert result["source_issue"] == "115040625"
    assert result["database_latest_issue"] == "115040625"
    assert result["target_issue"] == "115040626"
    assert result["database_saved"] is True
    assert result["analysis_created"] is True
    assert result["prediction_created"] is True
    assert result["dashboard_ready"] is True
    assert result["sync_status"] == "synced"
    assert result["attempt_count"] == 0
    assert result["stages"]["database"]["status"] == "completed"
    assert result["stages"]["prediction"]["status"] == "completed"
