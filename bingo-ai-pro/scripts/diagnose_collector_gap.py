from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from collectors.taiwan_lottery_collector import fetch_official_bingo_results, get_last_official_fetch_diagnostics

TAIPEI_TZ = timezone(timedelta(hours=8))


def _issue_int(value):
    try:
        return int(str(value))
    except Exception:
        return None


def _valid_draw(draw):
    numbers = draw.get("numbers") or []
    try:
        normalized = [int(value) for value in numbers]
    except Exception:
        normalized = []
    super_number = _issue_int(draw.get("super_number"))
    return {
        "issue": str(draw.get("issue") or ""),
        "number_count": len(normalized),
        "unique_count": len(set(normalized)),
        "numbers_in_range": all(1 <= number <= 80 for number in normalized),
        "super_in_numbers": super_number in normalized if super_number is not None else None,
        "draw_time_present": bool(draw.get("draw_time")),
        "valid": (
            len(normalized) == 20
            and len(set(normalized)) == 20
            and all(1 <= number <= 80 for number in normalized)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only collector gap diagnostic.")
    parser.add_argument("--db-latest-issue", required=True)
    parser.add_argument("--source-latest-issue")
    parser.add_argument("--live-fetch", action="store_true", help="Fetch the official source once for read-only diagnostics.")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()

    db_latest = _issue_int(args.db_latest_issue)
    source_latest_input = _issue_int(args.source_latest_issue)
    target = db_latest + 1 if db_latest is not None else None
    started = time.perf_counter()
    draws_by_issue = {}
    if args.live_fetch:
        query_date = datetime.now(TAIPEI_TZ).date()
        pages = max(1, min(int(args.pages or 1), 3))
        page_size = max(1, min(int(args.page_size or 100), 100))
        for page in range(1, pages + 1):
            for draw in fetch_official_bingo_results(query_date, page_num=page, page_size=page_size):
                issue = str(draw.get("issue") or "")
                if issue:
                    draws_by_issue[issue] = draw
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    parsed_latest = max((_issue_int(issue) for issue in draws_by_issue), default=None)
    source_latest = source_latest_input or parsed_latest
    target_draw = draws_by_issue.get(str(target)) if target is not None else None
    diagnostics = get_last_official_fetch_diagnostics()[-args.pages :]

    payload = {
        "read_only": True,
        "db_latest_issue": str(args.db_latest_issue),
        "source_latest_issue_input": str(args.source_latest_issue) if args.source_latest_issue else None,
        "source_latest_issue_parsed": str(parsed_latest) if parsed_latest is not None else None,
        "source_latest_issue_effective": str(source_latest) if source_latest is not None else None,
        "gap_count": max(0, source_latest - db_latest) if db_latest is not None and source_latest is not None else None,
        "first_missing_issue": str(target) if target is not None else None,
        "target_exists_by_latest_gate": bool(target is not None and source_latest is not None and target <= source_latest),
        "target_found_in_fetched_page": target_draw is not None,
        "target_validation": _valid_draw(target_draw) if target_draw else None,
        "live_fetch": bool(args.live_fetch),
        "max_pages": max(1, min(int(args.pages or 1), 3)) if args.live_fetch else 0,
        "fetched_issue_count": len(draws_by_issue),
        "fetched_min_issue": min(draws_by_issue, key=lambda value: int(value)) if draws_by_issue else None,
        "fetched_max_issue": max(draws_by_issue, key=lambda value: int(value)) if draws_by_issue else None,
        "elapsed_ms": elapsed_ms,
        "source_diagnostics": diagnostics,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
