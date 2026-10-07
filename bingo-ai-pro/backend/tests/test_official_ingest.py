from __future__ import annotations

import builtins
import pathlib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import official_ingest
from services.collector_runtime import official_collection_lock


def _draw(issue: str = "115040550", numbers=None, super_number=8):
    values = numbers or list(range(1, 21))
    return {
        "issue": issue,
        "draw_date": "2026-07-20",
        "draw_time": "2026-07-20T01:00:00+00:00",
        "numbers": values,
        "open_order_numbers": values,
        "super_number": super_number,
        "source": "taiwan_lottery",
    }


def setup_function():
    with official_ingest._POLL_STATE_LOCK:
        official_ingest._POLL_COMPLETED_ISSUES.clear()


def test_lightweight_ingest_no_new_issue_noop(monkeypatch):
    monkeypatch.setattr(official_ingest, "_latest_source_draw", lambda page_size=10: (_draw("115040550"), [_draw("115040550")]))
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: _draw("115040550"))
    monkeypatch.setattr(
        official_ingest,
        "save_official_draws",
        lambda draws: (_ for _ in ()).throw(AssertionError("save should not be called")),
    )

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "noop"
    assert result["exit_reason"] == "database_same_or_newer"
    assert result["save_ms"] == 0.0


def test_lightweight_ingest_new_valid_issue_saved_once(monkeypatch):
    saved = []
    draw = _draw("115040551")
    monkeypatch.setattr(official_ingest, "_latest_source_draw", lambda page_size=10: (draw, [draw]))
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: _draw("115040550"))
    monkeypatch.setattr(official_ingest, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(
        official_ingest,
        "save_official_draws",
        lambda draws: saved.append(draws) or {"status": "ok", "saved": len(draws), "storage": "cloud"},
    )

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "ok"
    assert result["source_issue"] == "115040551"
    assert saved == [[draw]]
    assert result["downstream"]["status"] == "deferred"


def test_lightweight_ingest_duplicate_issue_does_not_duplicate(monkeypatch):
    draw = _draw("115040551")
    monkeypatch.setattr(official_ingest, "_latest_source_draw", lambda page_size=10: (draw, [draw]))
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: _draw("115040550"))
    monkeypatch.setattr(official_ingest, "get_official_draw_by_issue", lambda issue: draw)
    monkeypatch.setattr(
        official_ingest,
        "save_official_draws",
        lambda draws: (_ for _ in ()).throw(AssertionError("duplicate should not save")),
    )

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "noop"
    assert result["exit_reason"] == "issue_already_exists"


def test_lightweight_ingest_invalid_20_numbers_rejected(monkeypatch):
    invalid = _draw(numbers=list(range(0, 20)), super_number=8)
    monkeypatch.setattr(official_ingest, "fetch_official_bingo_results", lambda *args, **kwargs: [invalid])
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: None)
    monkeypatch.setattr(
        official_ingest,
        "save_official_draws",
        lambda draws: (_ for _ in ()).throw(AssertionError("invalid draw should not save")),
    )

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "error"
    assert result["stage"] == "source_fetch"
    assert result["reason"] == "no_valid_complete_official_draw"


def test_lightweight_ingest_invalid_super_number_rejected(monkeypatch):
    invalid = _draw(super_number=80)
    monkeypatch.setattr(official_ingest, "fetch_official_bingo_results", lambda *args, **kwargs: [invalid])
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: None)

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "error"
    assert result["reason"] == "no_valid_complete_official_draw"


def test_lightweight_ingest_db_failure_is_controlled_error(monkeypatch):
    draw = _draw("115040551")
    monkeypatch.setattr(official_ingest, "_latest_source_draw", lambda page_size=10: (draw, [draw]))
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: _draw("115040550"))
    monkeypatch.setattr(official_ingest, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(
        official_ingest,
        "save_official_draws",
        lambda draws: {"status": "error", "saved": 0, "error": "db down"},
    )

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "error"
    assert result["stage"] == "database_save"
    assert "db down" in result["reason"]


def test_lightweight_ingest_source_timeout_is_controlled_error(monkeypatch):
    monkeypatch.setattr(official_ingest, "fetch_official_bingo_results", lambda *args, **kwargs: [])
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: None)

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "error"
    assert result["stage"] == "source_fetch"


def test_lightweight_ingest_does_not_import_ai_downstream(monkeypatch):
    real_import = builtins.__import__
    blocked = (
        "database.analysis_store",
        "services.prediction_refresh",
        "services.prediction_lifecycle_orchestrator",
        "services.learning_engine",
    )

    def guarded_import(name, *args, **kwargs):
        if name in blocked:
            raise AssertionError(f"blocked downstream import {name}")
        return real_import(name, *args, **kwargs)

    draw = _draw("115040551")
    monkeypatch.setattr(official_ingest, "_latest_source_draw", lambda page_size=10: (draw, [draw]))
    monkeypatch.setattr(official_ingest, "get_latest_official_draw_summary", lambda: _draw("115040550"))
    monkeypatch.setattr(official_ingest, "get_official_draw_by_issue", lambda issue: None)
    monkeypatch.setattr(official_ingest, "save_official_draws", lambda draws: {"status": "ok", "saved": 1})
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "ok"


def test_repeated_polling_does_not_overlap():
    with official_collection_lock("test_holder"):
        result = official_ingest.collect_latest_official_lightweight()

    assert result["status"] == "skipped_due_to_lock"


def test_polling_window_skips_non_draw_times():
    now = datetime(2026, 10, 5, 6, 59, tzinfo=ZoneInfo("Asia/Taipei"))

    result = official_ingest.run_lightweight_official_polling_tick(now)

    assert result == {"status": "skipped", "reason": "outside_draw_hours"}


def test_polling_window_stops_after_success(monkeypatch):
    now = datetime(2026, 10, 5, 7, 6, 1, tzinfo=ZoneInfo("Asia/Taipei"))
    calls = []
    monkeypatch.setattr(
        official_ingest,
        "collect_latest_official_lightweight",
        lambda: calls.append(1) or {"status": "ok", "source_issue": "115040001"},
    )

    first = official_ingest.run_lightweight_official_polling_tick(now)
    second = official_ingest.run_lightweight_official_polling_tick(now)

    assert first["status"] == "ok"
    assert second == {"status": "skipped", "reason": "issue_window_already_completed"}
    assert calls == [1]


def test_ingest_failure_does_not_clear_last_good_ai(monkeypatch):
    last_good = {"recommend_numbers": list(range(1, 21))}
    monkeypatch.setattr(official_ingest, "fetch_official_bingo_results", lambda *args, **kwargs: [])

    result = official_ingest.ingest_latest_official_once()

    assert result["status"] == "error"
    assert last_good == {"recommend_numbers": list(range(1, 21))}
