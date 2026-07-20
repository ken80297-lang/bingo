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
