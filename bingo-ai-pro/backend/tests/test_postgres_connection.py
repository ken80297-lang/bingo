from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from database import postgres


def test_postgres_connection_uses_short_connect_timeout(monkeypatch):
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


def test_postgres_connection_timeout_falls_back_to_safe_default(monkeypatch):
    calls = []

    def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(postgres, "DATABASE_URL", "postgres://example")
    monkeypatch.setattr(postgres, "_FAILURE_UNTIL", 0.0)
    monkeypatch.setattr(postgres, "_FAILURE_MESSAGE", None)
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)

    postgres.get_connection()

    assert calls == [("postgres://example", {"connect_timeout": 3})]


def test_postgres_connection_failure_opens_short_circuit(monkeypatch):
    calls = []

    def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        raise OSError("tenant not found")

    monkeypatch.setattr(postgres, "DATABASE_URL", "postgres://example")
    monkeypatch.setattr(postgres, "_FAILURE_UNTIL", 0.0)
    monkeypatch.setattr(postgres, "_FAILURE_MESSAGE", None)
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DATABASE_FAILURE_COOLDOWN_SECONDS", "60")
    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)

    try:
        postgres.get_connection()
    except OSError:
        pass
    else:
        raise AssertionError("expected connection failure")

    try:
        postgres.get_connection()
    except RuntimeError as exc:
        assert "temporarily unavailable" in str(exc)
    else:
        raise AssertionError("expected circuit breaker failure")

    assert calls == [("postgres://example", {"connect_timeout": 2})]
