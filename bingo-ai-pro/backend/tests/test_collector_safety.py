from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import official_draw_store
from database.official_draw_store import _valid_draw
from services import catch_up_service
from services import official_verification


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


def test_latest_official_draw_uses_numeric_production_order(monkeypatch):
    captured = {}

    def fake_query(sql, params=(), sqlite_sql=None):
        captured["sql"] = sql
        captured["sqlite_sql"] = sqlite_sql
        return [
            (
                1,
                "115039895",
                "2026-07-16",
                None,
                list(range(1, 21)),
                list(range(1, 21)),
                1,
                True,
                "taiwan_lottery",
                False,
                {},
                "2026-07-16T00:00:00",
                "2026-07-16T00:00:00",
            )
        ]

    monkeypatch.setattr(official_draw_store, "_query_with_fallback", fake_query)

    result = official_draw_store.get_latest_official_draw()

    assert result["issue"] == "115039895"
    assert "length(issue) >= 6" in captured["sql"]
    assert "issue::bigint desc" in captured["sql"]
    assert "cast(issue as integer) desc" in captured["sqlite_sql"]


def test_catch_up_limits_batch(monkeypatch):
    source_draws = [_draw(issue) for issue in range(101, 125)]
    saved_batches = []
    prediction_calls = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "100")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda page_size=20: source_draws)
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "save_draw_verification", lambda item: {"status": "ok"})

    def fake_save(draws):
        saved_batches.append(draws)
        return {"status": "ok", "saved": len(draws), "storage": "test"}

    monkeypatch.setattr(catch_up_service, "save_official_draws", fake_save)
    monkeypatch.setattr(catch_up_service, "_record_structured_event", lambda *args, **kwargs: None)

    import services.prediction_refresh as prediction_refresh

    monkeypatch.setattr(
        prediction_refresh,
        "ensure_next_prediction",
        lambda draw: prediction_calls.append(draw) or {"status": "created", "target_issue": str(int(draw["issue"]) + 1)},
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["status"] == "ok"
    assert result["max_batch_size"] == 20
    assert result["catch_count"] == 20
    assert result["success_count"] == 20
    assert len(saved_batches[0]) == 20
    assert prediction_calls[0]["issue"] == "120"
    assert result["prediction"]["status"] == "created"


def test_catch_up_already_synced_triggers_next_prediction(monkeypatch):
    prediction_calls = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "115039887")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda page_size=20: [_draw(115039887)])
    monkeypatch.setattr(catch_up_service, "get_latest_official_draw", lambda: _draw(115039887))
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "_record_structured_event", lambda *args, **kwargs: None)

    import services.prediction_refresh as prediction_refresh

    monkeypatch.setattr(
        prediction_refresh,
        "ensure_next_prediction",
        lambda draw: prediction_calls.append(draw) or {"status": "created", "target_issue": "115039888"},
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["status"] == "ok"
    assert result["catch_count"] == 0
    assert prediction_calls[0]["issue"] == "115039887"
    assert result["prediction"]["target_issue"] == "115039888"


def test_catch_up_deadline_skips_downstream(monkeypatch):
    source_draws = [_draw(issue) for issue in range(101, 104)]

    monkeypatch.setattr(catch_up_service, "JOB_TIME_BUDGET_SECONDS", 0)
    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "100")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda page_size=20: source_draws)
    monkeypatch.setattr(catch_up_service, "save_official_draws", lambda draws: {"status": "ok", "saved": len(draws)})
    monkeypatch.setattr(
        catch_up_service,
        "run_official_verification",
        lambda limit=10: (_ for _ in ()).throw(AssertionError("verification should be skipped")),
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["exit_reason"] == "deadline_exceeded"
    assert result["deadline_exceeded"] is True
    assert result["verification"]["status"] == "skipped"
    assert result["prediction"]["status"] == "skipped"


def test_official_collector_deadline_skips_downstream(monkeypatch):
    monkeypatch.setattr(official_verification, "COLLECTOR_JOB_TIME_BUDGET_SECONDS", 0)
    monkeypatch.setattr(official_verification, "fetch_official_bingo_results", lambda *args, **kwargs: [_draw(101)])
    monkeypatch.setattr(official_verification, "save_official_draws", lambda draws: {"status": "ok", "saved": len(draws)})
    monkeypatch.setattr(official_verification, "get_latest_official_draw", lambda: _draw(101))
    monkeypatch.setattr(
        official_verification,
        "run_official_verification",
        lambda limit=10: (_ for _ in ()).throw(AssertionError("verification should be skipped")),
    )

    result = official_verification.collect_official_today()

    assert result["exit_reason"] == "deadline_exceeded"
    assert result["deadline_exceeded"] is True
    assert result["verification"]["status"] == "skipped"
