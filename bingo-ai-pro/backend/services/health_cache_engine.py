from __future__ import annotations

import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from database.system_health_store import get_health_cache, upsert_health_cache
from services.system_health import build_system_health

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
HEALTH_CACHE_FILE = ROOT / "health_cache.json"
HEALTH_CACHE_KEY = "system-health"
LIVE_SECONDS = 5 * 60
RECENT_SECONDS = 10 * 60

_CACHE: dict[str, Any] | None = None
_REFRESH_LOCK = Lock()
_LAST_REFRESH_ATTEMPT: str | None = None
_LAST_REFRESH_ERROR: str | None = None
_LAST_REFRESH_DURATION_MS: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _age_seconds(payload: dict) -> float | None:
    checked = _parse_time(payload.get("last_checked") or payload.get("generated_at"))
    if not checked:
        return None
    return max(0.0, (_now() - checked).total_seconds())


def _cache_state(age: float | None) -> tuple[str, bool]:
    if age is None:
        return "unavailable", True
    if age <= LIVE_SECONDS:
        return "live", False
    if age <= RECENT_SECONDS:
        return "recent", False
    return "stale", True


def _health_status(status: str | None) -> str:
    if status == "ok":
        return "healthy"
    if status == "warning":
        return "warning"
    if status == "error":
        return "error"
    return "unknown"


def _with_cache_metadata(payload: dict, source: str, cached: bool = True) -> dict:
    result = deepcopy(payload)
    age = _age_seconds(result)
    cache_state, stale = _cache_state(age)
    status = result.get("status")
    result.setdefault("health_status", _health_status(status))
    result.setdefault("generated_at", result.get("last_checked") or _now_iso())
    result.setdefault("last_checked", result.get("generated_at"))
    result["cache_age_seconds"] = round(age, 3) if age is not None else None
    result["cache_age_minutes"] = round((age or 0) / 60, 3) if age is not None else None
    result["cache_state"] = cache_state
    result["cached"] = cached
    result["source"] = source
    result["stale"] = stale
    result.setdefault("components", {})
    result.setdefault("summary", {})
    result.setdefault("error", None)
    result["last_refresh_attempt"] = result.get("last_refresh_attempt") or _LAST_REFRESH_ATTEMPT
    result["last_refresh_error"] = result.get("last_refresh_error") or _LAST_REFRESH_ERROR
    result["refresh_duration_ms"] = result.get("refresh_duration_ms") or _LAST_REFRESH_DURATION_MS
    return result


def _store_memory(payload: dict) -> dict:
    global _CACHE
    _CACHE = deepcopy(payload)
    return deepcopy(payload)


def load_cache_from_file() -> dict | None:
    try:
        if not HEALTH_CACHE_FILE.exists():
            return None
        with HEALTH_CACHE_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:
        logger.exception("failed to load health cache file")
        return None


