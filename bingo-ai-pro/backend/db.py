from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from database.cloud_draws import insert_cloud_draw

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "bingo.db"
OFFICIAL_URL = "https://www.taiwanlottery.com/lotto/result/bingo_bingo/"
FALLBACK_URL = "https://www.pilio.idv.tw/bingo/list.asp"


@dataclass(frozen=True)
class Draw:
    issue: str
    time_text: str
    numbers: list[int]
    super_number: int | None = None
    big_small: str | None = None
    odd_even: str | None = None


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS draws (
                issue TEXT PRIMARY KEY,
                time_text TEXT,
                numbers TEXT NOT NULL,
                super_number INTEGER,
                big_small TEXT,
                odd_even TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis (
                issue TEXT PRIMARY KEY,
                analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                data TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS recommend (
                issue TEXT PRIMARY KEY,
                analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                data TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS statistics (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def save_draws(draws: Iterable[Draw]) -> int:
    rows = [
        (
            draw.issue,
            draw.time_text,
            ",".join(f"{n:02d}" for n in draw.numbers),
            draw.super_number,
            draw.big_small,
            draw.odd_even,
        )
        for draw in draws
    ]

    added = 0

    with _connect() as con:
        before = con.total_changes
        con.executemany(
            """
            INSERT OR IGNORE INTO draws(issue, time_text, numbers, super_number, big_small, odd_even)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        added = con.total_changes - before

    for draw in draws:
        try:
            insert_cloud_draw(
                issue=draw.issue,
                time_text=draw.time_text,
                numbers=draw.numbers,
                super_number=draw.super_number,
                source="auto",
            )
        except Exception as e:
            print(f"Supabase 寫入失敗 {draw.issue}: {e}")

    return added


def save_analysis_result(issue: str, data: dict) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO analysis(issue, data) VALUES (?, ?)",
            (issue, json.dumps(data, ensure_ascii=False)),
        )


def save_recommendation_result(issue: str, data: dict) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO recommend(issue, data) VALUES (?, ?)",
            (issue, json.dumps(data, ensure_ascii=False)),
        )


def save_statistics(key: str, value: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO statistics(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )


def _parse_pilio(html: str) -> list[Draw]:
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    pattern = re.compile(
        r"超級獎號:(?P<super>\d{2}|－).*?猜大小:(?P<bs>大|小|－).*?猜單雙:(?P<oe>單|雙|－).*?\((?P<time>\d{2}:\d{2})\).*?期別:\s*(?P<issue>\d+).*?(?P<num>(?:\d{2}[,，\s]+){19}\d{2})",
        re.S,
    )
    results: list[Draw] = []
    for match in pattern.finditer(text):
        numbers = [int(x) for x in re.findall(r"\d{2}", match.group("num"))[:20]]
        super_raw = match.group("super")
        results.append(
            Draw(
                issue=match.group("issue"),
                time_text=match.group("time"),
                numbers=numbers,
                super_number=None if super_raw == "－" else int(super_raw),
                big_small=None if match.group("bs") == "－" else match.group("bs"),
                odd_even=None if match.group("oe") == "－" else match.group("oe"),
            )
        )
    return results


def _fetch_html(url: str) -> str:
    response = requests.get(
        url,
        timeout=15,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        },
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text


def fetch_latest_draws() -> list[Draw]:
    # Try FALLBACK_URL first, then OFFICIAL_URL
    for url in (FALLBACK_URL, OFFICIAL_URL):
        try:
            html = _fetch_html(url)
            draws = _parse_pilio(html)
            if draws:
                return draws
        except Exception as error:
            print(url, error)
            continue
    
    # If both failed, try to get the last HTML and write to err.txt
    try:
        html = _fetch_html(FALLBACK_URL)
    except Exception as error:
        try:
            html = _fetch_html(OFFICIAL_URL)
        except Exception as error2:
            html = "無法獲取任何資料"
    
    # Write first 1000 characters to err.txt
    err_file = ROOT / "err.txt"
    err_file.write_text(html[:1000], encoding="utf-8")
    raise RuntimeError("無法抓取最新開獎資料，已寫入 err.txt")


def _row_to_draw(row: tuple[str, str, str, int | None, str | None, str | None]) -> dict:
    issue, time_text, numbers, super_number, big_small, odd_even = row
    return {
        "issue": issue,
        "time_text": time_text,
        "numbers": [int(x) for x in numbers.split(",") if x],
        "super_number": super_number,
        "big_small": big_small,
        "odd_even": odd_even,
    }


def get_history_draws(limit: int = 80) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT issue, time_text, numbers, super_number, big_small, odd_even FROM draws ORDER BY issue DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_draw(row) for row in rows]


def get_latest_draw() -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT issue, time_text, numbers, super_number, big_small, odd_even FROM draws ORDER BY issue DESC LIMIT 1"
        ).fetchone()
    return _row_to_draw(row) if row else None


def get_analysis_by_issue(issue: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT data FROM analysis WHERE issue = ?", (issue,)).fetchone()
    return json.loads(row[0]) if row else None


def get_recommendation_by_issue(issue: str) -> dict | None:
    with _connect() as con:
        row = con.execute("SELECT data FROM recommend WHERE issue = ?", (issue,)).fetchone()
    return json.loads(row[0]) if row else None


def get_statistics() -> dict[str, object]:
    with _connect() as con:
        rows = con.execute("SELECT key, value FROM statistics").fetchall()
    result: dict[str, object] = {}
    for key, value in rows:
        try:
            result[key] = json.loads(value)
        except Exception:
            result[key] = value
    return result
def insert_draw(issue: str, time_text: str, numbers: list[int], super_number: int | None = None):
    with _connect() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO draws
            (issue, time_text, numbers, super_number)
            VALUES (?, ?, ?, ?)
            """,
            (
                issue,
                time_text,
                ",".join(f"{n:02d}" for n in numbers),
                super_number,
            ),
        )
        con.commit()
