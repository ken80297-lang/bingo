import os
import time
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3
DEFAULT_FAILURE_COOLDOWN_SECONDS = 300
_FAILURE_UNTIL = 0.0
_FAILURE_MESSAGE = None


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
