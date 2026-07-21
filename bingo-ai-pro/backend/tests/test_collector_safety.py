from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import official_draw_store
from database.official_draw_store import _valid_draw
from collectors import taiwan_lottery_collector
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


def test_taiwan_lottery_draw_time_parser_normalizes_to_utc():
    parsed, reason = taiwan_lottery_collector._parse_draw_time("2026-07-17T15:35:08")
    assert parsed == "2026-07-17T07:35:08+00:00"
    assert reason is None

    parsed, reason = taiwan_lottery_collector._parse_draw_time("2026/07/17 15:35:08+08:00")
    assert parsed == "2026-07-17T07:35:08+00:00"
    assert reason is None

    parsed, reason = taiwan_lottery_collector._parse_draw_time("not-a-date")
    assert parsed is None
    assert reason == "invalid_datetime_format"


def test_taiwan_lottery_fetch_accepts_string_rt_code(monkeypatch):
    monkeypatch.setattr(
        taiwan_lottery_collector,
        "safe_get_json",
        lambda *args, **kwargs: {
            "ok": True,
            "elapsed_ms": 12,
            "ssl_fallback": True,
            "attempts": 2,
            "data": {
                "rtCode": "0",
                "content": {
                    "totalSize": 1,
                    "bingoQueryResult": [
                        {
                            "drawTerm": 115040897,
                            "dDate": "0001-01-01T00:00:00",
                            "bigShowOrder": [f"{number:02d}" for number in range(1, 21)],
                            "openShowOrder": [f"{number:02d}" for number in range(1, 21)],
                            "bullEyeTop": "20",
                            "winNoOnly": True,
                        }
                    ],
                },
            },
        },
    )

    draws = taiwan_lottery_collector.fetch_official_bingo_results("2026-07-21", page_num=1, page_size=100)
    diagnostics = taiwan_lottery_collector.get_last_official_fetch_diagnostics()

    assert draws[0]["issue"] == "115040897"
    assert len(draws[0]["numbers"]) == 20
    assert diagnostics[-1]["ok"] is True
    assert diagnostics[-1]["ssl_fallback"] is True
    assert diagnostics[-1]["parsed_count"] == 1


