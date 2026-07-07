from __future__ import annotations

import logging
from datetime import date

from collectors.kuaishou_collector import fetch_kuaishou_snapshot
from collectors.pilio_collector import fetch_pilio_history
from database.collector_store import save_draw_history, save_kuaishou_snapshot

logger = logging.getLogger(__name__)


def collect_kuaishou_snapshot() -> dict:
    try:
        snapshot = fetch_kuaishou_snapshot()
        result = save_kuaishou_snapshot(snapshot)
        return {"status": result.get("status", "unknown"), "saved": result}
    except Exception as exc:
        logger.exception("kuaishou collector failed")
        return {"status": "error", "error": str(exc)}


def collect_pilio_today() -> dict:
    try:
        draws = fetch_pilio_history(date.today())
        saved = []
        for draw in draws:
            saved.append(save_draw_history(draw))
        return {"status": "ok", "count": len(draws), "saved": saved}
    except Exception as exc:
        logger.exception("pilio collector failed")
        return {"status": "error", "error": str(exc)}
