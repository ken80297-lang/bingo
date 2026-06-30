from __future__ import annotations

import re
from dataclasses import dataclass

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PILIO_HISTORY_URL = "https://www.pilio.idv.tw/bingo/list_history.asp"


@dataclass(frozen=True)
class HistoryDraw:
    issue: str
    time_text: str
    numbers: list[int]
    super_number: int | None = None
    big_small: str | None = None
    odd_even: str | None = None


def _fetch_html() -> str:
    response = requests.get(
        PILIO_HISTORY_URL,
        timeout=20,
        verify=False,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        },
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text


def fetch_history() -> list[HistoryDraw]:
    html = _fetch_html()
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)

    pattern = re.compile(
        r"超級獎號:(?P<super>\d{2}|－).*?"
        r"猜大小:(?P<bs>大|小|－).*?"
        r"猜單雙:(?P<oe>單|雙|－).*?"
        r"\((?P<time>\d{2}:\d{2})\).*?"
        r"期別:\s*(?P<issue>\d+).*?"
        r"(?P<num>(?:\d{2}[,，\s]+){19}\d{2})",
        re.S,
    )

    results: list[HistoryDraw] = []

    for match in pattern.finditer(text):
        numbers = [int(x) for x in re.findall(r"\d{2}", match.group("num"))[:20]]

        if len(numbers) != 20:
            continue

        super_raw = match.group("super")

        results.append(
            HistoryDraw(
                issue=match.group("issue"),
                time_text=match.group("time"),
                numbers=numbers,
                super_number=None if super_raw == "－" else int(super_raw),
                big_small=None if match.group("bs") == "－" else match.group("bs"),
                odd_even=None if match.group("oe") == "－" else match.group("oe"),
            )
        )

    return results