from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from database.analysis_store import get_analysis_history
from database.learning_store import (
    get_learning_model_performance,
    get_learning_records,
    get_learning_status_counts,
    upsert_learning_record,
)
from database.official_draw_store import get_official_draw_by_issue
from database.prediction_history_store import (
    get_prediction_history_records,
)
from services.operations_center import record_operation_event

logger = logging.getLogger(__name__)

ENGINE_VERSION = "22.1"
DEFAULT_MODEL_VERSION = "v7"
TOP_N_VALUES = (5, 10, 20)


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _as_int_list(values: Any) -> list[int]:
    numbers = []
    for value in values or []:
        try:
            number = int(value)
        except Exception:
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    return numbers


def _analysis_by_issue(issue: str) -> dict:
    for item in get_analysis_history(300):
        if str(item.get("issue")) == str(issue):
            return item
    return {}


def _latest_prediction_for_issue(issue: str | None = None) -> dict | None:
    records = get_prediction_history_records(200)
    if issue:
        for item in records:
            if str(item.get("prediction_issue")) == str(issue):
                return item
        return None
    return records[0] if records else None


def _learning_snapshots_for_issue(issue: str) -> list[dict]:
    return get_learning_records(
        limit=200,
        issue=str(issue),
        prediction_type="live_prediction",
    )


def _resolve_pending_snapshot(source_issue: str, target_issue: str) -> dict:
    pending_issue = f"pending:{source_issue}"
    pending_records = _learning_snapshots_for_issue(pending_issue)
    if not pending_records:
        return {"status": "skipped", "message": "no pending snapshot", "records": 0}

    saved = []
    for pending in pending_records:
        prediction_snapshot = pending.get("prediction_snapshot") or {}
        prediction_snapshot["target_issue"] = target_issue
        resolved = {
            **pending,
            "issue": target_issue,
            "source_issue": source_issue,
            "target_issue": target_issue,
            "history_cutoff_issue": pending.get("history_cutoff_issue") or source_issue,
            "prediction_created_at": pending.get("prediction_created_at"),
            "prediction_snapshot": prediction_snapshot,
            "verification_status": "pending_official",
            "learned_status": "pending",
            "learned_at": None,
            "error_message": None,
        }
        saved.append(upsert_learning_record(resolved))

        marker = {
            **pending,
            "verification_status": "target_resolved",
            "learned_status": "resolved_to_target",
            "target_issue": target_issue,
            "error_message": None,
        }
        saved.append(upsert_learning_record(marker))

    return {
        "status": "ok",
        "source_issue": source_issue,
        "target_issue": target_issue,
        "pending_issue": pending_issue,
        "records": len(pending_records),
        "saved": saved,
    }


def _model_candidates(model_name: str, model_scores: dict, fallback_numbers: list[int]) -> list[int]:
    payload = (model_scores or {}).get(model_name)
    if isinstance(payload, dict):
        numbers = _as_int_list(payload.get("candidate_numbers"))
        if numbers:
            return numbers
    return fallback_numbers


def _model_weight(model_name: str, model_scores: dict) -> dict:
    confidences = {
        key: float((value or {}).get("confidence") or 0)
        for key, value in (model_scores or {}).items()
        if isinstance(value, dict)
    }
    total = sum(confidences.values()) or 1
    confidence = confidences.get(model_name, 0)
    return {
        "confidence": round(confidence, 4),
        "weight": round(confidence / total, 6),
    }


def calculate_model_result(predicted_numbers: list[int], official_numbers: list[int]) -> dict:
    predicted = _as_int_list(predicted_numbers)
    official = _as_int_list(official_numbers)
    hit_numbers = sorted(set(predicted) & set(official))
    predicted_count = max(1, len(predicted))
    hit_count = len(hit_numbers)
    return {
        "hit_numbers": hit_numbers,
        "hit_count": hit_count,
        "predicted_count": len(predicted),
        "precision_score": round(hit_count / predicted_count, 4),
        "official_coverage": round(hit_count / 20, 4),
    }


def _rank_score(hit_count: int, top_n: int) -> float:
    coverage = hit_count / max(1, top_n)
    bonus = 1 / max(1, top_n)
    return round((coverage * 100) + (hit_count * bonus), 4)


def capture_prediction_snapshot(issue: str | None = None) -> dict:
    if issue:
        records = _learning_snapshots_for_issue(str(issue))
        if records:
            first = records[0]
            return {
                "status": "ok",
                "issue": str(issue),
                "prediction_snapshot": first.get("prediction_snapshot") or {},
                "analysis_snapshot": first.get("analysis_snapshot") or {},
                "learning_records": records,
            }

    return {
        "status": "missing_snapshot",
        "issue": issue,
        "prediction_snapshot": None,
        "analysis_snapshot": {},
        "learning_records": [],
    }


