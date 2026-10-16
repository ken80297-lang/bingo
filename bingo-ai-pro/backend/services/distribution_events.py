"""Small in-process notifications; no official or database polling per viewer."""
from __future__ import annotations

import asyncio
import json
from threading import Lock

_lock = Lock()
_revision = 0
_issue = ""


def publish_draw(issue: str) -> None:
    global _revision, _issue
    issue = str(issue).strip()
    if not issue.isdigit():
        return
    with _lock:
        # No-op polls and writers racing with a newer draw must not cause
        # repeated full-day downloads or roll the notification backwards.
        if _issue and int(issue) <= int(_issue):
            return
        _revision += 1
        _issue = issue


def snapshot() -> tuple[int, str]:
    with _lock:
        return _revision, _issue


async def draw_events(request):
    seen, _ = snapshot()
    yield "retry: 5000\nevent: ready\ndata: {}\n\n"
    ticks = 0
    while not await request.is_disconnected():
        revision, issue = snapshot()
        if revision != seen:
            seen = revision
            yield "event: draw\ndata: " + json.dumps({"issue": issue}) + "\n\n"
        ticks += 1
        if ticks % 15 == 0:
            yield ": heartbeat\n\n"
        await asyncio.sleep(1)
