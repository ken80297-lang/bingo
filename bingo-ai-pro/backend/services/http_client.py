from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from threading import RLock
from urllib.parse import urlsplit

import requests
import urllib3
from requests.exceptions import HTTPError, SSLError, Timeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5, 30)
SSL_FALLBACK_ENABLED = os.getenv("OFFICIAL_SSL_FALLBACK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS = int(os.getenv("OFFICIAL_SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS", "300"))
SSL_FALLBACK_PROBE_INTERVAL_SECONDS = int(os.getenv("OFFICIAL_SSL_FALLBACK_PROBE_INTERVAL_SECONDS", "60"))
_SSL_FALLBACK_COOLDOWNS: dict[str, dict] = {}
_SSL_FALLBACK_COOLDOWN_LOCK = RLock()


def _error(error_type: str, message: str, start: float, retryable: bool = True) -> dict:
    return {
        "ok": False,
        "source": "official",
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
    }


def _cooldown_key(url: str) -> str:
    parsed = urlsplit(url)
    return (parsed.hostname or parsed.netloc or url).lower()


def _ssl_cooldown_snapshot(url: str) -> dict:
    key = _cooldown_key(url)
    with _SSL_FALLBACK_COOLDOWN_LOCK:
        state = dict(_SSL_FALLBACK_COOLDOWNS.get(key) or {})
    return {
        "ssl_fallback_cooldown": bool(state),
        "cooldown_until": state.get("cooldown_until"),
        "next_probe_at": state.get("next_probe_at"),
        "suppressed_count": state.get("suppressed_count", 0),
    }


def _ssl_verified_request_gate(url: str) -> dict:
    if not SSL_FALLBACK_ENABLED:
        return {"allowed": True, "probe": False, "fallback_allowed": False}
    key = _cooldown_key(url)
    now = datetime.now(timezone.utc)
    with _SSL_FALLBACK_COOLDOWN_LOCK:
        state = _SSL_FALLBACK_COOLDOWNS.get(key)
        if not state:
            return {"allowed": True, "probe": False, "fallback_allowed": True}
        until = state.get("cooldown_until")
        if until and now >= until:
            _SSL_FALLBACK_COOLDOWNS.pop(key, None)
            return {"allowed": True, "probe": False, "fallback_allowed": True}
        next_probe = state.get("next_probe_at")
        if state.get("probe_in_progress") or (next_probe and now < next_probe):
            state["suppressed_count"] = int(state.get("suppressed_count") or 0) + 1
            return {"allowed": True, "probe": False, "fallback_only": True, "fallback_allowed": True, **_ssl_cooldown_snapshot(url)}
        state["probe_in_progress"] = True
        return {"allowed": True, "probe": True, "fallback_allowed": True}


def _record_ssl_fallback_cooldown(url: str, reason: str) -> None:
    key = _cooldown_key(url)
    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS)
    next_probe_at = now + timedelta(seconds=min(SSL_FALLBACK_PROBE_INTERVAL_SECONDS, SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS))
    with _SSL_FALLBACK_COOLDOWN_LOCK:
        previous = _SSL_FALLBACK_COOLDOWNS.get(key)
        suppressed = int((previous or {}).get("suppressed_count") or 0)
        _SSL_FALLBACK_COOLDOWNS[key] = {
            "cooldown_until": until,
            "next_probe_at": next_probe_at,
            "probe_in_progress": False,
            "last_warning_at": now if previous is None else (previous or {}).get("last_warning_at"),
            "suppressed_count": suppressed,
        }
    if previous is None:
        logger.warning(
            "official http ssl fallback cooldown opened host=%s duration_seconds=%s next_probe_seconds=%s reason=%s",
            key,
            SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS,
            min(SSL_FALLBACK_PROBE_INTERVAL_SECONDS, SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS),
            reason,
        )


def _record_ssl_verified_success(url: str) -> None:
    key = _cooldown_key(url)
    with _SSL_FALLBACK_COOLDOWN_LOCK:
        _SSL_FALLBACK_COOLDOWNS.pop(key, None)


def _ssl_cooldown_error(url: str, start: float) -> dict:
    result = _error("ssl", "ssl fallback cooldown active", start)
    result.update(_ssl_cooldown_snapshot(url))
    return result


