import os
import threading
import time
from contextlib import contextmanager

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_FAILURE_COOLDOWN_SECONDS = 300
DASHBOARD_READ_POOL_MIN_SIZE = 0
DASHBOARD_READ_POOL_MAX_SIZE = 2
DASHBOARD_READ_POOL_ACQUIRE_TIMEOUT_SECONDS = 1.0
_FAILURE_UNTIL = 0.0
_FAILURE_MESSAGE = None
_DASHBOARD_READ_POOL = None
_DASHBOARD_READ_POOL_LOCK = threading.Lock()


def _connect_timeout_seconds() -> int:
    raw = os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", str(DEFAULT_CONNECT_TIMEOUT_SECONDS))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    return max(1, min(value, 30))


def _failure_cooldown_seconds() -> int:
    raw = os.getenv("DATABASE_FAILURE_COOLDOWN_SECONDS", str(DEFAULT_FAILURE_COOLDOWN_SECONDS))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_FAILURE_COOLDOWN_SECONDS
    return max(0, min(value, 3600))


def get_connection():
    global _FAILURE_MESSAGE, _FAILURE_UNTIL
    now = time.monotonic()
    if _FAILURE_UNTIL > now:
        raise RuntimeError(f"Postgres connection temporarily unavailable: {_FAILURE_MESSAGE}")
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=_connect_timeout_seconds())
    except Exception as exc:
        cooldown = _failure_cooldown_seconds()
        if cooldown > 0:
            _FAILURE_UNTIL = now + cooldown
            _FAILURE_MESSAGE = str(exc)
        raise


def _create_dashboard_read_pool():
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(
        conninfo=DATABASE_URL or "",
        kwargs={"connect_timeout": _connect_timeout_seconds()},
        min_size=DASHBOARD_READ_POOL_MIN_SIZE,
        max_size=DASHBOARD_READ_POOL_MAX_SIZE,
        open=False,
        timeout=DASHBOARD_READ_POOL_ACQUIRE_TIMEOUT_SECONDS,
        name="dashboard-read",
    )
    pool.open(wait=False)
    return pool


def _get_dashboard_read_pool():
    global _DASHBOARD_READ_POOL
    with _DASHBOARD_READ_POOL_LOCK:
        if _DASHBOARD_READ_POOL is None or getattr(_DASHBOARD_READ_POOL, "closed", False):
            _DASHBOARD_READ_POOL = _create_dashboard_read_pool()
        return _DASHBOARD_READ_POOL


@contextmanager
def dashboard_read_connection():
    pool = _get_dashboard_read_pool()
    with pool.connection(timeout=DASHBOARD_READ_POOL_ACQUIRE_TIMEOUT_SECONDS) as conn:
        yield conn


def close_dashboard_read_pool() -> None:
    global _DASHBOARD_READ_POOL
    with _DASHBOARD_READ_POOL_LOCK:
        pool = _DASHBOARD_READ_POOL
        _DASHBOARD_READ_POOL = None
    if pool is not None:
        pool.close(timeout=1.0)
