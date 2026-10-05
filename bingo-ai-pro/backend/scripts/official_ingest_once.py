from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from collectors.taiwan_lottery_collector import fetch_official_bingo_results
from database.official_draw_store import get_latest_official_draw, save_official_draws

TAIPEI_TZ = timezone(timedelta(hours=8))


def _issue_int(value):
    try:
        return int(str(value))
    except Exception:
        return -1


def main() -> int:
    started = time.perf_counter()
    today = datetime.now(TAIPEI_TZ).date()
    draws = fetch_official_bingo_results(today, page_num=1, page_size=10)
    complete = [
        draw for draw in draws
        if str(draw.get("issue") or "").isdigit()
        and len(draw.get("numbers") or []) == 20
        and len(set(draw.get("numbers") or [])) == 20
    ]
    if not complete:
        print(json.dumps({"status": "waiting", "reason": "no_complete_official_draw"}))
        return 0

    latest_source = max(complete, key=lambda draw: _issue_int(draw.get("issue")))
    latest_db = get_latest_official_draw()
    if _issue_int((latest_db or {}).get("issue")) >= _issue_int(latest_source.get("issue")):
        print(json.dumps({
            "status": "noop",
            "source_issue": latest_source.get("issue"),
            "database_issue": (latest_db or {}).get("issue"),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        }))
        return 0

    latest_source["verification_status"] = "validated"
    latest_source["fetched_at"] = datetime.now(timezone.utc).isoformat()
    saved = save_official_draws([latest_source])
    ok = saved.get("status") == "ok" and int(saved.get("saved") or 0) >= 1
    print(json.dumps({
        "status": "ok" if ok else "error",
        "source_issue": latest_source.get("issue"),
        "database_issue_before": (latest_db or {}).get("issue"),
        "saved": saved.get("saved"),
        "storage": saved.get("storage"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
