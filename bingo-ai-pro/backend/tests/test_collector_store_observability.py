from __future__ import annotations

import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import collector_store


def setup_function():
    collector_store._record_db_path_status(
        backend=None,
        result=None,
        fallback_occurred=False,
        error_type=None,
    )


def _messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "database.collector_store"]


def test_collector_db_path_status_initial_state_is_empty():
    assert collector_store.get_collector_db_path_status() == {
        "backend": None,
        "result": None,
        "fallback_occurred": False,
        "error_type": None,
    }


def test_query_with_fallback_logs_postgres_success_without_sqlite(monkeypatch, caplog):
    rows = [("ok",)]
    sqlite_called = []

    monkeypatch.setattr(collector_store, "_query_cloud", lambda sql, params=(): rows)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda *args, **kwargs: sqlite_called.append(True) or [])

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        result = collector_store._query_with_fallback("select 1", ("secret-param",))

    assert result == rows
    assert sqlite_called == []
    messages = _messages(caplog)
    assert "collector_store_query backend=postgres result=success" in messages
    assert "collector_store_query backend=sqlite result=fallback" not in messages
    assert "secret-param" not in "\n".join(messages)
    assert collector_store.get_collector_db_path_status() == {
        "backend": "postgres",
        "result": "success",
        "fallback_occurred": False,
        "error_type": None,
    }


def test_query_with_fallback_logs_postgres_failure_and_sqlite_success(monkeypatch, caplog):
    rows = [("fallback",)]

    def fail_cloud(sql, params=()):
        raise ConnectionError("sensitive host details")

    monkeypatch.setattr(collector_store, "_query_cloud", fail_cloud)
    monkeypatch.setattr(collector_store, "_query_sqlite", lambda sql, params=(): rows)

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        result = collector_store._query_with_fallback("select %s", ("secret-param",), sqlite_sql="select ?")

    assert result == rows
    messages = _messages(caplog)
    assert "collector_store_query backend=postgres result=failed error_type=ConnectionError" in messages
    assert "collector_store_query backend=sqlite result=fallback" in messages
    assert "collector_store_query backend=sqlite result=success" in messages
    assert "sensitive host details" not in "\n".join(messages)
    assert "secret-param" not in "\n".join(messages)
    assert collector_store.get_collector_db_path_status() == {
        "backend": "sqlite",
        "result": "success",
        "fallback_occurred": True,
        "error_type": "ConnectionError",
    }


def test_query_with_fallback_logs_both_failures_and_preserves_empty_return(monkeypatch, caplog):
    def fail_cloud(sql, params=()):
        raise TimeoutError("sensitive postgres timeout")

    def fail_sqlite(sql, params=()):
        raise RuntimeError("sensitive sqlite path")

    monkeypatch.setattr(collector_store, "_query_cloud", fail_cloud)
    monkeypatch.setattr(collector_store, "_query_sqlite", fail_sqlite)

    with caplog.at_level(logging.INFO, logger="database.collector_store"):
        result = collector_store._query_with_fallback("select %s", ("secret-param",), sqlite_sql="select ?")

    assert result == []
    messages = _messages(caplog)
    assert "collector_store_query backend=postgres result=failed error_type=TimeoutError" in messages
    assert "collector_store_query backend=sqlite result=fallback" in messages
    assert "collector_store_query backend=sqlite result=failed error_type=RuntimeError" in messages
    joined = "\n".join(messages)
    assert "sensitive postgres timeout" not in joined
    assert "sensitive sqlite path" not in joined
    assert "secret-param" not in joined
    assert collector_store.get_collector_db_path_status() == {
        "backend": "sqlite",
        "result": "failed",
        "fallback_occurred": True,
        "error_type": "RuntimeError",
    }
