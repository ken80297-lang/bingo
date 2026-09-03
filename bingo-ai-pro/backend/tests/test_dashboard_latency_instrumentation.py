from __future__ import annotations

import logging
import pathlib
import sys
import time
from concurrent.futures import Future

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store, official_draw_store
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
    monkeypatch.setattr(official_draw_store, "_cloud_connection", lambda: conn)
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

    monkeypatch.setattr(collector_store, "_cloud_connection", lambda: conn)
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

    monkeypatch.setattr(collector_store, "_cloud_connection", fail_connect)
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


def _completed_future(value):
    future = Future()
    future.set_result(value)
    return future
