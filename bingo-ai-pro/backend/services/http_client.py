from __future__ import annotations

import logging
import time

import requests
import urllib3
from requests.exceptions import HTTPError, SSLError, Timeout
from urllib3.exceptions import InsecureRequestWarning

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
        logger.warning("official http ssl verification failed; retrying once without certificate verification")
        try:
            urllib3.disable_warnings(InsecureRequestWarning)
            response = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
            response.raise_for_status()
            data = response.json()
            return {
                "ok": True,
                "source": "official",
                "data": data,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                "ssl_fallback": True,
            }
        except Timeout as retry_exc:
            return _error("timeout", str(retry_exc), start)
        except HTTPError as retry_exc:
            return _error("http", str(retry_exc), start, retryable=False)
        except ValueError as retry_exc:
            return _error("parse", str(retry_exc), start, retryable=False)
        except Exception as retry_exc:
            return _error("ssl", str(retry_exc), start)
    except Timeout as exc:
        return _error("timeout", str(exc), start)
    except HTTPError as exc:
        return _error("http", str(exc), start, retryable=False)
    except ValueError as exc:
        return _error("parse", str(exc), start, retryable=False)
    except Exception as exc:
        return _error("metadata", str(exc), start)
