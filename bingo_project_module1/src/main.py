from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "bingo.db"

# 官方頁面可作資料來源；若官方頁面使用前端動態載入，程式會退回第三方純文字頁面。
OFFICIAL_URL = "https://www.taiwanlottery.com/lotto/result/bingo_bingo/"
FALLBACK_URL = "https://www.pilio.idv.tw/bingo/list.asp"

@dataclass(frozen=True)
class Draw:
    issue: str
    time_text: str
    numbers: tuple[int, ...]
    super_number: int | None = None
    big_small: str | None = None
    odd_even: str | None = None


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
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


def save_draws(draws: Iterable[Draw]) -> int:
    rows = [
        (
            d.issue,
            d.time_text,
            ",".join(f"{n:02d}" for n in d.numbers),
            d.super_number,
            d.big_small,
            d.odd_even,
        )
        for d in draws
    ]
    with sqlite3.connect(DB_PATH) as con:
        before = con.total_changes
        con.executemany(
            """
            INSERT OR IGNORE INTO draws(issue, time_text, numbers, super_number, big_small, odd_even)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return con.total_changes - before


def fetch_html(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 bingo-module1/1.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse_pilio(html: str) -> list[Draw]:
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    pattern = re.compile(
        r"超級獎號:(?P<super>\d{2}|－).*?猜大小:(?P<bs>大|小|－).*?猜單雙:(?P<oe>單|雙|－).*?\((?P<time>\d{2}:\d{2})\).*?期別:\s*(?P<issue>\d+).*?(?P<num>(?:\d{2}[,，\s]+){19}\d{2})",
        re.S,
    )
    draws: list[Draw] = []
    for m in pattern.finditer(text):
        nums = tuple(int(x) for x in re.findall(r"\d{2}", m.group("num"))[:20])
        super_raw = m.group("super")
        draws.append(
            Draw(
                issue=m.group("issue"),
                time_text=m.group("time"),
                numbers=nums,
                super_number=None if super_raw == "－" else int(super_raw),
                big_small=None if m.group("bs") == "－" else m.group("bs"),
                odd_even=None if m.group("oe") == "－" else m.group("oe"),
            )
        )
    return draws


def fetch_latest_draws() -> list[Draw]:
    # 官方站目前常以動態方式呈現，若解析不到資料就用 fallback。
    try:
        html = fetch_html(OFFICIAL_URL)
        official_draws = parse_pilio(html)  # 保留同介面，若官方 HTML 變成可解析可替換 parser
        if official_draws:
            return official_draws
    except Exception:
        pass
    return parse_pilio(fetch_html(FALLBACK_URL))


def load_recent(limit: int = 80) -> list[Draw]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT issue, time_text, numbers, super_number, big_small, odd_even FROM draws ORDER BY issue DESC LIMIT ?",
            (limit,),
        ).fetchall()
    draws = []
    for issue, time_text, nums, super_number, big_small, odd_even in rows:
        draws.append(Draw(issue, time_text, tuple(int(x) for x in nums.split(",")), super_number, big_small, odd_even))
    return draws


def neighbors(n: int) -> set[int]:
    return {x for x in (n - 2, n - 1, n + 1, n + 2) if 1 <= x <= 80}


def diagonal_neighbors(n: int) -> set[int]:
    # 8x10 盤：01-10 第一列，11-20 第二列；斜線看左上/右上/左下/右下。
    row, col = divmod(n - 1, 10)
    out = set()
    for dr, dc in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
        r, c = row + dr, col + dc
        if 0 <= r < 8 and 0 <= c < 10:
            out.add(r * 10 + c + 1)
    return out


def module1_analyze(draws: list[Draw]) -> dict[str, object]:
    if not draws:
        return {"error": "尚無資料"}

    recent20 = draws[:20]
    recent50 = draws[:50]
    last = draws[0]

    score = {n: 0.0 for n in range(1, 81)}
    for d in recent20:
        for n in d.numbers:
            score[n] += 2.0
    for d in recent50:
        for n in d.numbers:
            score[n] += 0.6

    last_set = set(last.numbers)
    for n in last_set:
        score[n] += 1.5  # 重號
        for x in neighbors(n):
            score[x] += 0.9  # 補號
        for x in diagonal_neighbors(n):
            score[x] += 1.1  # 斜線

    for n in [11, 22, 33, 44, 55, 66, 77]:
        score[n] += 1.2

    # 控制大小單雙不要過度偏一邊：先取高分，再做平衡。
    ranked = sorted(score, key=lambda x: score[x], reverse=True)
    five: list[int] = []
    for n in ranked:
        if len(five) == 5:
            break
        small_count = sum(x <= 40 for x in five)
        odd_count = sum(x % 2 == 1 for x in five)
        if len(five) >= 3:
            if small_count >= 4 and n <= 40:
                continue
            if len(five) - small_count >= 4 and n > 40:
                continue
            if odd_count >= 4 and n % 2 == 1:
                continue
            if len(five) - odd_count >= 4 and n % 2 == 0:
                continue
        five.append(n)

    three = five[:3]
    super_candidates = sorted(
        [n for n in ranked[:20] if n in last_set or n in [11, 22, 33, 44, 55, 66, 77]][:3]
    )

    small_last = sum(n <= 40 for n in last.numbers)
    odd_last = sum(n % 2 == 1 for n in last.numbers)

    return {
        "latest_issue": last.issue,
        "latest_time": last.time_text,
        "last_numbers": [f"{n:02d}" for n in last.numbers],
        "three_star": [f"{n:02d}" for n in sorted(three)],
        "five_star": [f"{n:02d}" for n in sorted(five)],
        "super_candidates": [f"{n:02d}" for n in super_candidates],
        "big_small_bias": "小偏" if small_last >= 13 else "大偏" if (20 - small_last) >= 13 else "中性",
        "odd_even_bias": "單偏" if odd_last >= 13 else "雙偏" if (20 - odd_last) >= 13 else "中性",
    }


def main() -> None:
    init_db()
    draws = fetch_latest_draws()
    added = save_draws(draws)
    recent = load_recent(80)
    result = module1_analyze(recent)

    print(f"新增期數：{added}")
    print(f"最新期別：{result.get('latest_issue')}  時間：{result.get('latest_time')}")
    print(f"上一期號碼：{' '.join(result.get('last_numbers', []))}")
    print(f"三星：{' '.join(result.get('three_star', []))}")
    print(f"五星：{' '.join(result.get('five_star', []))}")
    print(f"超級獎號候選：{' '.join(result.get('super_candidates', []))}")
    print(f"大小：{result.get('big_small_bias')}｜單雙：{result.get('odd_even_bias')}")


if __name__ == "__main__":
    main()
