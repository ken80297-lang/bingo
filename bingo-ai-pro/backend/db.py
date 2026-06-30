from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from database.cloud_draws import insert_cloud_draw


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "bingo.db"

KUAISHOU_API_URL = "https://bingo2.kuaishou1688.com/api/get_data"


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


def fetch_kuaishou_data(count=None) -> dict:
    response = requests.post(
        KUAISHOU_API_URL,
        json={"count": count},
        timeout=20,
        headers={
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://bingo2.kuaishou1688.com",
            "referer": "https://bingo2.kuaishou1688.com/",
            "user-agent": "Mozilla/5.0",
        },
    )
    response.raise_for_status()
    return response.json()


def fetch_latest_draws() -> list[Draw]:
    result = fetch_kuaishou_data()

    if not result.get("success"):
        raise RuntimeError("快手 API 回傳失敗")

    draws: list[Draw] = []

    for item in result.get("data", []):
        numbers = [int(x) for x in item["一般獎號"]]

        if len(numbers) != 20:
            continue

        super_raw = item.get("超級獎號")

        draws.append(
            Draw(
                issue=str(item["期數"]),
                time_text=item.get("開獎時間", ""),
                numbers=numbers,
                super_number=None if super_raw in (None, "－", "") else int(super_raw),
                big_small=None if item.get("大小") == "－" else item.get("大小"),
                odd_even=None if item.get("單雙") == "－" else item.get("單雙"),
            )
        )

    return draws


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
                source="kuaishou",
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
            """
            SELECT issue, time_text, numbers, super_number, big_small, odd_even
            FROM draws
            ORDER BY issue DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_draw(row) for row in rows]


def get_latest_draw() -> dict | None:
    with _connect() as con:
        row = con.execute(
            """
            SELECT issue, time_text, numbers, super_number, big_small, odd_even
            FROM draws
            ORDER BY issue DESC
            LIMIT 1
            """
        ).fetchone()
    return _row_to_draw(row) if row else None


def get_analysis_by_issue(issue: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT data FROM analysis WHERE issue = ?",
            (issue,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def get_recommendation_by_issue(issue: str) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT data FROM recommend WHERE issue = ?",
            (issue,),
        ).fetchone()
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


def insert_draw(
    issue: str,
    time_text: str,
    numbers: list[int],
    super_number: int | None = None,
):
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