def save_live_prediction_snapshot(recommendation: dict) -> dict:
    source_issue = str(recommendation.get("issue") or "") or None
    target_issue = str(recommendation.get("target_issue") or "") or None
    if not source_issue:
        return {
            "status": "skipped",
            "message": "missing source_issue",
            "source_issue": source_issue,
            "target_issue": target_issue,
        }
    snapshot_issue = target_issue or f"pending:{source_issue}"
    initial_verification_status = "pending_official" if target_issue else "pending_target_issue"
    pending_resolution = None
    if target_issue:
        pending_resolution = _resolve_pending_snapshot(source_issue, target_issue)
    existing = _learning_snapshots_for_issue(snapshot_issue)
    if existing:
        return {
            "status": "ok",
            "skipped": True,
            "message": "live prediction snapshot already exists",
            "source_issue": source_issue,
            "target_issue": target_issue,
            "history_cutoff_issue": existing[0].get("history_cutoff_issue"),
            "prediction_created_at": existing[0].get("prediction_created_at"),
            "records": len(existing),
            "pending_resolution": pending_resolution,
        }

    prediction_created_at = recommendation.get("created_at") or datetime.utcnow().isoformat()
    voting = recommendation.get("model_voting") or {}
    model_scores = recommendation.get("model_scores") or voting.get("model_scores") or {}
    results = recommendation.get("results") or []
    fallback_numbers = _as_int_list((results[0] if results else {}).get("numbers"))
    ensemble_numbers = _as_int_list(voting.get("final_candidates")) or fallback_numbers
    analysis = _analysis_by_issue(source_issue)
    snapshot = {
        "source_issue": source_issue,
        "target_issue": target_issue,
        "history_cutoff_issue": source_issue,
        "prediction_created_at": prediction_created_at,
        "best_strategy": recommendation.get("best_strategy"),
        "confidence": recommendation.get("confidence"),
        "model_voting": voting,
        "results": results,
        "super_recommendation": recommendation.get("super_recommendation"),
        "sync": recommendation.get("sync"),
    }

    model_names = list(model_scores.keys())
    records = []
    for model_name in model_names:
        candidates = _model_candidates(model_name, model_scores, fallback_numbers)
        for top_n in TOP_N_VALUES:
            records.append(
                {
                    "issue": snapshot_issue,
                    "source_issue": source_issue,
                    "target_issue": target_issue,
                    "history_cutoff_issue": source_issue,
                    "prediction_created_at": prediction_created_at,
                    "draw_time": None,
                    "model_name": model_name,
                    "model_version": DEFAULT_MODEL_VERSION,
                    "prediction_type": "live_prediction",
                    "predicted_numbers": candidates[:top_n],
                    "predicted_scores": model_scores.get(model_name, {}),
                    "model_weight": _model_weight(model_name, model_scores),
                    "official_numbers": [],
                    "hit_numbers": [],
                    "predicted_count": len(candidates[:top_n]),
                    "hit_count": 0,
                    "precision_score": 0,
                    "official_coverage": 0,
                    "rank_score": 0,
                    "top_n": top_n,
                    "prediction_snapshot": snapshot,
                    "analysis_snapshot": analysis,
                    "verification_status": initial_verification_status,
                    "learned_status": "pending",
                    "learned_at": None,
                    "error_message": None,
                }
            )

    for top_n in TOP_N_VALUES:
        records.append(
            {
                "issue": snapshot_issue,
                "source_issue": source_issue,
                "target_issue": target_issue,
                "history_cutoff_issue": source_issue,
                "prediction_created_at": prediction_created_at,
                "draw_time": None,
                "model_name": "ensemble",
                "model_version": DEFAULT_MODEL_VERSION,
                "prediction_type": "live_prediction",
                "predicted_numbers": ensemble_numbers[:top_n],
                "predicted_scores": {
                    "confidence": voting.get("confidence") or recommendation.get("confidence"),
                    "winning_model": voting.get("winning_model") or recommendation.get("winning_model"),
                },
                "model_weight": {"confidence": voting.get("confidence") or recommendation.get("confidence"), "weight": 1.0},
                "official_numbers": [],
                "hit_numbers": [],
                "predicted_count": len(ensemble_numbers[:top_n]),
                "hit_count": 0,
                "precision_score": 0,
                "official_coverage": 0,
                "rank_score": 0,
                "top_n": top_n,
                "prediction_snapshot": snapshot,
                "analysis_snapshot": analysis,
                "verification_status": initial_verification_status,
                "learned_status": "pending",
                "learned_at": None,
                "error_message": None,
            }
        )

    saved = [upsert_learning_record(record) for record in records]
    return {
        "status": "ok",
        "source_issue": source_issue,
        "target_issue": target_issue,
        "history_cutoff_issue": source_issue,
        "prediction_created_at": prediction_created_at,
        "records": len(records),
        "saved": saved,
        "pending_resolution": pending_resolution,
    }


