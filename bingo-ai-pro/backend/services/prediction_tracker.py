from __future__ import annotations

import logging

from database.official_draw_store import get_latest_official_draw, get_official_draw_by_issue
from database.prediction_tracker_store import (
    get_latest_prediction_run,
    get_pending_prediction_runs,
    get_prediction_history,
    get_prediction_statistics,
    save_prediction_results,
    save_prediction_run,
)

logger = logging.getLogger(__name__)


def _as_int_list(values) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    return numbers


def _as_int(value) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if 1 <= number <= 80 else None


def _numbers_from_snapshot(snapshot: dict) -> list[int]:
    parsed = snapshot.get("parsed_json") or {}
    api_data = parsed.get("api_get_data") if isinstance(parsed, dict) else None
    latest = (api_data.get("data") or [{}])[0] if isinstance(api_data, dict) else {}
    return _as_int_list(
        snapshot.get("numbers")
        or snapshot.get("draw_numbers")
        or snapshot.get("result")
        or latest.get("一般獎號")
        or latest.get("numbers")
        or latest.get("draw_numbers")
    )


def _super_from_mapping(value) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ["super_number", "super", "超級獎號", "超級號"]:
        number = _as_int(value.get(key))
        if number is not None:
            return number
    for item in value.values():
        if isinstance(item, dict):
            number = _super_from_mapping(item)
            if number is not None:
                return number
        if isinstance(item, list):
            for nested in item:
                number = _super_from_mapping(nested)
                if number is not None:
                    return number
    return None


def _super_from_snapshot(snapshot: dict) -> int | None:
    return _as_int(snapshot.get("super_number") or snapshot.get("super")) or _super_from_mapping(snapshot.get("parsed_json") or {})


def _actual_draw_from_record(record: dict) -> dict | None:
    numbers = _as_int_list(record.get("numbers")) or _numbers_from_snapshot(record)
    if not record.get("issue") or len(numbers) < 1:
        return None
    return {
        "issue": str(record.get("issue")),
        "numbers": numbers,
        "super_number": _as_int(record.get("super_number")) or _super_from_snapshot(record),
    }


def _load_actual_draw(issue: str) -> dict | None:
    target = str(issue)
    try:
        official = get_official_draw_by_issue(target, verified_only=True)
        if official:
            draw = _actual_draw_from_record(official)
            if draw:
                draw["source"] = "official"
                draw["verified"] = True
                return draw
    except Exception:
        logger.exception("failed to load official verified draw for prediction tracker")

    return None


def register_recommendation_prediction(
    recommendation: dict,
    recommendation_run_id: int | None,
    simulation_run_id: int | None = None,
) -> dict:
    try:
        if not recommendation_run_id:
            return {"status": "error", "message": "missing recommendation_run_id"}
        run = {
            "recommendation_run_id": recommendation_run_id,
            "simulation_run_id": simulation_run_id,
            "issue": recommendation.get("issue"),
            "target_issue": recommendation.get("target_issue"),
            "status": "pending",
        }
        return save_prediction_run(run)
    except Exception as exc:
        logger.exception("failed to register prediction run")
        return {"status": "error", "message": str(exc)}


def _super_prediction(recommendation: dict) -> int | None:
    super_recommendation = recommendation.get("super_recommendation") or {}
    recommended = super_recommendation.get("recommended") or []
    if not recommended:
        return None
    return _as_int(recommended[0].get("number"))


def _evaluate_results(recommendation: dict, actual_draw: dict) -> list[dict]:
    actual_numbers = _as_int_list(actual_draw.get("numbers"))
    actual_set = set(actual_numbers)
    actual_super = _as_int(actual_draw.get("super_number"))
    super_prediction = _super_prediction(recommendation)

    results = []
    for item in recommendation.get("results") or []:
        recommended = _as_int_list(item.get("numbers"))
        hit_numbers = sorted(set(recommended) & actual_set)
        miss_numbers = [number for number in recommended if number not in hit_numbers]
        results.append(
            {
                "rank": item.get("rank"),
                "recommended_numbers": recommended,
                "actual_numbers": actual_numbers,
                "hit_count": len(hit_numbers),
                "hit_numbers": hit_numbers,
                "miss_numbers": miss_numbers,
                "super_prediction": super_prediction,
                "actual_super": actual_super,
                "super_hit": bool(super_prediction is not None and actual_super is not None and super_prediction == actual_super),
                "confidence": item.get("confidence"),
                "strategy": item.get("strategy"),
            }
        )
    return results


