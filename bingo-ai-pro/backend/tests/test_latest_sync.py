from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import latest_sync


def setup_function():
    latest_sync._LATEST_SYNC_CACHE["snapshot"] = None
    latest_sync._LATEST_SYNC_CACHE["expires_at"] = 0.0
    latest_sync._RECONCILE_IN_FLIGHT.clear()
    latest_sync._LATEST_SYNC_STATE.update(
        {
            "source_issue": None,
            "analysis_created": False,
            "prediction_created": False,
            "analysis_reconcile": None,
            "prediction_reconcile": None,
            "last_attempt_at": None,
            "attempt_count": 0,
        }
    )


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


def test_latest_sync_treats_stale_fast_path_prediction_as_missing(monkeypatch):
    monkeypatch.setattr(
        latest_sync,
        "get_prediction_for_source_target",
        lambda source_issue, target_issue: {
            "issue": source_issue,
            "prediction_issue": target_issue,
            "recommend_numbers": list(range(1, 21)),
            "model_scores": {"production_fast_path": {"fast_path_strategy_version": "28.0-old"}},
        },
    )

    assert latest_sync._prediction_exists_for_latest("115040927") is False


def test_latest_sync_existing_draw_does_not_save_duplicate(monkeypatch):
    saved_calls = []
    draw = _draw()
    monkeypatch.setattr(latest_sync, "_latest_draw_from_source", lambda: draw)
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
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
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(latest_sync, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(latest_sync, "save_official_draws", lambda draws: saved_calls.append(draws) or {"status": "ok", "saved": 1})

    result = latest_sync.process_latest_official_draw()

    assert result["database_saved"] is False
    assert result["failure_stage"] == "validated"
    assert saved_calls == []


def test_latest_sync_database_failure_does_not_mark_saved(monkeypatch):
    draw = _draw()
    monkeypatch.setattr(latest_sync, "_latest_draw_from_source", lambda: draw)
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(latest_sync, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(latest_sync, "save_official_draws", lambda draws: {"status": "error", "saved": 0, "error": "boom"})

    result = latest_sync.process_latest_official_draw()

    assert result["database_saved"] is False
    assert result["sync_status"] == "error"
    assert result["failure_stage"] == "database_saved"


def test_latest_sync_snapshot_queues_missing_prediction_without_blocking(monkeypatch):
    draw = _draw("115040625")
    submitted = []

    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: {
            "draw": draw,
            "analysis_exists": True,
            "prediction_exists": False,
            "target_issue": "115040626",
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
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


def test_latest_sync_snapshot_queues_missing_analysis_and_prediction(monkeypatch):
    draw = _draw("115040899")
    submitted = []

    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: {
            "draw": draw,
            "analysis_exists": False,
            "prediction_exists": False,
            "target_issue": "115040900",
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
    latest_sync._RECONCILE_IN_FLIGHT.clear()

    class Executor:
        def submit(self, fn, *args):
            submitted.append((fn, args))

    monkeypatch.setattr(latest_sync, "_RECONCILE_EXECUTOR", Executor())
    result = latest_sync.get_latest_sync_snapshot()

    assert len(submitted) == 1
    assert result["source_issue"] == "115040899"
    assert result["target_issue"] == "115040900"
    assert result["database_saved"] is True
    assert result["analysis_created"] is False
    assert result["prediction_created"] is False
    assert result["sync_status"] == "analysis_pending"
    assert result["prediction_reconcile"]["status"] == "queued"
    assert result["prediction_reconcile"]["analysis_created"] is False
    assert result["stages"]["analysis"]["status"] == "pending"


def test_latest_sync_downstream_reconcile_creates_analysis_before_prediction(monkeypatch):
    draw = _draw("115040899")
    calls = []

    monkeypatch.setattr(
        latest_sync,
        "save_analysis_history",
        lambda latest: calls.append(("analysis", latest["issue"])) or {"status": "ok", "issue": latest["issue"]},
    )
    monkeypatch.setattr(latest_sync, "_analysis_exists", lambda issue: True)
    monkeypatch.setattr(latest_sync, "_prediction_exists_for_latest", lambda issue: False)

    import services.prediction_refresh as prediction_refresh

    monkeypatch.setattr(
        prediction_refresh,
        "ensure_next_prediction",
        lambda latest: calls.append(("prediction", latest["issue"])) or {
            "status": "created",
            "refresh_status": "ready",
            "based_on_issue": latest["issue"],
            "target_issue": "115040900",
        },
    )

    result = latest_sync._reconcile_latest_downstream(
        draw,
        "115040899",
        "115040900",
        analysis_created=False,
    )

    assert calls == [("analysis", "115040899"), ("prediction", "115040899")]
    assert result["analysis_created"] is True
    assert result["prediction_created"] is True
    assert result["failure_stage"] is None


def test_latest_sync_snapshot_preserves_fast_reconcile_completion(monkeypatch):
    draw = _draw("115040899")

    latest_sync._LATEST_SYNC_STATE.update(
        {
            "source_issue": None,
            "analysis_created": False,
            "prediction_created": False,
            "prediction_reconcile": None,
            "analysis_reconcile": None,
        }
    )
    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: {
            "draw": draw,
            "analysis_exists": True,
            "prediction_exists": False,
            "target_issue": "115040900",
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
    monkeypatch.setattr(latest_sync, "_prediction_exists_for_latest", lambda issue: False)

    import services.prediction_refresh as prediction_refresh

    monkeypatch.setattr(
        prediction_refresh,
        "ensure_next_prediction",
        lambda latest: {
            "status": "created",
            "refresh_status": "ready",
            "based_on_issue": latest["issue"],
            "target_issue": "115040900",
        },
    )

    class Future:
        def result(self):
            return None

        def add_done_callback(self, callback):
            callback(self)

    class ImmediateExecutor:
        def submit(self, fn, *args):
            fn(*args)
            return Future()

    monkeypatch.setattr(latest_sync, "_RECONCILE_EXECUTOR", ImmediateExecutor())

    result = latest_sync.get_latest_sync_snapshot()

    assert result["analysis_created"] is True
    assert result["prediction_created"] is True
    assert result["dashboard_ready"] is True
    assert result["sync_status"] == "synced"
    assert result["prediction_reconcile"]["refresh_status"] == "ready"


def test_latest_sync_stale_already_running_reconcile_runs_inline(monkeypatch):
    draw = _draw("115040915")
    calls = []
    latest_sync._RECONCILE_IN_FLIGHT.add("115040915")
    latest_sync._LATEST_SYNC_STATE.update(
        {
            "source_issue": "115040915",
            "last_attempt_at": "2026-07-21T00:00:00+00:00",
            "attempt_count": latest_sync._RECONCILE_MAX_QUEUED_ATTEMPTS,
            "prediction_reconcile": {"reason": "reconcile_already_running"},
        }
    )

    monkeypatch.setattr(
        latest_sync,
        "_reconcile_latest_downstream",
        lambda latest, source_issue, target_issue, analysis_created=True: calls.append((source_issue, target_issue)) or {
            "attempt_count": 17,
            "last_attempt_at": "2026-07-21T00:01:00+00:00",
            "analysis_reconcile": {"status": "existing"},
            "prediction_reconcile": {"status": "created", "refresh_status": "ready"},
            "analysis_created": True,
            "prediction_created": True,
            "failure_stage": None,
            "failure_reason": None,
            "next_retry_expected_at": None,
        },
    )

    result = latest_sync._queue_latest_downstream_reconcile(
        draw,
        "115040915",
        "115040916",
        analysis_created=True,
    )

    assert calls == [("115040915", "115040916")]
    assert result["refresh_status"] == "ready"
    assert "115040915" not in latest_sync._RECONCILE_IN_FLIGHT


def test_latest_sync_snapshot_rebuilds_from_database_after_memory_reset(monkeypatch):
    draw = _draw("115040625")
    prediction = {
        "id": 7,
        "issue": "115040625",
        "prediction_issue": "115040626",
        "recommend_numbers": list(range(1, 21)),
        "model_scores": {
            "production_fast_path": {
                "fast_path_strategy_version": "28.0-diversity-v1",
            }
        },
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

    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: {
            "draw": draw,
            "analysis_exists": True,
            "prediction_exists": True,
            "target_issue": "115040626",
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: None)
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


def test_latest_sync_snapshot_marks_database_behind_kuaishou(monkeypatch):
    draw = _draw("115040850")
    monkeypatch.setattr(
        latest_sync,
        "get_latest_official_draw_sync_status",
        lambda: {
            "draw": draw,
            "analysis_exists": True,
            "prediction_exists": True,
            "target_issue": "115040851",
        },
    )
    monkeypatch.setattr(latest_sync, "get_latest_kuaishou_snapshot", lambda: {"issue": "115040888"})

    result = latest_sync.get_latest_sync_snapshot()

    assert result["official_detected_issue"] == "115040888"
    assert result["external_detected_issue"] == "115040888"
    assert result["database_latest_issue"] == "115040850"
    assert result["issues_behind"] == 38
    assert result["sync_status"] == "database_behind"
    assert result["failure_stage"] == "database"
