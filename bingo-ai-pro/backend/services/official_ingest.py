from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.official_draw_store import (
    get_latest_official_draw_summary,
    get_official_draw_by_issue,
    save_official_draws,
)
from services.collector_runtime import mark_error, mark_success, official_collection_lock

logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))
DEFAULT_PAGE_SIZE = 10
MAX_POLL_RETRY_MINUTES_AFTER_DRAW = int(os.getenv("LIGHTWEIGHT_OFFICIAL_MAX_RETRY_MINUTES", "3"))
POLL_FIRST_RETRY_SECONDS_AFTER_DRAW = int(os.getenv("LIGHTWEIGHT_OFFICIAL_FIRST_RETRY_SECONDS", "60"))
AI_LIFECYCLE_SUBPROCESS_ENABLED = os.getenv("AI_LIFECYCLE_SUBPROCESS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
ROOT = Path(__file__).resolve().parents[1]
_AI_WORKER_LOCK = threading.Lock()

def _launch_ai_lifecycle_subprocess(issue: str) -> None:
    if not AI_LIFECYCLE_SUBPROCESS_ENABLED:
        return
    if not _AI_WORKER_LOCK.acquire(blocking=False):
        print(f"AI_LIFECYCLE_SUBPROCESS_SKIPPED issue={issue} reason=worker_busy", flush=True)
        return

    def _run() -> None:
        try:
            env = os.environ.copy()
            env["AI_LIFECYCLE_ISSUE"] = issue
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "ai_lifecycle_worker_once.py")],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            print(
                "AI_LIFECYCLE_SUBPROCESS "
                f"issue={issue} returncode={completed.returncode} "
                f"stdout={completed.stdout[-4000:]!r} stderr={completed.stderr[-2000:]!r}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"AI_LIFECYCLE_SUBPROCESS_ERROR issue={issue} "
                f"error_type={type(exc).__name__} error={exc}",
                flush=True,
            )
        finally:
            _AI_WORKER_LOCK.release()

    threading.Thread(target=_run, name=f"ai-lifecycle-{issue}", daemon=True).start()
    print(f"AI_LIFECYCLE_SUBPROCESS_STARTED issue={issue}", flush=True)

_POLL_STATE_LOCK = threading.RLock()
_POLL_COMPLETED_ISSUES: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        pass
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            psapi = ctypes.WinDLL("psapi.dll")
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            kernel32 = ctypes.WinDLL("kernel32.dll")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return round(float(counters.WorkingSetSize) / (1024 * 1024), 2)
        except Exception:
            return None
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if rss > 10_000_000:
            return round(rss / (1024 * 1024), 2)
        return round(rss / 1024, 2)
    except Exception:
        return None


def _issue_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _valid_issue(value: Any) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and len(text) >= 6 and not text.startswith("99")


def _valid_numbers(values: Any) -> list[int]:
    try:
        numbers = [int(value) for value in list(values or [])]
    except Exception:
        return []
    if len(numbers) != 20 or len(set(numbers)) != 20:
        return []
    if any(number < 1 or number > 80 for number in numbers):
        return []
    return numbers


def validate_official_draw(draw: dict | None) -> tuple[bool, str | None, dict | None]:
    if not draw:
        return False, "missing_draw", None
    issue = str(draw.get("issue") or "").strip()
    if not _valid_issue(issue):
        return False, "invalid_issue", None
    numbers = _valid_numbers(draw.get("numbers"))
    if len(numbers) != 20:
        return False, "invalid_numbers", None
    try:
        super_number = int(draw.get("super_number"))
    except Exception:
        return False, "invalid_super_number", None
    if super_number not in numbers:
        return False, "invalid_super_number", None
    open_order_numbers = _valid_numbers(draw.get("open_order_numbers")) or numbers
    sanitized = {
        **draw,
        "issue": issue,
        "numbers": numbers,
        "open_order_numbers": open_order_numbers,
        "super_number": super_number,
        "source": draw.get("source") or "taiwan_lottery",
        "verification_status": "validated",
        "fetched_at": draw.get("fetched_at") or _now(),
        "verified": bool(draw.get("verified", False)),
    }
    return True, None, sanitized


