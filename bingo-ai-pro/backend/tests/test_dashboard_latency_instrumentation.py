from __future__ import annotations

import logging
import pathlib
import sys
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store, learning_store, official_draw_store, prediction_history_store
from services import player_dashboard


@pytest.fixture(autouse=True)
def reset_player_dashboard_state():
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    for key, value in list(player_dashboard._PLAYER_COMPONENT_CACHE.items()):
        player_dashboard._PLAYER_COMPONENT_CACHE[key] = [] if isinstance(value, list) else None
    player_dashboard._PLAYER_COMPONENT_CACHE["prediction_aggregates"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["analysis"] = {}
    player_dashboard._PLAYER_COMPONENT_CACHE["kuaishou"] = {}
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()
    for key in player_dashboard._PLAYER_RUNTIME_METRICS:
        player_dashboard._PLAYER_RUNTIME_METRICS[key] = 0
    yield
    player_dashboard._PLAYER_SUMMARY_CACHE["payload"] = None
    player_dashboard._PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    player_dashboard._PLAYER_COMPONENT_IN_FLIGHT.clear()


class FakeCursor:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.execute_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=(), **kwargs):
        self.execute_count += 1
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.cursor_obj = cursor
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def cursor(self):
        return self.cursor_obj


def _messages(caplog, logger_name: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == logger_name]


def test_official_draw_latest_summary_logs_postgres_latency_once(monkeypatch, caplog):
    cursor = FakeCursor(
        rows=[
            (
                1,
                "115040900",
                "2026-07-30",
                "2026-07-30T12:00:00+08:00",
                "[1, 2, 3]",
                "[1, 2, 3]",
                3,
                False,
                "official",
                "verified",
                None,
                True,
                "created",
                "updated",
            )
        ]
    )
    conn = FakeConnection(cursor)

    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setattr(official_draw_store, "_dashboard_read_connection", lambda: conn)
    monkeypatch.setattr(official_draw_store, "_query_sqlite", lambda *args, **kwargs: pytest.fail("sqlite fallback should not run"))

    with caplog.at_level(logging.INFO, logger="database.official_draw_store"):
        result = official_draw_store.get_latest_official_draw_summary()

    assert result["issue"] == "115040900"
    assert cursor.execute_count == 1
    assert conn.closed is True
    joined = "\n".join(_messages(caplog, "database.official_draw_store"))
    assert "postgres_latency operation=official_draw_latest" in joined
    assert "connect_ms=" in joined
    assert "query_ms=" in joined
    assert "result=success" in joined
    assert "postgres://secret" not in joined


def test_kuaishou_latest_summary_logs_postgres_latency_once(monkeypatch, caplog):
    cursor = FakeCursor(rows=[(1, "115040900", "2026-07-30T12:00:00+08:00", "kuaishou", "created", "updated")])
    conn = FakeConnection(cursor)

    monkeypatch.setattr(collector_store, "_dashboard_read_connection", lambda: conn)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda *args, **kwargs: pytest.fail("sqlite fallback should not run"))

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        result = collector_store.get_latest_kuaishou_summary()

    assert result["issue"] == "115040900"
    assert cursor.execute_count == 1
    assert conn.closed is True
    joined = "\n".join(_messages(caplog, "database.collector_store"))
    assert "postgres_latency operation=kuaishou_latest" in joined
    assert "connect_ms=" in joined
    assert "query_ms=" in joined
    assert "result=success" in joined


def test_postgres_latency_connection_failure_hides_raw_error(monkeypatch, caplog):
    def fail_connect():
        raise ConnectionError("secret host and credential details")

    monkeypatch.setattr(collector_store, "_dashboard_read_connection", fail_connect)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda sql, params=(): [])

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        assert collector_store.get_latest_kuaishou_summary() is None

    joined = "\n".join(_messages(caplog, "database.collector_store"))
    assert "postgres_latency operation=kuaishou_latest" in joined
    assert "result=failed" in joined
    assert "error_type=ConnectionError" in joined
    assert "secret host" not in joined
    assert "credential" not in joined


