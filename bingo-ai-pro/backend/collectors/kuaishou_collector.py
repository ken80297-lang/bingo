from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

KUAISHOU_URL = "https://bingo2.kuaishou1688.com"
KUAISHOU_API_URL = "https://bingo2.kuaishou1688.com/api/get_data"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)

logger = logging.getLogger(__name__)


def _empty_snapshot(raw_html: str = "", parsed_json: dict | None = None) -> dict:
    return {
        "source": "kuaishou",
        "issue": None,
        "draw_time": None,
        "numbers": [],
        "recommendations": {},
        "raw_html": raw_html,
        "parsed_json": parsed_json or {},
    }


def _extract_json_objects(html: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    soup = BeautifulSoup(html, "html.parser")

    for index, script in enumerate(soup.find_all("script")):
        text = script.string or script.get_text("", strip=True)
        if not text:
            continue

        for pattern in [
            r"window\.__INITIAL_STATE__\s*=\s*({.*?})\s*;",
            r"window\.__NUXT__\s*=\s*({.*?})\s*;",
        ]:
            match = re.search(pattern, text, re.S)
            if not match:
                continue
            try:
                parsed[f"script_{index}"] = json.loads(match.group(1))
            except Exception:
                parsed[f"script_{index}_raw"] = match.group(1)[:5000]

    return parsed


def _parse_snapshot(html: str, parsed_json: dict) -> dict:
    snapshot = _empty_snapshot(html, parsed_json)
    api_data = parsed_json.get("api_get_data", {})
    latest = (api_data.get("data") or [{}])[0] if isinstance(api_data, dict) else {}

    if latest:
        numbers = latest.get("\u4e00\u822c\u734e\u865f") or []
        snapshot["issue"] = str(latest.get("\u671f\u6578")) if latest.get("\u671f\u6578") else None
        snapshot["draw_time"] = latest.get("\u958b\u734e\u6642\u9593")
        snapshot["numbers"] = [int(number) for number in numbers if str(number).isdigit()]
        snapshot["recommendations"] = {}
        return snapshot

    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)

    issue_match = re.search(r"(?:期號|期号|issue)[^\d]*(\d{6,})", text, re.I)
    if issue_match:
        snapshot["issue"] = issue_match.group(1)

    time_match = re.search(r"(\d{2}:\d{2}(?::\d{2})?)", text)
    if time_match:
        snapshot["draw_time"] = time_match.group(1)

    numbers = []
    for value in re.findall(r"\b([0-7]?\d|80)\b", text):
        number = int(value)
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
        if len(numbers) == 20:
            break

    snapshot["numbers"] = numbers if len(numbers) == 20 else []
    snapshot["recommendations"] = {}
    return snapshot


def _fetch_data_api() -> dict:
    try:
        response = requests.post(
            KUAISHOU_API_URL,
            json={"count": 1},
            timeout=15,
            headers={
                "User-Agent": USER_AGENT,
                "origin": KUAISHOU_URL,
                "referer": f"{KUAISHOU_URL}/",
            },
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.exception("failed to fetch kuaishou data api")
        return {"error": str(exc)}


def fetch_kuaishou_snapshot() -> dict:
    try:
        response = requests.get(
            KUAISHOU_URL,
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        raw_html = response.text
    except Exception as exc:
        logger.exception("failed to fetch kuaishou snapshot")
        snapshot = _empty_snapshot()
        snapshot["parsed_json"] = {"error": str(exc)}
        return snapshot

    parsed_json = _extract_json_objects(raw_html)
    parsed_json["api_get_data"] = _fetch_data_api()
    try:
        return _parse_snapshot(raw_html, parsed_json)
    except Exception as exc:
        logger.exception("failed to parse kuaishou snapshot")
        snapshot = _empty_snapshot(raw_html, parsed_json)
        snapshot["parsed_json"]["parse_error"] = str(exc)
        return snapshot
