from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TAIPEI = ZoneInfo("Asia/Taipei")
READ_OFFSETS = (70, 90, 110, 130, 150, 170, 180)


def draw_datetime(draw: dict) -> datetime | None:
    value = draw.get("draw_time")
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TAIPEI) if parsed.tzinfo is None else parsed.astimezone(TAIPEI)
    except (ValueError, TypeError):
        return None


def prediction_is_timely(draw: dict, *, now: datetime | None = None) -> bool:
    draw_at = draw_datetime(draw)
    return draw_at is not None and (now or datetime.now(TAIPEI)) < draw_at + timedelta(minutes=5)


def current_draw_slot(now: datetime) -> datetime | None:
    local = now.astimezone(TAIPEI)
    slot = local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)
    minutes = slot.hour * 60 + slot.minute
    return slot if 425 <= minutes <= 1435 else None


def wait_for_current_draw(read_draw, *, now=None, sleep=None) -> dict | None:
    now = now or (lambda: datetime.now(TAIPEI))
    sleep = sleep or time.sleep
    slot = current_draw_slot(now())
    if slot is None:
        print("AI_LIFECYCLE_CRON_NOOP reason=outside_draw_hours", flush=True)
        return None
    for offset in READ_OFFSETS:
        delay = (slot + timedelta(seconds=offset) - now()).total_seconds()
        if delay > 0:
            sleep(delay)
        if now() >= slot + timedelta(seconds=190):
            break
        draw = read_draw() or {}
        issue = str(draw.get("issue") or "").strip()
        draw_at = draw_datetime(draw)
        ready = issue.isdigit() and draw_at == slot
        print(json.dumps({"event": "ai_lifecycle_draw_readiness", "slot": slot.isoformat(),
                          "observed_issue": issue, "observed_draw_time": draw_at.isoformat() if draw_at else None,
                          "offset_seconds": round((now() - slot).total_seconds(), 2), "ready": ready}), flush=True)
        if ready:
            return draw
    print("AI_LIFECYCLE_CRON_NOOP reason=current_draw_not_ready", flush=True)
    return None


def main() -> int:
    from database.official_draw_store import get_latest_official_draw

    draw = wait_for_current_draw(get_latest_official_draw)
    if not draw:
        return 0
    issue = str(draw["issue"])

    env = os.environ.copy()
    env["AI_LIFECYCLE_ISSUE"] = issue
    env["AI_LIFECYCLE_CRON_CORE_ONLY"] = "1"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ai_lifecycle_worker_once.py")],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
