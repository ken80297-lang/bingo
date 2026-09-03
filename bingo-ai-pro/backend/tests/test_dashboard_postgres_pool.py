from __future__ import annotations

import pathlib
import sys
import threading
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store, official_draw_store, postgres


class FakeCursor:
    def __init__(self, rows=None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.execute_count = 0
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=(), **kwargs):
        self.execute_count += 1
        self.sql = sql
        self.params = params
        if self.error:
            raise self.error

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor, name: str = "conn"):
        self.cursor_obj = cursor
        self.name = name
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited += 1
        return False

    def cursor(self):
        return self.cursor_obj


class FakeConnectionContext:
    def __init__(self, pool, connection):
        self.pool = pool
        self.connection_obj = connection

    def __enter__(self):
        self.pool.active.append(self.connection_obj)
        return self.connection_obj

    def __exit__(self, exc_type, exc, tb):
        self.pool.active.remove(self.connection_obj)
        self.pool.returned.append((self.connection_obj, exc_type))
        return False


class FakePool:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.open_calls = []
        self.close_calls = []
        self.connection_timeouts = []
        self.closed = False
        self.created_connections = []
        self.active = []
        self.returned = []
        FakePool.instances.append(self)

    def open(self, **kwargs):
        self.open_calls.append(kwargs)

    def close(self, **kwargs):
        self.close_calls.append(kwargs)
        self.closed = True

    def connection(self, timeout=None):
        self.connection_timeouts.append(timeout)
        connection = FakeConnection(FakeCursor(rows=[]), name=f"conn-{len(self.created_connections) + 1}")
        self.created_connections.append(connection)
        return FakeConnectionContext(self, connection)


@pytest.fixture(autouse=True)
def reset_pool():
    postgres.close_dashboard_read_pool()
    FakePool.instances.clear()
    yield
    postgres.close_dashboard_read_pool()
    FakePool.instances.clear()


def test_dashboard_read_pool_is_lazy_and_bounded(monkeypatch):
    fake_module = types.SimpleNamespace(ConnectionPool=FakePool)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_module)
    monkeypatch.setattr(postgres, "DATABASE_URL", "postgres://example")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "2")

    assert FakePool.instances == []

    with postgres.dashboard_read_connection() as conn:
        assert conn.name == "conn-1"

    assert len(FakePool.instances) == 1
    pool = FakePool.instances[0]
    assert pool.kwargs["conninfo"] == "postgres://example"
    assert pool.kwargs["kwargs"] == {"connect_timeout": 2}
    assert pool.kwargs["min_size"] == 0
    assert pool.kwargs["max_size"] == 2
    assert pool.kwargs["open"] is False
    assert pool.kwargs["timeout"] == 1.0
    assert pool.open_calls == [{"wait": False}]
    assert pool.connection_timeouts == [1.0]
    assert pool.returned[0][0] is conn


def test_close_dashboard_read_pool_is_safe_and_idempotent(monkeypatch):
    fake_module = types.SimpleNamespace(ConnectionPool=FakePool)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_module)

    postgres.close_dashboard_read_pool()
    with postgres.dashboard_read_connection():
        pass
    pool = FakePool.instances[0]

    postgres.close_dashboard_read_pool()
    postgres.close_dashboard_read_pool()

    assert pool.close_calls == [{"timeout": 1.0}]


def test_concurrent_dashboard_pool_borrowers_get_distinct_connections(monkeypatch):
    fake_module = types.SimpleNamespace(ConnectionPool=FakePool)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_module)
    both_entered = threading.Event()
    release = threading.Event()
    borrowed = []
    lock = threading.Lock()

    def borrow():
        with postgres.dashboard_read_connection() as conn:
            with lock:
                borrowed.append(conn)
                if len(borrowed) == 2:
                    both_entered.set()
            release.wait(timeout=1)

    first = threading.Thread(target=borrow)
    second = threading.Thread(target=borrow)
    first.start()
    second.start()
    assert both_entered.wait(timeout=1)

    assert len(borrowed) == 2
    assert borrowed[0] is not borrowed[1]

    release.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert len(FakePool.instances[0].returned) == 2


def test_get_connection_keeps_original_psycopg_path(monkeypatch):
    calls = []

    def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(postgres, "DATABASE_URL", "postgres://example")
    monkeypatch.setattr(postgres, "_FAILURE_UNTIL", 0.0)
    monkeypatch.setattr(postgres, "_FAILURE_MESSAGE", None)
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)

    postgres.get_connection()

    assert calls == [("postgres://example", {"connect_timeout": 2})]
    assert FakePool.instances == []