def test_official_draw_upsert_keeps_existing_draw_time_when_incoming_null(tmp_path, monkeypatch):
    monkeypatch.setattr(official_draw_store, "SQLITE_PATH", tmp_path / "bingo.db")
    monkeypatch.setattr(official_draw_store, "_cloud_enabled", lambda: False)
    official_draw_store.init_official_draw_tables()

    first = {
        **_draw(115040098),
        "draw_date": "2026-07-17",
        "draw_time": "2026-07-17T07:35:08+00:00",
        "raw_json": {"dDate": "2026-07-17T15:35:08"},
    }
    second = {**first, "draw_time": None}

    assert official_draw_store.save_official_draws([first])["saved"] == 1
    assert official_draw_store.save_official_draws([second])["saved"] == 1

    saved = official_draw_store.get_official_draw_by_issue("115040098")
    assert saved["draw_time"] == "2026-07-17T07:35:08+00:00"


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
    source_draws = [_draw(issue) for issue in range(101, 245)]
    saved_batches = []
    prediction_calls = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "100")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda max_pages=10, page_size=100: source_draws)
    monkeypatch.setattr(catch_up_service, "get_official_draw_history", lambda limit=200: [_draw(100)])
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "save_draw_verification", lambda item: {"status": "ok"})

    def fake_save(draws):
        saved_batches.append(draws)
        return {"status": "ok", "saved": len(draws), "storage": "test"}

    monkeypatch.setattr(catch_up_service, "save_official_draws", fake_save)
    monkeypatch.setattr(catch_up_service, "_record_structured_event", lambda *args, **kwargs: None)

    import services.prediction_lifecycle_orchestrator as lifecycle_orchestrator

    monkeypatch.setattr(
        lifecycle_orchestrator,
        "process_official_draw_lifecycle",
        lambda draw, **kwargs: prediction_calls.append(draw) or {
            "status": "ok",
            "verification": {"status": "ok"},
            "learning": {"status": "ok"},
            "prediction": {"status": "created", "target_issue": str(int(draw["issue"]) + 1)},
        },
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["status"] == "ok"
    assert result["max_batch_size"] == 120
    assert result["catch_count"] == 120
    assert result["success_count"] == 120
    assert len(saved_batches[0]) == 120
    assert prediction_calls[0]["issue"] == "220"
    assert result["prediction"]["status"] == "created"


def test_catch_up_recovers_missing_source_issues_at_or_below_database_latest(monkeypatch):
    source_draws = [_draw(issue) for issue in range(101, 106)]
    saved_batches = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "105")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda max_pages=10, page_size=100: source_draws)
    monkeypatch.setattr(catch_up_service, "get_official_draw_history", lambda limit=240: [_draw(101), _draw(102), _draw(104), _draw(105)])
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "save_draw_verification", lambda item: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "_record_structured_event", lambda *args, **kwargs: None)

    def fake_save(draws):
        saved_batches.append(draws)
        return {"status": "ok", "saved": len(draws), "storage": "test"}

    monkeypatch.setattr(catch_up_service, "save_official_draws", fake_save)

    import services.prediction_lifecycle_orchestrator as lifecycle_orchestrator

    monkeypatch.setattr(
        lifecycle_orchestrator,
        "process_official_draw_lifecycle",
        lambda draw, **kwargs: {
            "status": "ok",
            "verification": {"status": "ok"},
            "learning": {"status": "ok"},
            "prediction": {"status": "created", "target_issue": str(int(draw["issue"]) + 1)},
        },
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["status"] == "ok"
    assert result["catch_count"] == 1
    assert [draw["issue"] for draw in saved_batches[0]] == ["103"]
    assert result["start_issue"] == "103"
    assert result["end_issue"] == "103"


def test_catch_up_already_synced_triggers_next_prediction(monkeypatch):
    prediction_calls = []

    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "115039887")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda max_pages=10, page_size=100: [_draw(115039887)])
    monkeypatch.setattr(catch_up_service, "get_official_draw_history", lambda limit=240: [_draw(115039887)])
    monkeypatch.setattr(catch_up_service, "get_latest_official_draw", lambda: _draw(115039887))
    monkeypatch.setattr(catch_up_service, "run_official_verification", lambda limit=10: {"status": "ok"})
    monkeypatch.setattr(catch_up_service, "_record_structured_event", lambda *args, **kwargs: None)

    import services.prediction_lifecycle_orchestrator as lifecycle_orchestrator

    monkeypatch.setattr(
        lifecycle_orchestrator,
        "process_official_draw_lifecycle",
        lambda draw, **kwargs: prediction_calls.append(draw) or {
            "status": "ok",
            "verification": {"status": "ok"},
            "learning": {"status": "ok"},
            "prediction": {"status": "created", "target_issue": "115039888"},
        },
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
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda max_pages=10, page_size=100: source_draws)
    monkeypatch.setattr(catch_up_service, "get_official_draw_history", lambda limit=240: [_draw(100)])
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


def test_catch_up_source_error_includes_fetch_diagnostics(monkeypatch):
    monkeypatch.setattr(catch_up_service, "get_database_latest_issue", lambda: "115040850")
    monkeypatch.setattr(catch_up_service, "fetch_source_today_draws", lambda max_pages=10, page_size=100: [])
    monkeypatch.setattr(
        catch_up_service,
        "get_last_official_fetch_diagnostics",
        lambda: [{"open_date": "2026-07-21", "error_type": "ssl", "ok": False}],
    )

    result = catch_up_service.catch_up_missing_issues()

    assert result["exit_reason"] == "source_error"
    assert result["reason"] == "source_latest_issue_unavailable"
    assert result["source_fetch_diagnostics"][-1]["error_type"] == "ssl"


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
