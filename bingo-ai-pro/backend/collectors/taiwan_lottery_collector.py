from __future__ import annotations

import logging
from datetime import date
from typing import Any

import requests
import urllib3
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning

logger = logging.getLogger(__name__)

OFFICIAL_BINGO_URL = "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/BingoResult"


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
        if number is not None:
            numbers.append(number)
    return numbers


def _draw_date(open_date: str, d_date: str | None) -> str:
    if not d_date or str(d_date).startswith("0001-01-01"):
        return open_date
    return str(d_date).split("T", 1)[0]


def fetch_official_bingo_results(
    open_date: str | date,
    page_num: int = 1,
    page_size: int = 10,
) -> list[dict]:
    query_date = open_date.isoformat() if isinstance(open_date, date) else str(open_date)
    page_num = max(1, int(page_num or 1))
    page_size = max(1, min(int(page_size or 10), 100))
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
        try:
            response = requests.get(
                OFFICIAL_BINGO_URL,
                params=params,
                headers=headers,
                timeout=15,
            )
        except SSLError:
            logger.warning("official bingo api ssl verification failed; retrying without certificate verification")
            urllib3.disable_warnings(InsecureRequestWarning)
            response = requests.get(
                OFFICIAL_BINGO_URL,
                params=params,
                headers=headers,
                timeout=15,
                verify=False,
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("rtCode") != 0:
            logger.error("official bingo api returned error: %s", payload)
            return []

        rows = ((payload.get("content") or {}).get("bingoQueryResult") or [])
        draws = []
        for row in rows:
            issue = row.get("drawTerm")
            numbers = _as_numbers(row.get("bigShowOrder"))
            open_order_numbers = _as_numbers(row.get("openShowOrder"))
            if not issue or len(numbers) != 20:
                continue
            draws.append(
                {
                    "issue": str(issue),
                    "draw_date": _draw_date(query_date, row.get("dDate")),
                    "draw_time": None,
                    "numbers": numbers,
                    "open_order_numbers": open_order_numbers,
                    "super_number": _as_int(row.get("bullEyeTop")),
                    "win_no_only": bool(row.get("winNoOnly")),
                    "source": "taiwan_lottery",
                    "verified": False,
                    "raw_json": row,
                }
            )
        return draws
    except Exception:
        logger.exception("official bingo api fetch failed")
        return []
