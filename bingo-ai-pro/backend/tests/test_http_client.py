from __future__ import annotations

import pathlib
import sys

import pytest
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.http_client import safe_get_json


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
    assert calls[0]["timeout"] == (5, 15)


def test_safe_get_json_ssl_fallback_once(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("certificate failed")
        return DummyResponse({"rtCode": 0})

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is True
    assert result["ssl_fallback"] is True
    assert [call["verify"] for call in calls] == [True, False]


def test_safe_get_json_ssl_fallback_stops_after_second_failure(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.SSLError("certificate failed")

    monkeypatch.setattr(requests, "get", fake_get)

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == "ssl"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "exception,error_type",
    [
        (requests.exceptions.ConnectTimeout("connect timeout"), "timeout"),
        (requests.exceptions.ReadTimeout("read timeout"), "timeout"),
    ],
)
def test_safe_get_json_timeout(monkeypatch, exception, error_type):
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(exception))

    result = safe_get_json("https://example.test/api")

    assert result["ok"] is False
    assert result["error_type"] == error_type


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