def _latest_source_draw(page_size: int = DEFAULT_PAGE_SIZE) -> tuple[dict | None, list[dict]]:
    draws = fetch_official_bingo_results(_today_taipei(), page_num=1, page_size=page_size)
    valid: list[dict] = []
    for draw in draws:
        ok, _reason, sanitized = validate_official_draw(draw)
        if ok and sanitized:
            valid.append(sanitized)
    if not valid:
        return None, draws
    return max(valid, key=lambda item: _issue_int(item.get("issue")) or 0), draws


def ingest_latest_official_once(*, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    start = time.perf_counter()
    rss_before = _rss_mb()
    rss_peak = rss_before
    timings: dict[str, float] = {}

    def mark_rss() -> float | None:
        nonlocal rss_peak
        rss = _rss_mb()
        if rss is not None:
            rss_peak = rss if rss_peak is None else max(rss_peak, rss)
        return rss

    try:
        stage = time.perf_counter()
        source_draw, raw_draws = _latest_source_draw(page_size=page_size)
        timings["source_fetch_ms"] = round((time.perf_counter() - stage) * 1000, 2)
        mark_rss()
        if not source_draw:
            result = {
                "status": "error",
                "stage": "source_fetch",
                "reason": "no_valid_complete_official_draw",
                "raw_count": len(raw_draws),
                "saved": {"status": "skipped", "reason": "source_empty"},
            }
            return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())

        source_issue = str(source_draw["issue"])
        source_issue_int = _issue_int(source_issue)

        stage = time.perf_counter()
        latest_db = get_latest_official_draw_summary()
        timings["db_lookup_ms"] = round((time.perf_counter() - stage) * 1000, 2)
        mark_rss()
        latest_db_issue = str((latest_db or {}).get("issue") or "").strip() or None
        latest_db_issue_int = _issue_int(latest_db_issue)
        if latest_db_issue_int is not None and source_issue_int is not None and latest_db_issue_int >= source_issue_int:
            result = {
                "status": "noop",
                "exit_reason": "database_same_or_newer",
                "source_issue": source_issue,
                "database_latest_issue": latest_db_issue,
                "saved": {"status": "ok", "saved": 0, "storage": "existing"},
            }
            return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())

        stage = time.perf_counter()
        existing = get_official_draw_by_issue(source_issue)
        timings["existing_lookup_ms"] = round((time.perf_counter() - stage) * 1000, 2)
        mark_rss()
        if existing:
            result = {
                "status": "noop",
                "exit_reason": "issue_already_exists",
                "source_issue": source_issue,
                "database_latest_issue": latest_db_issue,
                "saved": {"status": "ok", "saved": 0, "storage": "existing"},
            }
            return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())

        stage = time.perf_counter()
        save_result = save_official_draws([source_draw])
        timings["save_ms"] = round((time.perf_counter() - stage) * 1000, 2)
        mark_rss()
        if save_result.get("status") != "ok" or int(save_result.get("saved") or 0) != 1:
            result = {
                "status": "error",
                "stage": "database_save",
                "reason": str(save_result.get("error") or save_result),
                "source_issue": source_issue,
                "database_latest_issue": latest_db_issue,
                "saved": save_result,
            }
            return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())

        result = {
            "status": "ok",
            "exit_reason": "saved",
            "source_issue": source_issue,
            "database_latest_issue": source_issue,
            "saved": save_result,
            "downstream": {"status": "deferred", "reason": "lightweight_official_ingest"},
        }
        return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())
    except Exception as exc:
        logger.exception("lightweight official ingest failed")
        result = {"status": "error", "stage": "exception", "reason": str(exc), "error_type": type(exc).__name__}
        return _finish_result(result, start, timings, rss_before, rss_peak, mark_rss())


def _finish_result(
    result: dict[str, Any],
    start: float,
    timings: dict[str, float],
    rss_before: float | None,
    rss_peak: float | None,
    rss_after: float | None,
) -> dict[str, Any]:
    timings.setdefault("source_fetch_ms", 0.0)
    timings.setdefault("db_lookup_ms", 0.0)
    timings.setdefault("save_ms", 0.0)
    timings["total_ms"] = round((time.perf_counter() - start) * 1000, 2)
    result["timing"] = timings
    result["source_fetch_ms"] = timings["source_fetch_ms"]
    result["db_lookup_ms"] = timings["db_lookup_ms"]
    result["save_ms"] = timings["save_ms"]
    result["total_ms"] = timings["total_ms"]
    result["rss_mb_before"] = rss_before
    result["rss_mb_peak"] = rss_peak
    result["rss_mb_after"] = rss_after
    return result


