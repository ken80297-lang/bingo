from __future__ import annotations

import logging
from datetime import date

from collectors.kuaishou_collector import fetch_kuaishou_snapshot
from collectors.pilio_collector import fetch_pilio_history
from database.analysis_store import save_analysis_history
from database.collector_store import save_draw_history, save_kuaishou_snapshot
from services.laowanjia_features import run_laowanjia_feature_analysis
from services.recommendation_center import generate_recommendation_center
from services.simulation_model import ensure_simulation_for_issue

logger = logging.getLogger(__name__)


def _run_dynamic_ai_pipeline(issue: str | None, result: dict) -> None:
    if not issue:
        return
    try:
        laowanjia_feature = run_laowanjia_feature_analysis(limit=100, issue=str(issue))
        result["laowanjia_feature"] = {
            "status": laowanjia_feature.get("status"),
            "issue": (laowanjia_feature.get("data") or {}).get("issue"),
            "storage": (laowanjia_feature.get("saved") or {}).get("storage"),
        }
    except Exception as exc:
        logger.exception("dynamic laowanjia feature failed")
        result["laowanjia_feature"] = {"status": "error", "error": str(exc)}

    try:
        simulation = ensure_simulation_for_issue(issue, window=100, groups=5, numbers_per_group=10)
        result["simulation"] = {
            "status": simulation.get("status"),
            "skipped": simulation.get("skipped", False),
            "source_issue": issue,
            "run_id": (simulation.get("run") or {}).get("run_id") or (simulation.get("run") or {}).get("id"),
        }
    except Exception as exc:
        logger.exception("dynamic simulation failed")
        result["simulation"] = {"status": "error", "error": str(exc)}

    try:
        recommendation = generate_recommendation_center()
        result["recommendation_center"] = {
            "status": recommendation.get("status"),
            "run_id": (recommendation.get("saved") or {}).get("run_id"),
        }
    except Exception as exc:
        logger.exception("dynamic recommendation failed")
        result["recommendation_center"] = {"status": "error", "error": str(exc)}


def collect_kuaishou_snapshot() -> dict:
    try:
        snapshot = fetch_kuaishou_snapshot()
        result = save_kuaishou_snapshot(snapshot)
        try:
            if result.get("status") == "ok":
                result["analysis"] = save_analysis_history(snapshot)
                _run_dynamic_ai_pipeline(snapshot.get("issue"), result)
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
                    _run_dynamic_ai_pipeline(draw.get("issue"), result)
            except Exception as exc:
                logger.exception("pilio analysis history save failed")
                result["analysis"] = {"status": "error", "error": str(exc)}
            saved.append(result)
        return {"status": "ok", "count": len(draws), "saved": saved}
    except Exception as exc:
        logger.exception("pilio collector failed")
        return {"status": "error", "error": str(exc)}