def _learning_records_from_prediction(prediction: dict, official: dict | None, analysis: dict) -> list[dict]:
    issue = str(prediction.get("prediction_issue") or "")
    model_scores = prediction.get("model_scores") or {}
    fallback_numbers = _as_int_list(prediction.get("recommend_numbers"))
    official_numbers = _as_int_list((official or {}).get("numbers"))
    verification_status = "verified" if official and len(official_numbers) == 20 else "pending_official"
    learned_status = "learned" if verification_status == "verified" else "pending"
    learned_at = datetime.utcnow().isoformat() if learned_status == "learned" else None

    model_names = list(model_scores.keys()) if model_scores else ["ensemble"]
    records = []
    for model_name in model_names:
        candidates = _model_candidates(model_name, model_scores, fallback_numbers)
        for top_n in TOP_N_VALUES:
            top_numbers = candidates[:top_n]
            result = calculate_model_result(top_numbers, official_numbers) if official_numbers else {
                "hit_numbers": [],
                "hit_count": 0,
                "predicted_count": len(top_numbers),
                "precision_score": 0,
                "official_coverage": 0,
            }
            records.append(
                {
                    "issue": issue,
                    "source_issue": prediction.get("issue"),
                    "target_issue": issue,
                    "history_cutoff_issue": prediction.get("issue"),
                    "prediction_created_at": prediction.get("predict_time"),
                    "draw_time": (official or {}).get("draw_time") or prediction.get("predict_time"),
                    "model_name": model_name,
                    "model_version": DEFAULT_MODEL_VERSION,
                    "prediction_type": "live_prediction",
                    "predicted_numbers": top_numbers,
                    "predicted_scores": (model_scores or {}).get(model_name, {}),
                    "model_weight": _model_weight(model_name, model_scores),
                    "official_numbers": official_numbers,
                    "hit_numbers": result["hit_numbers"],
                    "predicted_count": result["predicted_count"],
                    "hit_count": result["hit_count"],
                    "precision_score": result["precision_score"],
                    "official_coverage": result["official_coverage"],
                    "rank_score": _rank_score(result["hit_count"], top_n),
                    "top_n": top_n,
                    "prediction_snapshot": prediction,
                    "analysis_snapshot": analysis,
                    "verification_status": verification_status,
                    "learned_status": learned_status,
                    "learned_at": learned_at,
                    "error_message": None,
                }
            )
    return records