def collect_latest_official_lightweight() -> dict[str, Any]:
    start = time.perf_counter()
    with official_collection_lock("official_lightweight_ingest") as (locked, lock_payload):
        if not locked:
            return {
                "status": "skipped_due_to_lock",
                "exit_reason": "skipped_due_to_lock",
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                **lock_payload,
            }
        result = ingest_latest_official_once()
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if result.get("status") in {"ok", "noop"}:
            mark_success("official_collector", duration_ms, exit_reason=result.get("exit_reason") or result.get("status"))
        else:
            mark_error("official_collector", result.get("reason") or result.get("status"), duration_ms)
        logger.info(
            "lightweight_official_ingest_finished status=%s source_issue=%s db_issue=%s source_fetch_ms=%s db_lookup_ms=%s save_ms=%s total_ms=%s rss_mb_before=%s rss_mb_peak=%s rss_mb_after=%s",
            result.get("status"),
            result.get("source_issue"),
            result.get("database_latest_issue"),
            result.get("source_fetch_ms"),
            result.get("db_lookup_ms"),
            result.get("save_ms"),
            result.get("total_ms"),
            result.get("rss_mb_before"),
            result.get("rss_mb_peak"),
            result.get("rss_mb_after"),
        )
        # Uvicorn does not configure the root INFO logger. Keep the acceptance
        # record visible in Render stdout without enabling noisy global logs.
        print("LIGHTWEIGHT_OFFICIAL_INGEST " + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)
        if result.get("status") == "ok" and result.get("exit_reason") == "saved" and result.get("source_issue"):
            _launch_ai_lifecycle_subprocess(str(result["source_issue"]))
        return result


def _expected_issue_for_draw_window(now: datetime) -> str:
    local = now.astimezone(TAIPEI_TZ)
    minutes_since_midnight = local.hour * 60 + local.minute
    draw_index = max(0, (minutes_since_midnight - (7 * 60 + 5)) // 5)
    return f"{local.date().isoformat()}:{draw_index}"


def should_poll_official_now(now: datetime | None = None) -> tuple[bool, str]:
    local = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    minutes_since_midnight = local.hour * 60 + local.minute
    first_draw = 7 * 60 + 5
    last_draw = 23 * 60 + 55
    seconds_after_draw = ((minutes_since_midnight - first_draw) % 5) * 60 + local.second
    if minutes_since_midnight < first_draw or minutes_since_midnight > last_draw + MAX_POLL_RETRY_MINUTES_AFTER_DRAW:
        return False, "outside_draw_hours"
    if seconds_after_draw < POLL_FIRST_RETRY_SECONDS_AFTER_DRAW:
        return False, "before_first_retry"
    if seconds_after_draw >= (MAX_POLL_RETRY_MINUTES_AFTER_DRAW + 1) * 60:
        return False, "retry_window_elapsed"
    key = _expected_issue_for_draw_window(local)
    with _POLL_STATE_LOCK:
        if key in _POLL_COMPLETED_ISSUES:
            return False, "issue_window_already_completed"
    return True, key


def run_lightweight_official_polling_tick(now: datetime | None = None) -> dict[str, Any]:
    should_poll, reason = should_poll_official_now(now)
    if not should_poll:
        return {"status": "skipped", "reason": reason}
    result = collect_latest_official_lightweight()
    if result.get("status") == "ok" and result.get("source_issue"):
        with _POLL_STATE_LOCK:
            _POLL_COMPLETED_ISSUES.add(reason)
            if len(_POLL_COMPLETED_ISSUES) > 256:
                for key in sorted(_POLL_COMPLETED_ISSUES)[:128]:
                    _POLL_COMPLETED_ISSUES.discard(key)
    result["poll_window"] = reason
    return result
