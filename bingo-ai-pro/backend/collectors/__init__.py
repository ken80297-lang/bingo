from __future__ import annotations

import logging
from datetime import date

from collectors.kuaishou_collector import fetch_kuaishou_snapshot
from collectors.pilio_collector import fetch_pilio_history
from database.analysis_store import save_analysis_history
from database.collector_store import save_draw_history, save_kuaishou_snapshot

logger = logging.getLogger(__name__)


def collect_kuaishou_snapshot() -> dict:
    try:
        snapshot = fetch_kuaishou_snapshot()
        result = save_kuaishou_snapshot(snapshot)
        try:
            if result.get("status") == "ok":
                result["analysis"] = save_analysis_history(snapshot)
        except Exception as exc:
            logger.exception("kuaishou analysis history save failed")
            result["analysis"] = {"status": "error", "error": str(exc)}
        return {"status": result.get("status", "unknown"), "saved": result}
    except Exception as exc:
        logger.exception("kuaishou collector failed")
        return {"status": "error", "error": str(exc)}


def collect_pilio_today() -> dict:
    try:
        draws = fetch_pilio_history(date.today())
        saved = []
        for draw in draws:
            result = save_draw_history(draw)
            try:
                if result.get("status") == "ok":
                    result["analysis"] = save_analysis_history(draw)
            except Exception as exc:
                logger.exception("pilio analysis history save failed")
                result["analysis"] = {"status": "error", "error": str(exc)}
            saved.append(result)
        return {"status": "ok", "count": len(draws), "saved": saved}
    except Exception as exc:
        logger.exception("pilio collector failed")
        return {"status": "error", "error": str(exc)}