def test_dashboard_component_latency_preserves_result(monkeypatch, caplog):
    monkeypatch.setattr(player_dashboard._PLAYER_EXECUTOR, "submit", lambda fn: _completed_future(fn()))

    with caplog.at_level(logging.INFO, logger="services.player_dashboard"):
        future, state = player_dashboard._submit_component("kuaishou", lambda: {"status": "ok"})

    assert state == "submitted"
    assert future.result() == {"status": "ok"}
    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "dashboard_component_latency component=kuaishou" in joined
    assert "queue_ms=" in joined
    assert "execution_ms=" in joined
    assert "result=success" in joined


def test_dashboard_component_timeout_fallback_behavior_unchanged():
    warnings: list[str] = []
    timings: list[dict] = []
    blocked = Future()

    result = player_dashboard._component_result(
        "kuaishou",
        blocked,
        deadline=time.monotonic() + 1,
        timeout_seconds=0.001,
        timings=timings,
        warnings=warnings,
        fallback={},
    )

    assert result == {}
    assert warnings == ["kuaishou fallback cache"]
    assert timings[0]["result"] == "timeout"
    assert blocked.cancelled() is False


def test_dashboard_component_stage_latency_preserves_result(caplog):
    with caplog.at_level(logging.WARNING, logger="services.player_dashboard"):
        result = player_dashboard._timed_component_stage("card_two", "analysis_lookup", lambda: {"status": "ok"})

    assert result == {"status": "ok"}
    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "component_stage_latency component=card_two stage=analysis_lookup" in joined
    assert "duration_ms=" in joined
    assert "result=success" in joined


def test_dashboard_component_stage_latency_preserves_exception(caplog):
    with caplog.at_level(logging.WARNING, logger="services.player_dashboard"):
        with pytest.raises(RuntimeError):
            player_dashboard._timed_component_stage(
                "previous_verification",
                "prediction_target_lookup",
                lambda: (_ for _ in ()).throw(RuntimeError("raw details")),
            )

    joined = "\n".join(_messages(caplog, "services.player_dashboard"))
    assert "component_stage_latency component=previous_verification stage=prediction_target_lookup" in joined
    assert "result=failed" in joined
    assert "error_type=RuntimeError" in joined
    assert "raw details" not in joined


def test_prediction_history_summary_stage_adds_no_extra_query(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return []

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_history_summary_records(
            100,
            diagnostic_component="card_two_history",
        )

    assert result == []
    assert len(calls) == 1
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency component=card_two_history stage=prediction_history_summary_query" in joined
    assert "result=success" in joined


def test_prediction_aggregates_stage_adds_no_extra_query(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(learning_store, "get_learned_live_target_count", lambda: 7)

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return [(10, 9, 1, 8, 6, 6, 5)]

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_lifecycle_aggregates(
            diagnostic_component="prediction_aggregates",
        )

    assert result["total_prediction_count"] == 10
    assert result["learned_distinct_target_count"] == 7
    assert len(calls) == 2
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency component=prediction_aggregates stage=learned_live_target_count" in joined
    assert "component_stage_latency component=prediction_aggregates stage=prediction_history_aggregate_query" in joined
    assert "component_stage_latency component=prediction_aggregates stage=official_result_join_count" in joined


def test_prediction_history_stage_logging_is_dashboard_opt_in(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)

    def fake_query(sql, params=(), sqlite_sql=None):
        calls.append((sql, params, sqlite_sql))
        return []

    monkeypatch.setattr(prediction_history_store, "_query_with_fallback", fake_query)

    with caplog.at_level(logging.WARNING, logger="database.prediction_history_store"):
        result = prediction_history_store.get_prediction_history_summary_records(100)

    assert result == []
    assert len(calls) == 1
    joined = "\n".join(_messages(caplog, "database.prediction_history_store"))
    assert "component_stage_latency" not in joined


def _completed_future(value):
    future = Future()
    future.set_result(value)
    return future
