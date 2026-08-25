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
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)

    postgres.get_connection()

    assert calls == [("postgres://example", {"connect_timeout": 3})]
