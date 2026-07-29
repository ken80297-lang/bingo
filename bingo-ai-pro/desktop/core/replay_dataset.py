from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from desktop.core.validators import normalize_numbers


DEFAULT_MASTER_DRAWS_PATH = Path(r"C:\Users\ken80297\Desktop\master_draws.csv")
NUMBER_COLUMNS = [f"n{index:02d}" for index in range(1, 21)]
REQUIRED_COLUMNS = ["date", "issue", "time", *NUMBER_COLUMNS, "super", "big_small", "odd_even"]


@dataclass(frozen=True)
class ReplayDraw:
    date: str
    issue: str
    time: str
    numbers: list[int]
    super_number: int | None
    big_small: str | None
    odd_even: str | None
    row_number: int
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class DatasetSummary:
    total_rows: int
    valid_rows: int
    warmup_rows: int
    replay_target_rows: int
    first_issue: str | None
    last_issue: str | None
    missing_issues: list[str]
    duplicate_issues: list[str]
    invalid_rows: list[dict[str, Any]]


@dataclass(frozen=True)
class ReplayDataset:
    draws: list[ReplayDraw]
    summary: DatasetSummary
    path: Path

    @property
    def valid_draws(self) -> list[ReplayDraw]:
        return [draw for draw in self.draws if draw.valid]


def load_replay_dataset(path: str | Path = DEFAULT_MASTER_DRAWS_PATH) -> ReplayDataset:
    csv_path = Path(path)
    if not csv_path.exists():
        summary = DatasetSummary(0, 0, 0, 0, None, None, [], [], [{"row": 0, "issue": None, "errors": [f"file_not_found:{csv_path}"]}])
        return ReplayDataset([], summary, csv_path)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing_columns = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        draws = [_row_to_draw(row, index + 2, missing_columns) for index, row in enumerate(reader)]
    draws = sorted(draws, key=_issue_sort_key)
    duplicate_issues = _duplicate_issues(draws)
    invalid_rows = []
    seen_duplicates = set(duplicate_issues)
    normalized_draws = []
    for draw in draws:
        errors = list(draw.errors)
        if draw.issue in seen_duplicates:
            errors.append("duplicate_issue")
        normalized = ReplayDraw(
            draw.date,
            draw.issue,
            draw.time,
            draw.numbers,
            draw.super_number,
            draw.big_small,
            draw.odd_even,
            draw.row_number,
            errors,
        )
        normalized_draws.append(normalized)
        if errors:
            invalid_rows.append({"row": draw.row_number, "issue": draw.issue, "errors": errors})
    valid_draws = [draw for draw in normalized_draws if draw.valid]
    summary = DatasetSummary(
        total_rows=len(normalized_draws),
        valid_rows=len(valid_draws),
        warmup_rows=0,
        replay_target_rows=len(valid_draws),
        first_issue=valid_draws[0].issue if valid_draws else None,
        last_issue=valid_draws[-1].issue if valid_draws else None,
        missing_issues=_missing_issues(valid_draws),
        duplicate_issues=duplicate_issues,
        invalid_rows=invalid_rows,
    )
    return ReplayDataset(normalized_draws, summary, csv_path)


def _row_to_draw(row: dict[str, str], row_number: int, missing_columns: list[str]) -> ReplayDraw:
    errors = list(missing_columns)
    issue = str(row.get("issue") or "").strip()
    if not issue.isdigit():
        errors.append("invalid_issue")
    numbers = normalize_numbers([row.get(column) for column in NUMBER_COLUMNS])
    if len(numbers) != 20:
        errors.append("numbers_must_have_20_unique_values")
    super_number = _int_or_none(row.get("super"))
    if super_number is None:
        errors.append("invalid_super")
    elif super_number not in numbers:
        errors.append("super_not_in_numbers")
    inferred_big_small = _infer_big_small(numbers)
    inferred_odd_even = _infer_odd_even(numbers)
    raw_big_small = _normalize_big_small(row.get("big_small"))
    raw_odd_even = _normalize_odd_even(row.get("odd_even"))
    return ReplayDraw(
        date=str(row.get("date") or "").strip(),
        issue=issue,
        time=str(row.get("time") or "").strip(),
        numbers=numbers,
        super_number=super_number,
        big_small=raw_big_small or inferred_big_small,
        odd_even=raw_odd_even or inferred_odd_even,
        row_number=row_number,
        errors=errors,
    )


def _duplicate_issues(draws: list[ReplayDraw]) -> list[str]:
    counts: dict[str, int] = {}
    for draw in draws:
        if draw.issue:
            counts[draw.issue] = counts.get(draw.issue, 0) + 1
    return sorted([issue for issue, count in counts.items() if count > 1], key=lambda item: int(item) if item.isdigit() else item)


def _missing_issues(draws: list[ReplayDraw]) -> list[str]:
    issues = [int(draw.issue) for draw in draws if draw.issue.isdigit()]
    missing: list[str] = []
    for previous, current in zip(issues, issues[1:]):
        if current - previous > 1:
            missing.extend(str(issue) for issue in range(previous + 1, current))
    return missing


def _issue_sort_key(draw: ReplayDraw) -> tuple[int, str]:
    return (int(draw.issue), draw.issue) if draw.issue.isdigit() else (10**18, draw.issue)


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 80 else None


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_big_small(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    lowered = text.lower()
    if text in {"大", "偏大"} or lowered in {"big", "large", "high"}:
        return "偏大"
    if text in {"小", "偏小"} or lowered in {"small", "low"}:
        return "偏小"
    if text in {"均衡", "和"} or lowered in {"balanced", "balance", "equal"}:
        return "均衡"
    return None


def _normalize_odd_even(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    lowered = text.lower()
    if text in {"單", "偏單"} or lowered in {"odd"}:
        return "單"
    if text in {"雙", "偏雙"} or lowered in {"even"}:
        return "雙"
    if text in {"均衡", "和"} or lowered in {"balanced", "balance", "equal"}:
        return "均衡"
    return None


def _infer_big_small(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "偏大"
    if small > big:
        return "偏小"
    return "均衡"


def _infer_odd_even(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "單"
    if even > odd:
        return "雙"
    return "均衡"
