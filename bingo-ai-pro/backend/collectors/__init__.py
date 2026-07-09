from __future__ import annotations

import logging
import time
from datetime import date

from collectors.kuaishou_collector import fetch_kuaishou_snapshot
from collectors.pilio_collector import fetch_pilio_history
from database.analysis_store import save_analysis_history
from database.collector_store import save_draw_history, save_kuaishou_snapshot
from services.laowanjia_features import run_laowanjia_feature_analysis
from services.operations_center import record_operation_event
from services.prediction_tracker import evaluate_pending_predictions
from services.recommendation_center import generate_recommendation_center
from services.simulation_model import ensure_simulation_for_issue

logger = logging.getLogger(__name__)


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _pipeline_status(status: str | None) -> str:
    value = (status or "unknown").lower()
    if value == "ok":
        return "ok"
    if value == "error":
        return "error"
    return "warning"


def _record_event(
    component: str,
    issue: str | None,
    start: float,
    status: str,
    message: str,
    error: Exception | None = None,
) -> None:
    try:
        record_operation_event(
            component=component,
            event_type="pipeline_stage",
            status=status,
            issue=str(issue) if issue else None,
            message=message,
            duration_ms=_duration_ms(start),
            error_type=type(error).__name__ if error else None,
            error_message=str(error) if error else None,
        )
    except Exception:
        logger.exception("operation pipeline event failed")


def _run_dynamic_ai_pipeline(issue: str | None, result: dict, actual_draw: dict | None = None) -> None:
    if not issue:
        return
    start = time.perf_counter()
    try:
        prediction = evaluate_pending_predictions(actual_draw)
        result["prediction_tracker"] = {
            "status": prediction.get("status"),
            "checked": prediction.get("checked"),
            "evaluated_count": len(prediction.get("evaluated") or []),
        }
        _record_event(
            "prediction",
            issue,
            start,
            _pipeline_status(prediction.get("status")),
            "prediction tracker evaluated",
        )
    except Exception as exc:
        logger.exception("prediction tracker evaluation failed")
        result["prediction_tracker"] = {"status": "error", "error": str(exc)}
        _record_event("prediction", issue, start, "error", "prediction tracker failed", exc)

    start = time.perf_counter()
    try:
        laowanjia_feature = run_laowanjia_feature_analysis(limit=100, issue=str(issue))
        result["laowanjia_feature"] = {
            "status": laowanjia_feature.get("status"),
            "issue": (laowanjia_feature.get("data") or {}).get("issue"),
            "storage": (laowanjia_feature.get("saved") or {}).get("storage"),
        }
        _record_event(
            "laowanjia_features",
            issue,
            start,
            _pipeline_status(laowanjia_feature.get("status")),
            "laowanjia features generated",
        )
    except Exception as exc:
        logger.exception("dynamic laowanjia feature failed")
        result["laowanjia_feature"] = {"status": "error", "error": str(exc)}
        _record_event("laowanjia_features", issue, start, "error", "laowanjia features failed", exc)

    start = time.perf_counter()
    try:
        simulation = ensure_simulation_for_issue(issue, window=100, groups=5, numbers_per_group=10)
        result["simulation"] = {
            "status": simulation.get("status"),
            "skipped": simulation.get("skipped", False),
            "source_issue": issue,
            "run_id": (simulation.get("run") or {}).get("run_id") or (simulation.get("run") or {}).get("id"),
        }
        _record_event(
            "simulation",
            issue,
            start,
            _pipeline_status(simulation.get("status")),
            "simulation ensured for latest issue",
        )
    except Exception as exc:
        logger.exception("dynamic simulation failed")
        result["simulation"] = {"status": "error", "error": str(exc)}
        _record_event("simulation", issue, start, "error", "simulation failed", exc)

    start = time.perf_counter()
    try:
        recommendation = generate_recommendation_center()
        result["recommendation_center"] = {
            "status": recommendation.get("status"),
            "run_id": (recommendation.get("saved") or {}).get("run_id"),
        }
        _record_event(
            "recommendation",
            issue,
            start,
            _pipeline_status(recommendation.get("status")),
            "recommendation generated",
        )
    except Exception as exc:
        logger.exception("dynamic recommendation failed")
        result["recommendation_center"] = {"status": "error", "error": str(exc)}
        _record_event("recommendation", issue, start, "error", "recommendation failed", exc)


def collect_kuaishou_snapshot() -> dict:
    start = time.perf_counter()
    try:
        snapshot = fetch_kuaishou_snapshot()
        result = save_kuaishou_snapshot(snapshot)
        issue = snapshot.get("issue")
        _record_event(
            "collector",
            issue,
            start,
            _pipeline_status(result.get("status")),
            "kuaishou snapshot collected",
        )
        try:
            if result.get("status") == "ok":
                analysis_start = time.perf_counter()
                result["analysis"] = save_analysis_history(snapshot)
                _record_event(
                    "analysis",
                    issue,
                    analysis_start,
                    _pipeline_status((result.get("analysis") or {}).get("status")),
                    "analysis history saved",
                )
                _run_dynamic_ai_pipeline(snapshot.get("issue"), result, snapshot)
        except Exception as exc:
            logger.exception("kuaishou analysis history save failed")
            result["analysis"] = {"status": "error", "error": str(exc)}
            _record_event("analysis", issue, time.perf_counter(), "error", "analysis history failed", exc)
        return {"status": result.get("status", "unknown"), "saved": result}
    except Exception as exc:
        logger.exception("kuaishou collector failed")
        _record_event("collector", None, start, "error", "kuaishou collector failed", exc)
        return {"status": "error", "error": str(exc)}


def collect_pilio_today() -> dict:
    start = time.perf_counter()
    try:
        draws = fetch_pilio_history(date.today())
        saved = []
        _record_event("collector", None, start, "ok", f"pilio collected {len(draws)} draws")
        for draw in draws:
            result = save_draw_history(draw)
            issue = draw.get("issue")
            try:
                if result.get("status") == "ok":
                    analysis_start = time.perf_counter()
                    result["analysis"] = save_analysis_history(draw)
                    _record_event(
                        "analysis",
                        issue,
                        analysis_start,
                        _pipeline_status((result.get("analysis") or {}).get("status")),
                        "pilio analysis history saved",
                    )
                    _run_dynamic_ai_pipeline(draw.get("issue"), result, draw)
            except Exception as exc:
                logger.exception("pilio analysis history save failed")
                result["analysis"] = {"status": "error", "error": str(exc)}
                _record_event("analysis", issue, time.perf_counter(), "error", "pilio analysis history failed", exc)
            saved.append(result)
        return {"status": "ok", "count": len(draws), "saved": saved}
    except Exception as exc:
        logger.exception("pilio collector failed")
        _record_event("collector", None, start, "error", "pilio collector failed", exc)
        return {"status": "error", "error": str(exc)}
