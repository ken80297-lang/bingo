from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from requests.exceptions import HTTPError, SSLError, Timeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5, 30)
SSL_FALLBACK_ENABLED = os.getenv("OFFICIAL_SSL_FALLBACK_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS = int(os.getenv("OFFICIAL_SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS", "300"))
_SSL_FALLBACK_COOLDOWN_UNTIL: datetime | None = None


def _error(error_type: str, message: str, start: float, retryable: bool = True) -> dict:
    return {
        "ok": False,
        "source": "official",
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
    }


def _ssl_fallback_available() -> bool:
    return (
        SSL_FALLBACK_ENABLED
        and (_SSL_FALLBACK_COOLDOWN_UNTIL is None or datetime.now(timezone.utc) >= _SSL_FALLBACK_COOLDOWN_UNTIL)
    )


def _record_ssl_fallback_failure() -> None:
    global _SSL_FALLBACK_COOLDOWN_UNTIL
    _SSL_FALLBACK_COOLDOWN_UNTIL = datetime.now(timezone.utc) + timedelta(seconds=SSL_FALLBACK_FAILURE_COOLDOWN_SECONDS)


def _record_ssl_fallback_success() -> None:
    global _SSL_FALLBACK_COOLDOWN_UNTIL
    _SSL_FALLBACK_COOLDOWN_UNTIL = None


def safe_get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
) -> dict:
    start = time.perf_counter()
    attempts = 0

    def request_json(*, verify: bool, timeout_value: tuple[int, int]) -> dict:
        nonlocal attempts
        attempts += 1
        response = requests.get(url, params=params, headers=headers, timeout=timeout_value, verify=verify)
        response.raise_for_status()
        return response.json()

    try:
        return {
            "ok": True,
            "source": "official",
            "data": request_json(verify=True, timeout_value=timeout),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            "ssl_fallback": False,
            "attempts": attempts,
        }
    except SSLError as exc:
        if _ssl_fallback_available():
            try:
                logger.warning("official http ssl verification failed; retrying with ssl_fallback")
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
                _record_ssl_fallback_success()
                return result
            except Exception as fallback_exc:
                _record_ssl_fallback_failure()
                return _error("ssl", f"{exc}; fallback_failed={fallback_exc}", start)
        return _error("ssl", str(exc), start)
    except Timeout as exc:
        try:
            retry_timeout = (max(timeout[0], 10), max(timeout[1], 45))
            return {
                "ok": True,
                "source": "official",
                "data": request_json(verify=True, timeout_value=retry_timeout),
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "ssl_fallback": False,
                "attempts": attempts,
                "timeout_retry": True,
                "timeout_error": str(exc),
            }
        except Exception as retry_exc:
            if isinstance(retry_exc, SSLError) and _ssl_fallback_available():
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
                    _record_ssl_fallback_success()
                    return result
                except Exception as fallback_exc:
                    _record_ssl_fallback_failure()
                    return _error("timeout", f"{exc}; retry_failed={retry_exc}; fallback_failed={fallback_exc}", start)
            return _error("timeout", f"{exc}; retry_failed={retry_exc}", start)
        return _error("timeout", str(exc), start)
    except HTTPError as exc:
        return _error("http", str(exc), start, retryable=False)
    except ValueError as exc:
        return _error("parse", str(exc), start, retryable=False)
    except Exception as exc:
        return _error("metadata", str(exc), start)