def test_only_official_draw_latest_operation_uses_dashboard_pool(monkeypatch):
    calls = []
    pooled = FakeConnection(FakeCursor(rows=[]), name="pooled")
    regular = FakeConnection(FakeCursor(rows=[]), name="regular")
    monkeypatch.setattr(official_draw_store, "_dashboard_read_connection", lambda: calls.append("pool") or pooled)
    monkeypatch.setattr(official_draw_store, "_cloud_connection", lambda: calls.append("regular") or regular)

    official_draw_store._query_cloud("select 1", operation="official_draw_latest")
    official_draw_store._query_cloud("select 1")

    assert calls == ["pool", "regular"]
    assert pooled.cursor_obj.execute_count == 1
    assert regular.cursor_obj.execute_count == 1


def test_only_kuaishou_latest_operation_uses_dashboard_pool(monkeypatch):
    calls = []
    pooled = FakeConnection(FakeCursor(rows=[]), name="pooled")
    regular = FakeConnection(FakeCursor(rows=[]), name="regular")
    monkeypatch.setattr(collector_store, "_dashboard_read_connection", lambda: calls.append("pool") or pooled)
    monkeypatch.setattr(collector_store, "_cloud_connection", lambda: calls.append("regular") or regular)

    collector_store._query_cloud("select 1", operation="kuaishou_latest")
    collector_store._query_cloud("select 1")

    assert calls == ["pool", "regular"]
    assert pooled.cursor_obj.execute_count == 1
    assert regular.cursor_obj.execute_count == 1


def test_pool_acquire_failure_preserves_collector_sqlite_fallback(monkeypatch):
    def fail_pool():
        raise TimeoutError("pool exhausted")

    sqlite_rows = [(1, "115040900", "2026-07-30T12:00:00+08:00", "kuaishou", "created", "updated")]
    monkeypatch.setattr(collector_store, "_dashboard_read_connection", fail_pool)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda sql, params=(): sqlite_rows)

    result = collector_store.get_latest_kuaishou_summary()

    assert result["issue"] == "115040900"
    assert collector_store.get_collector_db_path_status()["backend"] == "sqlite"


def test_pool_query_failure_preserves_official_sqlite_fallback(monkeypatch):
    rows = [
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
    monkeypatch.setattr(
        official_draw_store,
        "_dashboard_read_connection",
        lambda: FakeConnection(FakeCursor(error=RuntimeError("query failed"))),
    )
    monkeypatch.setattr(official_draw_store, "_query_sqlite", lambda sql, params=(): rows)

    result = official_draw_store.get_latest_official_draw_summary()

    assert result["issue"] == "115040900"


def test_app_startup_does_not_create_dashboard_read_pool(monkeypatch):
    import app as app_module

    def fail_pool_creation():
        raise AssertionError("startup should not create dashboard read pool")

    class EmptyScheduler:
        running = False

        def get_jobs(self):
            return []

    monkeypatch.setattr(postgres, "_create_dashboard_read_pool", fail_pool_creation)
    monkeypatch.setattr(app_module, "STARTUP_DB_INIT_ENABLED", False)
    monkeypatch.setattr(app_module, "OPERATIONS_DB_INIT_ENABLED", False)
    monkeypatch.setattr(app_module, "DAILY_RECOVERY_ENABLED", False)
    monkeypatch.setattr(app_module, "_ensure_scheduler_listener", lambda: None)
    monkeypatch.setattr(app_module, "_schedule_background_cache_jobs", lambda: None)
    monkeypatch.setattr(app_module, "_schedule_production_catch_up_jobs", lambda: None)
    monkeypatch.setattr(app_module, "_schedule_collector_jobs", lambda: None)
    monkeypatch.setattr(app_module, "_schedule_data_quality_jobs", lambda: None)
    monkeypatch.setattr(app_module, "_schedule_legacy_refresh_jobs", lambda: None)
    monkeypatch.setattr(app_module, "scheduler", EmptyScheduler())

    app_module.startup_event()


def test_app_shutdown_closes_dashboard_read_pool(monkeypatch):
    import app as app_module
    from services import latest_sync, prediction_service

    calls = []

    class StoppedScheduler:
        running = False

    monkeypatch.setattr(latest_sync, "shutdown_latest_sync_background_tasks", lambda: calls.append("latest"))
    monkeypatch.setattr(prediction_service, "shutdown_prediction_background_tasks", lambda: calls.append("prediction"))
    monkeypatch.setattr(postgres, "close_dashboard_read_pool", lambda: calls.append("pool"))
    monkeypatch.setattr(app_module, "scheduler", StoppedScheduler())
    monkeypatch.setattr(app_module.app.state, "last_health_request_at", None, raising=False)
    monkeypatch.setattr(app_module.app.state, "health_request_count_since_start", 0, raising=False)
    monkeypatch.setattr(app_module.app.state, "last_health_request_method", None, raising=False)
    monkeypatch.setattr(app_module.app.state, "wake_source", "none", raising=False)

    app_module.shutdown_event()

    assert calls == ["latest", "prediction", "pool"]
