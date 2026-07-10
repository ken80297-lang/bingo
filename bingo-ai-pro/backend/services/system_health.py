from __future__ import annotations

import logging
from datetime import datetime, timezone

from database.collector_store import get_latest_kuaishou_snapshot
from database.official_draw_store import get_latest_official_draw, get_official_statistics_counts
from database.prediction_tracker_store import get_latest_prediction_run
from database.recommendation_center_store import get_recommendation_run_by_issue
from database.simulation_store import get_simulation_run_by_issue
from database.strategy_evolution_store import get_latest_strategy_version
from database.system_health_store import save_system_health_report
from services.simulation_model import get_production_latest_issue

logger = logging.getLogger(__name__)


def _issue_lag(latest_issue: str | None, issue: str | None) -> int | None:
    try:
        return max(0, int(latest_issue) - int(issue))
    except Exception:
        return None


def _status_from_lag(lag: int | None, fallback: str = "unknown") -> str:
    if lag is None:
        return fallback
    if lag == 0:
        return "ok"
    if lag == 1:
        return "warning"
    return "error"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _delay_seconds(start: str | None, end: str | None) -> float | None:
    start_dt = _parse_time(start)
    end_dt = _parse_time(end)
    if not start_dt or not end_dt:
        return None
    return round(max(0, (end_dt - start_dt).total_seconds()), 3)


def _overall_status(parts: list[dict]) -> str:
    statuses = [part.get("status") for part in parts]
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    if all(status in ("ok", "pending") for status in statuses):
        return "ok"
    return "unknown"


def build_system_health(save: bool = True) -> dict:
    try:
        latest_issue = get_production_latest_issue()
        collector = get_latest_kuaishou_snapshot()
        collector_issue = str(collector.get("issue")) if collector and collector.get("issue") is not None else None
        collector_lag = _issue_lag(latest_issue, collector_issue)

        simulation = get_simulation_run_by_issue(latest_issue) if latest_issue else None
        simulation_issue = simulation.get("source_issue") if simulation else None
        simulation_lag = _issue_lag(latest_issue, simulation_issue)

        recommendation = get_recommendation_run_by_issue(latest_issue) if latest_issue else None
        recommendation_issue = recommendation.get("issue") if recommendation else None
        recommendation_lag = _issue_lag(latest_issue, recommendation_issue)
        super_issue = None
        if recommendation:
            super_recommendation = recommendation.get("super_recommendation") or {}
            super_issue = super_recommendation.get("based_on_issue") or super_recommendation.get("source_issue")

        prediction = get_latest_prediction_run()
        prediction_issue = prediction.get("issue") if prediction else None
        prediction_lag = _issue_lag(latest_issue, prediction_issue)

        evolution = get_latest_strategy_version()
        official = get_latest_official_draw()
        official_issue = str(official.get("issue")) if official and official.get("issue") is not None else None
        official_lag = _issue_lag(collector_issue, official_issue)
        official_counts = get_official_statistics_counts()
        mismatch_count = official_counts.get("mismatch_count", 0)
        waiting_count = official_counts.get("waiting_count", 0)
        if mismatch_count:
            official_status = "error"
        elif waiting_count:
            official_status = "warning"
        else:
            official_status = _status_from_lag(official_lag)

        collector_time = collector.get("updated_at") if collector else None
        simulation_time = simulation.get("generated_at") or simulation.get("created_at") if simulation else None
        recommendation_time = recommendation.get("created_at") if recommendation else None
        prediction_time = prediction.get("created_at") or prediction.get("updated_at") if prediction else None

        payload = {
            "status": "unknown",
            "latest_issue": latest_issue,
            "collector": {
                "issue": collector_issue,
                "status": _status_from_lag(collector_lag),
                "lag": collector_lag,
                "last_update": collector_time,
            },
            "simulation": {
                "issue": simulation_issue,
                "status": _status_from_lag(simulation_lag),
                "lag": simulation_lag,
                "generated_at": simulation_time,
            },
            "recommendation": {
                "issue": recommendation_issue,
                "target_issue": recommendation.get("target_issue") if recommendation else None,
                "super_issue": super_issue,
                "status": _status_from_lag(recommendation_lag),
                "lag": recommendation_lag,
                "created_at": recommendation_time,
            },
            "prediction": {
                "issue": prediction_issue,
                "target_issue": prediction.get("target_issue") if prediction else None,
                "status": prediction.get("status") if prediction and prediction_lag == 0 else _status_from_lag(prediction_lag),
                "lag": prediction_lag,
                "created_at": prediction_time,
            },
            "evolution": {
                "status": "ok" if evolution else "unknown",
                "last_version": evolution.get("version") if evolution else None,
                "created_at": evolution.get("created_at") if evolution else None,
            },
            "official_verification": {
                "latest_official_issue": official_issue,
                "latest_kuaishou_issue": collector_issue,
                "status": official_status,
                "lag": official_lag,
                "mismatch_count": mismatch_count,
                "waiting_count": waiting_count,
                "waiting_kuaishou_count": official_counts.get("waiting_kuaishou_count", 0),
                "waiting_official_count": official_counts.get("waiting_official_count", 0),
                "waiting_super_number_count": official_counts.get("waiting_super_number_count", 0),
            },
            "pipeline": {
                "status": "unknown",
                "delay_seconds": {
                    "collector_to_simulation": _delay_seconds(collector_time, simulation_time),
                    "simulation_to_recommendation": _delay_seconds(simulation_time, recommendation_time),
                    "recommendation_to_prediction": _delay_seconds(recommendation_time, prediction_time),
                },
            },
        }

        payload["pipeline"]["status"] = _overall_status(
            [
                payload["collector"],
                payload["simulation"],
                payload["recommendation"],
                payload["prediction"],
                payload["evolution"],
                payload["official_verification"],
            ]
        )
        payload["status"] = payload["pipeline"]["status"]

        if save:
            try:
                payload["saved"] = save_system_health_report(payload)
            except Exception:
                logger.exception("failed to save system health report")

        return payload
    except Exception as exc:
        logger.exception("system health build failed")
        return {
            "status": "error",
            "message": str(exc),
            "latest_issue": None,
            "collector": {},
            "simulation": {},
            "recommendation": {},
            "prediction": {},
            "evolution": {},
            "official_verification": {},
            "pipeline": {"status": "error", "delay_seconds": {}},
        }
