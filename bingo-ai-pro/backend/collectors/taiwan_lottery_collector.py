from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.http_client import safe_get_json

logger = logging.getLogger(__name__)

OFFICIAL_BINGO_URL = "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/BingoResult"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
_LAST_FETCH_DIAGNOSTICS: list[dict] = []


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if 1 <= number <= 80 else None


def _as_numbers(values: Any) -> list[int]:
    numbers = []
    for value in values or []:
        number = _as_int(value)
        if number is not None and number not in numbers:
            numbers.append(number)
    return numbers


def _remember_diagnostic(payload: dict) -> None:
    _LAST_FETCH_DIAGNOSTICS.append(payload)
    del _LAST_FETCH_DIAGNOSTICS[:-20]


def get_last_official_fetch_diagnostics() -> list[dict]:
    return list(_LAST_FETCH_DIAGNOSTICS)


def _rt_code_ok(value: Any) -> bool:
    text = str(value).strip()
    return text in {"0", "00", "000", "0000"}


def _draw_date(open_date: str, d_date: str | None) -> str:
    if not d_date or str(d_date).startswith("0001-01-01"):
        return open_date
    return str(d_date).split("T", 1)[0]


def _parse_draw_time(value: Any) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, "missing"
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(), None
        except Exception:
            return None, "invalid_unix_timestamp"

    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return None, "placeholder_datetime"
    normalized = text.replace("/", "-").replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None, "invalid_datetime_format"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(timezone.utc).isoformat(), None


def _record_draw_time_parse_failure(issue: Any, raw_value: Any, reason: str | None) -> None:
    if not reason or reason in {"missing", "placeholder_datetime"}:
        return
    try:
        from services.operations_center import record_operation_event

        record_operation_event(
            component="official_collector",
            event_type="official_draw_time_parse_failed",
            status="warning",
            issue=str(issue) if issue else None,
            message=(
                f"official draw_time parse failed "
                f"raw_value_type={type(raw_value).__name__} reason={reason}"
            ),
            error_type=reason,
        )
    except Exception:
        logger.exception("official draw_time parse failure event failed")


def fetch_official_bingo_results(
    open_date: str | date,
    page_num: int = 1,
    page_size: int = 10,
) -> list[dict]:
    query_date = open_date.isoformat() if isinstance(open_date, date) else str(open_date)
    page_num = max(1, int(page_num or 1))
    page_size = max(1, min(int(page_size or 10), 100))
    diagnostic = {
        "open_date": query_date,
        "page_num": page_num,
        "page_size": page_size,
        "ok": False,
        "parsed_count": 0,
    }
    try:
        params = {
            "openDate": query_date,
            "pageNum": page_num,
            "pageSize": page_size,
        }
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.taiwanlottery.com",
            "Referer": "https://www.taiwanlottery.com/lotto/result/bingo_bingo",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
        }
        result = safe_get_json(OFFICIAL_BINGO_URL, params=params, headers=headers)
        diagnostic.update(
            {
                "http_ok": result.get("ok"),
                "error_type": result.get("error_type"),
                "message": result.get("message"),
                "elapsed_ms": result.get("elapsed_ms"),
                "ssl_fallback": result.get("ssl_fallback"),
                "attempts": result.get("attempts"),
            }
        )
        if not result.get("ok"):
            logger.warning("official bingo api fetch skipped: %s", result)
            _remember_diagnostic(diagnostic)
            return []
        payload = result.get("data") or {}
        diagnostic["rt_code"] = payload.get("rtCode")
        if not _rt_code_ok(payload.get("rtCode")):
            logger.error("official bingo api returned error: %s", payload)
            diagnostic["message"] = payload.get("rtMsg") or "rt_code_error"
            _remember_diagnostic(diagnostic)
            return []

        rows = ((payload.get("content") or {}).get("bingoQueryResult") or [])
        diagnostic["row_count"] = len(rows)
        diagnostic["total_size"] = (payload.get("content") or {}).get("totalSize")
        draws = []
        for row in rows:
            issue = row.get("drawTerm")
            numbers = _as_numbers(row.get("bigShowOrder"))
            open_order_numbers = _as_numbers(row.get("openShowOrder"))
            if not issue or len(numbers) != 20 or len(set(numbers)) != 20:
                continue
            draw_time, parse_reason = _parse_draw_time(row.get("dDate"))
            _record_draw_time_parse_failure(issue, row.get("dDate"), parse_reason)
            draws.append(
                {
                    "issue": str(issue),
                    "draw_date": _draw_date(query_date, row.get("dDate")),
                    "draw_time": draw_time,
                    "numbers": numbers,
                    "open_order_numbers": open_order_numbers,
                    "super_number": _as_int(row.get("bullEyeTop")),
                    "win_no_only": bool(row.get("winNoOnly")),
                    "source": "taiwan_lottery",
                    "verified": False,
                    "raw_json": row,
                }
            )
        diagnostic["ok"] = True
        diagnostic["parsed_count"] = len(draws)
        _remember_diagnostic(diagnostic)
        return draws
    except Exception as exc:
        diagnostic["error_type"] = exc.__class__.__name__
        diagnostic["message"] = str(exc)
        _remember_diagnostic(diagnostic)
        logger.exception("official bingo api fetch failed")
        return []