def save_cache_to_file(payload: dict) -> dict:
    tmp_path = HEALTH_CACHE_FILE.with_suffix(".json.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, HEALTH_CACHE_FILE)
        return {"status": "ok", "storage": "file", "path": str(HEALTH_CACHE_FILE)}
    except Exception as exc:
        logger.exception("failed to save health cache file")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            logger.exception("failed to remove incomplete health cache temp file")
        return {"status": "error", "storage": "file", "error": str(exc)}


def load_cache_from_database() -> dict | None:
    try:
        return get_health_cache(HEALTH_CACHE_KEY)
    except Exception:
        logger.exception("failed to load health cache from database")
        return None


def save_cache_to_database(payload: dict) -> dict:
    try:
        return upsert_health_cache(HEALTH_CACHE_KEY, payload)
    except Exception as exc:
        logger.exception("failed to save health cache to database")
        return {"status": "error", "storage": None, "error": str(exc)}


def build_fallback_health() -> dict:
    now = _now_iso()
    return {
        "status": "unknown",
        "health_status": "unknown",
        "generated_at": now,
        "last_checked": now,
        "cache_age_seconds": None,
        "cache_age_minutes": None,
        "cache_state": "unavailable",
        "cached": True,
        "source": "fallback",
        "stale": True,
        "components": {},
        "summary": {},
        "error": "health cache unavailable",
        "collector": {},
        "simulation": {},
        "recommendation": {},
        "prediction": {},
        "evolution": {},
        "official_verification": {},
        "pipeline": {"status": "unknown", "delay_seconds": {}},
    }


def get_cached_health() -> dict:
    if isinstance(_CACHE, dict):
        return _with_cache_metadata(_CACHE, "memory", cached=True)

    database_payload = load_cache_from_database()
    if isinstance(database_payload, dict):
        _store_memory(database_payload)
        return _with_cache_metadata(database_payload, "database", cached=True)

    file_payload = load_cache_from_file()
    if isinstance(file_payload, dict):
        _store_memory(file_payload)
        return _with_cache_metadata(file_payload, "file", cached=True)

    return build_fallback_health()


def get_cache_metadata() -> dict:
    payload = get_cached_health()
    return {
        "source": payload.get("source"),
        "cache_state": payload.get("cache_state"),
        "cache_age_seconds": payload.get("cache_age_seconds"),
        "stale": payload.get("stale"),
        "last_checked": payload.get("last_checked"),
        "last_refresh_attempt": payload.get("last_refresh_attempt"),
        "last_refresh_error": payload.get("last_refresh_error"),
        "refresh_duration_ms": payload.get("refresh_duration_ms"),
    }


def is_cache_stale(payload: dict | None = None) -> bool:
    current = payload if payload is not None else get_cached_health()
    return bool(current.get("stale"))


def calculate_health_snapshot() -> dict:
    snapshot = build_system_health(save=False, use_cache=False)
    now = _now_iso()
    snapshot["generated_at"] = now
    snapshot["last_checked"] = now
    snapshot["health_status"] = _health_status(snapshot.get("status"))
    snapshot["components"] = {
        "collector": snapshot.get("collector", {}),
        "pipeline": snapshot.get("pipeline", {}),
        "learning": snapshot.get("learning", {}),
        "operations": snapshot.get("operations", {}),
        "database": snapshot.get("database", {}),
        "scheduler": snapshot.get("scheduler", {}),
    }
    snapshot["summary"] = {
        "latest_issue": snapshot.get("latest_issue"),
        "pipeline_status": (snapshot.get("pipeline") or {}).get("status"),
        "official_status": (snapshot.get("official_verification") or {}).get("status"),
    }
    snapshot.setdefault("error", None)
    return snapshot


def refresh_health_cache() -> dict:
    global _LAST_REFRESH_ATTEMPT, _LAST_REFRESH_ERROR, _LAST_REFRESH_DURATION_MS
    if not _REFRESH_LOCK.acquire(blocking=False):
        return {
            "status": "skipped",
            "refresh_skipped": True,
            "refresh_skip_reason": "refresh already running",
            "cache": get_cache_metadata(),
        }

    started = time.perf_counter()
    _LAST_REFRESH_ATTEMPT = _now_iso()
    try:
        payload = calculate_health_snapshot()
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        payload["last_refresh_attempt"] = _LAST_REFRESH_ATTEMPT
        payload["last_refresh_success"] = _now_iso()
        payload["last_refresh_error"] = None
        payload["refresh_duration_ms"] = duration_ms
        payload = _with_cache_metadata(payload, "memory", cached=False)
        _store_memory(payload)
        database_result = save_cache_to_database(payload)
        file_result = save_cache_to_file(payload)
        _LAST_REFRESH_ERROR = None
        _LAST_REFRESH_DURATION_MS = duration_ms
        return {
            "status": "ok",
            "refresh_success": True,
            "refresh_duration_ms": duration_ms,
            "database": database_result,
            "file": file_result,
            "cache": get_cache_metadata(),
        }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        _LAST_REFRESH_ERROR = str(exc)
        _LAST_REFRESH_DURATION_MS = duration_ms
        logger.exception("health cache refresh failed")
        fallback = get_cached_health()
        fallback["health_refresh_failed"] = True
        fallback["last_refresh_attempt"] = _LAST_REFRESH_ATTEMPT
        fallback["last_refresh_error"] = str(exc)
        fallback["refresh_duration_ms"] = duration_ms
        _store_memory(fallback)
        return {
            "status": "error",
            "refresh_success": False,
            "refresh_duration_ms": duration_ms,
            "error": str(exc),
            "cache": get_cache_metadata(),
        }
    finally:
        _REFRESH_LOCK.release()


def warm_health_cache() -> dict:
    payload = get_cached_health()
    if payload.get("source") != "fallback":
        return {"status": "ok", "source": payload.get("source"), "cache": get_cache_metadata()}
    return {"status": "empty", "source": "fallback", "cache": payload}
