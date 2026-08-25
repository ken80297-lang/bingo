import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3


def _connect_timeout_seconds() -> int:
    raw = os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", str(DEFAULT_CONNECT_TIMEOUT_SECONDS))
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CONNECT_TIMEOUT_SECONDS
    return max(1, min(value, 30))


def get_connection():
    return psycopg.connect(DATABASE_URL, connect_timeout=_connect_timeout_seconds())
