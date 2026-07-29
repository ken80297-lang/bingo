from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import http_client
from services.http_client import safe_get_json


@pytest.fixture(autouse=True)
def reset_ssl_fallback_cooldown():
    http_client._SSL_FALLBACK_COOLDOWNS.clear()
    yield
    http_client._SSL_FALLBACK_COOLDOWNS.clear()


class DummyResponse:
    def __init__(self, payload=None, status_error=None, json_error=None):
        self.payload = payload or {"ok": True}
        self.status_error = status_error
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


def test_safe_get_json_success(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return DummyResponse({"rtCode": 0})

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is True
    assert result["data"] == {"rtCode": 0}
    assert result["ssl_fallback"] is False
    assert calls[0]["verify"] is True
    assert calls[0]["timeout"] == (5, 30)


def test_safe_get_json_ssl_failure_uses_marked_fallback(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("verify") is True:
            raise requests.exceptions.SSLError("certificate failed")
        return DummyResponse({"rtCode": 0})

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is True
    assert result["data"] == {"rtCode": 0}
    assert result["ssl_fallback"] is True
    assert len(calls) == 2
    assert calls[0]["verify"] is True
    assert calls[1]["verify"] is False


def test_safe_get_json_ssl_failure_reports_fallback_failure(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.SSLError("certificate failed")

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == "ssl"
    assert len(calls) == 2
    assert calls[0]["verify"] is True
    assert calls[1]["verify"] is False


def test_safe_get_json_ssl_fallback_failure_opens_cooldown(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.SSLError("certificate failed")

    monkeypatch.setattr(requests, "get", fake_get)

    first = safe_get_json("https://example.test/api")
    second = safe_get_json("https://example.test/api")

    assert first["ok"] is False
    assert second["ok"] is False
    assert len(calls) == 2
    assert calls[0]["verify"] is True
    assert calls[1]["verify"] is False
    assert second["attempts"] == 0
    assert second["ssl_fallback_cooldown"] is True


def test_safe_get_json_ssl_cooldown_is_host_level(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        if "example.test" in url:
            raise requests.exceptions.SSLError("certificate failed")
        if kwargs.get("verify") is True:
            raise requests.exceptions.SSLError("other certificate failed")
        return DummyResponse({"rtCode": 0})

    monkeypatch.setattr(requests, "get", fake_get)

    first = safe_get_json("https://example.test/api?a=1")
    second = safe_get_json("https://example.test/api?b=2")
    other = safe_get_json("https://other.test/api")

    assert first["ok"] is False
    assert second["ok"] is False
    assert second["ssl_fallback_cooldown"] is True
    assert second["attempts"] == 0
    assert other["ok"] is True
    assert [item[1]["verify"] for item in calls] == [True, False, True, False]


def test_safe_get_json_ssl_cooldown_allows_next_probe_without_fallback(monkeypatch):
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.SSLError("certificate failed")

    monkeypatch.setattr(requests, "get", fake_get)

    first = safe_get_json("https://example.test/api")
    key = http_client._cooldown_key("https://example.test/api")
    http_client._SSL_FALLBACK_COOLDOWNS[key]["next_probe_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    probe = safe_get_json("https://example.test/api")

    assert first["ok"] is False
    assert probe["ok"] is False
    assert probe["ssl_fallback_cooldown"] is True
    assert [item["verify"] for item in calls] == [True, False, True]


@pytest.mark.parametrize(
    "exception,error_type",
    [
        (requests.exceptions.ConnectTimeout("connect timeout"), "timeout"),
        (requests.exceptions.ReadTimeout("read timeout"), "timeout"),
    ],
)
def test_safe_get_json_timeout(monkeypatch, exception, error_type):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        raise exception

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == error_type
    assert len(calls) == 2


def test_safe_get_json_http_error_does_not_use_ssl_fallback(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return DummyResponse(status_error=requests.exceptions.HTTPError("500"))

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == "http"
    assert len(calls) == 1
    assert calls[0]["verify"] is True


def test_safe_get_json_parse_error(monkeypatch):
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: DummyResponse(json_error=ValueError("not json")),
    )

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == "parse"