def evaluate_verified_issue(issue: str) -> dict:
    start = time.perf_counter()
    try:
        snapshot = capture_prediction_snapshot(issue)
        if snapshot.get("status") != "ok":
            record = {
                "issue": str(issue),
                "draw_time": None,
                "model_name": "unknown",
                "model_version": DEFAULT_MODEL_VERSION,
                "prediction_type": "live_prediction",
                "predicted_numbers": [],
                "predicted_scores": {},
                "official_numbers": [],
                "hit_numbers": [],
                "predicted_count": 0,
                "hit_count": 0,
                "precision_score": 0,
                "official_coverage": 0,
                "rank_score": 0,
                "top_n": 0,
                "prediction_snapshot": {},
                "analysis_snapshot": {},
                "verification_status": "missing_prediction",
                "learned_status": "missing_snapshot",
                "learned_at": None,
                "error_message": "prediction snapshot not found",
            }
            saved = upsert_learning_record(record)
            return {"status": "missing_snapshot", "issue": issue, "saved": [saved]}

        official = get_official_draw_by_issue(str(issue), verified_only=True)
        existing_records = snapshot.get("learning_records") or []
        if existing_records:
            official_numbers = _as_int_list((official or {}).get("numbers"))
            verification_status = "verified" if official and len(official_numbers) == 20 else "pending_official"
            learned_status = "learned" if verification_status == "verified" else "pending"
            learned_at = datetime.utcnow().isoformat() if learned_status == "learned" else None
            records = []
            for existing in existing_records:
                result = calculate_model_result(existing.get("predicted_numbers") or [], official_numbers) if official_numbers else {
                    "hit_numbers": [],
                    "hit_count": 0,
                    "predicted_count": len(existing.get("predicted_numbers") or []),
                    "precision_score": 0,
                    "official_coverage": 0,
                }
                updated = {
                    **existing,
                    "official_numbers": official_numbers,
                    "hit_numbers": result["hit_numbers"],
                    "predicted_count": result["predicted_count"],
                    "hit_count": result["hit_count"],
                    "precision_score": result["precision_score"],
                    "official_coverage": result["official_coverage"],
                    "rank_score": _rank_score(result["hit_count"], int(existing.get("top_n") or 0)),
                    "verification_status": verification_status,
                    "learned_status": learned_status,
                    "learned_at": learned_at,
                    "draw_time": (official or {}).get("draw_time") or existing.get("draw_time"),
                    "error_message": None,
                }
                records.append(updated)
        else:
            return {"status": "missing_snapshot", "issue": issue, "saved": []}
        saved = [upsert_learning_record(record) for record in records]
        status = "ok" if official else "pending_official"
        record_operation_event(
            component="learning",
            event_type="learning_evaluation",
            status="ok" if status == "ok" else "warning",
            issue=str(issue),
            message=f"learning evaluation {status}",
            duration_ms=_duration_ms(start),
        )
        return {"status": status, "issue": issue, "records": len(records), "saved": saved}
    except Exception as exc:
        logger.exception("learning evaluation failed")
        record_operation_event(
            component="learning",
            event_type="learning_evaluation",
            status="error",
            issue=str(issue),
            message="learning evaluation failed",
            duration_ms=_duration_ms(start),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return {"status": "error", "issue": issue, "error": str(exc)}


def recalculate_issue(issue: str) -> dict:
    return evaluate_verified_issue(issue)


def backfill_learning_records(limit: int = 50) -> dict:
    start = time.perf_counter()
    limit = max(1, min(int(limit or 50), 200))
    processed = []
    for prediction in get_prediction_history_records(limit):
        issue = prediction.get("prediction_issue")
        if not issue:
            continue
        processed.append(evaluate_historical_backtest_issue(str(issue), prediction))
    record_operation_event(
        component="learning",
        event_type="learning_backfill",
        status="ok",
        issue=None,
        message=f"learning backfill processed {len(processed)} predictions",
        duration_ms=_duration_ms(start),
    )
    return {"status": "ok", "processed": len(processed), "results": processed}


def evaluate_historical_backtest_issue(issue: str, prediction: dict | None = None) -> dict:
    try:
        prediction = prediction or _latest_prediction_for_issue(issue)
        if not prediction:
            return {"status": "missing_snapshot", "issue": issue}
        official = get_official_draw_by_issue(str(issue), verified_only=True)
        records = _learning_records_from_prediction(prediction, official, {})
        for record in records:
            record["prediction_type"] = "historical_backtest"
            record["learned_status"] = "learned" if official else "pending"
        saved = [upsert_learning_record(record) for record in records]
        return {"status": "ok" if official else "pending_official", "issue": issue, "records": len(records), "saved": saved}
    except Exception as exc:
        logger.exception("historical backtest evaluation failed")
        return {"status": "error", "issue": issue, "error": str(exc)}


def get_learning_status() -> dict:
    counts = get_learning_status_counts()
    status = "ok"
    if counts.get("failed_records", 0) > 0:
        status = "warning"
    return {
        "status": status,
        "engine_version": ENGINE_VERSION,
        **counts,
    }


def get_model_performance(
    model_name: str | None = None,
    window: int | str = 100,
    top_n: int | None = None,
    prediction_type: str | None = None,
) -> dict:
    rows = get_learning_model_performance(
        model_name=model_name,
        window=window,
        top_n=top_n,
        prediction_type=prediction_type,
    )
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "window": window,
        "top_n": top_n,
        "models": rows,
    }


def get_learning_models_summary() -> dict:
    all_rows = get_learning_model_performance(window="all")
    recent_10 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window=10)
    }
    recent_50 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window=50)
    }
    top_5 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window="all", top_n=5)
    }
    top_10 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window="all", top_n=10)
    }
    top_20 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window="all", top_n=20)
    }

    model_names = sorted(
        {
            item["model_name"]
            for group in (all_rows, recent_10.values(), recent_50.values(), top_5.values(), top_10.values(), top_20.values())
            for item in group
        }
    )
    models = []
    for name in model_names:
        base = next((item for item in all_rows if item["model_name"] == name), {})
        models.append(
            {
                "model_name": name,
                "model_version": base.get("model_version"),
                "sample_size": base.get("sample_size", 0),
                "top_5_average_hits": top_5.get(name, {}).get("average_hits", 0),
                "top_10_average_hits": top_10.get(name, {}).get("average_hits", 0),
                "top_20_average_hits": top_20.get(name, {}).get("average_hits", 0),
                "recent_10_average_hits": recent_10.get(name, {}).get("average_hits", 0),
                "recent_50_average_hits": recent_50.get(name, {}).get("average_hits", 0),
                "precision_score": base.get("precision_score", 0),
                "rank_score": base.get("rank_score", 0),
                "latest_issue": base.get("latest_issue"),
                "latest_learned_at": base.get("latest_learned_at"),
            }
        )
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "models": models,
    }


def get_learning_history(**filters: Any) -> dict:
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "data": get_learning_records(**filters),
    }