def safe_get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
) -> dict:
    start = time.perf_counter()
    attempts = 0
    gate = _ssl_verified_request_gate(url)
    if not gate.get("allowed"):
        result = _ssl_cooldown_error(url, start)
        result["attempts"] = attempts
        return result

    def request_json(*, verify: bool, timeout_value: tuple[int, int]) -> dict:
        nonlocal attempts
        attempts += 1
        response = requests.get(url, params=params, headers=headers, timeout=timeout_value, verify=verify)
        response.raise_for_status()
        return response.json()

    if gate.get("fallback_only"):
        try:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            return {
                "ok": True,
                "source": "official",
                "data": request_json(verify=False, timeout_value=timeout),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "ssl_fallback": True,
                "ssl_fallback_cooldown": True,
                "attempts": attempts,
            }
        except Exception as fallback_exc:
            _record_ssl_fallback_cooldown(url, "cooldown_fallback_failed")
            result = _error("ssl", f"cooldown_fallback_failed={fallback_exc}", start)
            result["attempts"] = attempts
            result["ssl_fallback_cooldown"] = True
            return result

    try:
        result = {
            "ok": True,
            "source": "official",
            "data": request_json(verify=True, timeout_value=timeout),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            "ssl_fallback": False,
            "attempts": attempts,
        }
        _record_ssl_verified_success(url)
        return result
    except SSLError as exc:
        _record_ssl_fallback_cooldown(url, "verified_ssl_failure")
        if gate.get("fallback_allowed"):
            try:
                logger.warning("official http ssl verification failed; retrying with ssl_fallback host=%s", _cooldown_key(url))
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                result = {
                    "ok": True,
                    "source": "official",
                    "data": request_json(verify=False, timeout_value=timeout),
                    "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                    "ssl_fallback": True,
                    "attempts": attempts,
                    "ssl_error": str(exc),
                }
                return result
            except Exception as fallback_exc:
                _record_ssl_fallback_cooldown(url, "fallback_failed")
                return _error("ssl", f"{exc}; fallback_failed={fallback_exc}", start)
        result = _error("ssl", str(exc), start)
        result.update(_ssl_cooldown_snapshot(url))
        return result
    except Timeout as exc:
        try:
            retry_timeout = (max(timeout[0], 10), max(timeout[1], 45))
            result = {
                "ok": True,
                "source": "official",
                "data": request_json(verify=True, timeout_value=retry_timeout),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "ssl_fallback": False,
                "attempts": attempts,
                "timeout_retry": True,
                "timeout_error": str(exc),
            }
            _record_ssl_verified_success(url)
            return result
        except Exception as retry_exc:
            if isinstance(retry_exc, SSLError):
                _record_ssl_fallback_cooldown(url, "timeout_retry_ssl_failure")
            else:
                _record_ssl_verified_success(url)
            if isinstance(retry_exc, SSLError) and gate.get("fallback_allowed"):
                try:
                    retry_timeout = (max(timeout[0], 10), max(timeout[1], 45))
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    result = {
                        "ok": True,
                        "source": "official",
                        "data": request_json(verify=False, timeout_value=retry_timeout),
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                        "ssl_fallback": True,
                        "attempts": attempts,
                        "timeout_retry": True,
                        "timeout_error": str(exc),
                        "ssl_error": str(retry_exc),
                    }
                    return result
                except Exception as fallback_exc:
                    _record_ssl_fallback_cooldown(url, "timeout_retry_fallback_failed")
                    return _error("timeout", f"{exc}; retry_failed={retry_exc}; fallback_failed={fallback_exc}", start)
            if isinstance(retry_exc, SSLError):
                result = _error("timeout", f"{exc}; retry_failed={retry_exc}", start)
                result.update(_ssl_cooldown_snapshot(url))
                return result
            return _error("timeout", f"{exc}; retry_failed={retry_exc}", start)
        return _error("timeout", str(exc), start)
    except HTTPError as exc:
        _record_ssl_verified_success(url)
        return _error("http", str(exc), start, retryable=False)
    except ValueError as exc:
        _record_ssl_verified_success(url)
        return _error("parse", str(exc), start, retryable=False)
    except Exception as exc:
        return _error("metadata", str(exc), start)
