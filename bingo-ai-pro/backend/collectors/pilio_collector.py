from __future__ import annotations

import logging
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

PILIO_HISTORY_URL = "https://www.pilio.idv.tw/bingo/list_history.asp"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
)

logger = logging.getLogger(__name__)


def _normalize_date(target_date: date | str | None) -> str:
    if target_date is None:
        return date.today().strftime("%Y/%m/%d")
    if isinstance(target_date, date):
        return target_date.strftime("%Y/%m/%d")
    return datetime.strptime(target_date.replace("-", "/"), "%Y/%m/%d").strftime("%Y/%m/%d")


def _row_to_draw(row_text: str) -> dict | None:
    issue_match = re.search(r"\b(\d{6,})\b", row_text)
    if not issue_match:
        return None

    time_match = re.search(r"\b(\d{2}:\d{2}(?::\d{2})?)\b", row_text)
    number_text = row_text.replace(issue_match.group(1), " ")
    if time_match:
        number_text = number_text.replace(time_match.group(1), " ")
    numeric_tokens = [int(value) for value in re.findall(r"\b([0-7]?\d|80)\b", number_text)]
    numbers = []

    for number in numeric_tokens:
        if 1 <= number <= 80:
            numbers.append(number)
        if len(numbers) == 20:
            break

    if len(numbers) != 20:
        return None

    remaining = numeric_tokens[len(numbers):]
    super_number = next((n for n in remaining if 1 <= n <= 80), None)

    big_small = None
    if "\u5927" in row_text:
        big_small = "big"
    elif "\u5c0f" in row_text:
        big_small = "small"

    odd_even = None
    if "\u55ae" in row_text or "\u5355" in row_text:
        odd_even = "odd"
    elif "\u96d9" in row_text or "\u53cc" in row_text:
        odd_even = "even"

    return {
        "source": "pilio",
        "issue": issue_match.group(1),
        "draw_time": time_match.group(1) if time_match else None,
        "numbers": numbers,
        "super_number": super_number,
        "big_small": big_small,
        "odd_even": odd_even,
    }


def fetch_pilio_history(target_date: date | str | None = None) -> list[dict]:
    indate = _normalize_date(target_date)
    try:
        response = requests.get(
            f"{PILIO_HISTORY_URL}?indate={indate}",
            timeout=15,
            verify=False,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding
    except Exception:
        logger.exception("failed to fetch pilio history")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    draws: list[dict] = []
    seen: set[str] = set()

    for row in soup.find_all("tr"):
        row_text = row.get_text(" ", strip=True)
        draw = _row_to_draw(row_text)
        if draw and draw["issue"] not in seen:
            draws.append(draw)
            seen.add(draw["issue"])

    if draws:
        return draws

    text = soup.get_text("\n", strip=True)
    for block in re.split(r"\n{2,}", text):
        draw = _row_to_draw(block)
        if draw and draw["issue"] not in seen:
            draws.append(draw)
            seen.add(draw["issue"])

    return draws
