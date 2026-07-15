from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database.official_draw_store import _valid_draw
from services import catch_up_service


def _draw(issue: int, numbers=None):
    return {
        "issue": str(issue),
        "numbers": numbers or list(range(1, 21)),
        "super_number": 1,
        "source": "taiwan_lottery",
    }


def test_official_draw_validation_rejects_invalid_numbers():
    assert _valid_draw(_draw(101)) is True
    assert _valid_draw(_draw(101, list(range(1, 20)))) is False
    assert _valid_draw(_draw(101, list(range(1, 20)) + [81])) is False
    assert _valid_draw(_draw(101, list(range(1, 20)) + [0])) is False
    assert _valid_draw(_draw(101, list(range(1, 20)) + [19])) is False


def test_catch_up_limits_batch(monkeypatch):
    source_draws = [_draw(issue) for issue in range(101, 125)]
    saved_batches = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "100")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda page_size=20: source_draws)
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "save_draw_verification", lambda item: {"status": "ok"})

    def fake_save(draws):
        saved_batches.append(draws)
        return {"status": "ok", "saved": len(draws), "storage": "test"}

    monkeypatch.setattr(catch_up_service, "save_official_draws", fake_save)

    result = catch_up_service.catch_up_missing_issues()

    assert result["status"] == "ok"
    assert result["max_batch_size"] == 20
    assert result["catch_count"] == 20
    assert result["success_count"] == 20
    assert len(saved_batches[0]) == 20
