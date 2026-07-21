from __future__ import annotations

import logging
import time

import requests
from requests.exceptions import HTTPError, SSLError, Timeout

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (5, 15)


def _error(error_type: str, message: str, start: float, retryable: bool = True) -> dict:
    return {
        "ok": False,
        "source": "official",
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
    }


def safe_get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
) -> dict:
    start = time.perf_counter()
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout, verify=True)
        response.raise_for_status()
        return {
            "ok": True,
            "source": "official",
            "data": response.json(),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            "ssl_fallback": False,
        }
    except SSLError as exc:
        logger.warning("official http ssl verification failed; keeping result pending_verification")
        return _error("ssl", str(exc), start)
    except Timeout as exc:
        return _error("timeout", str(exc), start)
    except HTTPError as exc:
        return _error("http", str(exc), start, retryable=False)
    except ValueError as exc:
        return _error("parse", str(exc), start, retryable=False)
    except Exception as exc:
        return _error("metadata", str(exc), start)