def evaluate_prediction_run(prediction_run: dict, recommendation: dict, actual_draw: dict | None = None) -> dict:
    try:
        actual_draw = actual_draw or _load_actual_draw(prediction_run.get("target_issue"))
        if not actual_draw:
            return {"status": "pending", "message": "actual draw not available", "prediction_run_id": prediction_run.get("id")}

        results = _evaluate_results(recommendation, actual_draw)
        if not results:
            return {"status": "error", "message": "no recommendation results", "prediction_run_id": prediction_run.get("id")}
        saved = save_prediction_results(int(prediction_run["id"]), actual_draw["issue"], results)
        return {
            "status": "ok" if saved.get("status") == "ok" else "error",
            "prediction_run_id": prediction_run.get("id"),
            "actual_issue": actual_draw["issue"],
            "results": results,
            "saved": saved,
        }
    except Exception as exc:
        logger.exception("failed to evaluate prediction run")
        return {"status": "error", "message": str(exc), "prediction_run_id": prediction_run.get("id")}


def _issue_lte(left: str | None, right: str | None) -> bool:
    try:
        return int(left) <= int(right)
    except Exception:
        return False


def evaluate_pending_predictions(actual_draw: dict | None = None, max_runs: int = 3) -> dict:
    try:
        from database.recommendation_center_store import get_recommendation_history

        max_runs = max(1, min(int(max_runs or 3), 3))
        latest_official = get_latest_official_draw()
        latest_official_issue = str(latest_official.get("issue")) if latest_official else None
        if not latest_official_issue and not actual_draw:
            return {
                "status": "ok",
                "checked": 0,
                "evaluated": [],
                "skipped": "official_actual_not_available",
            }

        pending = get_pending_prediction_runs(limit=20)
        pending = [
            run
            for run in pending
            if actual_draw or _issue_lte(run.get("target_issue"), latest_official_issue)
        ][:max_runs]
        if not pending:
            return {
                "status": "ok",
                "checked": 0,
                "evaluated": [],
                "skipped": "no_ready_pending_predictions",
                "latest_official_issue": latest_official_issue,
            }

        needed_recommendation_ids = {run.get("recommendation_run_id") for run in pending}
        recommendations = {
            item.get("id"): item
            for item in get_recommendation_history(50)
            if item.get("id") in needed_recommendation_ids
        }
        evaluated = []
        for run in pending:
            recommendation = recommendations.get(run.get("recommendation_run_id"))
            if not recommendation:
                continue
            candidate_actual = actual_draw
            if candidate_actual and str(candidate_actual.get("issue")) != str(run.get("target_issue")):
                candidate_actual = None
            if candidate_actual and not (
                candidate_actual.get("source") == "official" and candidate_actual.get("verified") is True
            ):
                candidate_actual = None
            result = evaluate_prediction_run(run, recommendation, candidate_actual)
            evaluated.append(result)
        return {
            "status": "ok",
            "checked": len(pending),
            "evaluated": evaluated,
            "latest_official_issue": latest_official_issue,
            "max_runs": max_runs,
        }
    except Exception as exc:
        logger.exception("failed to evaluate pending predictions")
        return {"status": "error", "message": str(exc), "checked": 0, "evaluated": []}


def latest_prediction() -> dict:
    return {"status": "ok", "data": get_latest_prediction_run()}


def prediction_history(limit: int = 30) -> dict:
    return {"status": "ok", "data": get_prediction_history(limit)}


def prediction_statistics() -> dict:
    return {"status": "ok", "data": get_prediction_statistics()}
