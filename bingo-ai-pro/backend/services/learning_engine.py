from __future__ import annotations

import logging
import copy
import threading
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
from services.analysis_engine import analysis_engine_status
from services.catch_up_service import get_catch_up_status
from services.operations_center import record_operation_event
from services.official_verification import official_statistics

logger = logging.getLogger(__name__)

ENGINE_VERSION = "22.1"
OBSERVATION_VERSION = "22.1.5"
OBSERVATION_CACHE_TTL_SECONDS = 30
DEFAULT_MODEL_VERSION = "v7"
TOP_N_VALUES = (5, 10, 20)
EXPECTED_LIVE_MODELS = {"laowanjia", "hotcold", "missing", "pattern", "balance", "ensemble"}
EXPECTED_TOP_N = {5, 10, 20}
EXPECTED_RECORDS_PER_TARGET = len(EXPECTED_LIVE_MODELS) * len(EXPECTED_TOP_N)
LEARNING_READINESS_THRESHOLDS = {
    "minimum_learned_targets": 100,
    "minimum_model_samples": 100,
    "minimum_complete_rate": 0.99,
    "maximum_missing_rate": 0.01,
    "maximum_evaluation_errors": 0,
    "maximum_duplicate_risk": 0,
    "maximum_official_lag": 3,
}
_OBSERVATION_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_OBSERVATION_CACHE_LOCK = threading.Lock()


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


def _issue_int(value: Any) -> int | None:
    try:
        text = str(value or "")
        if text.startswith("pending:"):
            text = text.split(":", 1)[1]
        if text.upper().startswith("TEST"):
            return None
        return int(text)
    except Exception:
        return None


def _safe_lag(newer: Any, older: Any) -> int | None:
    newer_int = _issue_int(newer)
    older_int = _issue_int(older)
    if newer_int is None or older_int is None:
        return None
    return max(0, newer_int - older_int)


def _learning_scope_records(limit: int = 500) -> list[dict]:
    return get_learning_records(limit=limit, prediction_type="live_prediction")


def _target_key(record: dict) -> str:
    issue = str(record.get("issue") or "")
    if issue:
        return issue
    target = record.get("target_issue")
    if target:
        return str(target)
    source = record.get("source_issue")
    return f"pending:{source}" if source else "unknown"


def _group_live_targets(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if record.get("prediction_type") != "live_prediction":
            continue
        model_name = str(record.get("model_name") or "")
        if model_name == "unknown":
            continue
        grouped.setdefault(_target_key(record), []).append(record)
    return grouped


def _target_quality(target_issue: str, records: list[dict]) -> dict:
    combos: dict[tuple[str, int], int] = {}
    models = set()
    top_n_values = set()
    duplicate_count = 0
    prediction_created_at_missing = False
    source_missing = False
    target_missing = False
    error_status = False
    for record in records:
        model_name = str(record.get("model_name") or "")
        top_n = int(record.get("top_n") or 0)
        combo = (model_name, top_n)
        combos[combo] = combos.get(combo, 0) + 1
        if combos[combo] > 1:
            duplicate_count += 1
        if model_name:
            models.add(model_name)
        if top_n:
            top_n_values.add(top_n)
        if not record.get("prediction_created_at"):
            prediction_created_at_missing = True
        if not record.get("source_issue"):
            source_missing = True
        if not record.get("target_issue") and not str(target_issue).startswith("pending:"):
            target_missing = True
        if record.get("learned_status") == "error":
            error_status = True

    missing_models = sorted(EXPECTED_LIVE_MODELS - models)
    missing_top_n = sorted(EXPECTED_TOP_N - top_n_values)
    is_complete = (
        len(records) == EXPECTED_RECORDS_PER_TARGET
        and not missing_models
        and not missing_top_n
        and duplicate_count == 0
        and not prediction_created_at_missing
        and not source_missing
        and not target_missing
        and not error_status
    )
    reasons = []
    if len(records) != EXPECTED_RECORDS_PER_TARGET:
        reasons.append(f"record_count is {len(records)}, expected {EXPECTED_RECORDS_PER_TARGET}")
    if missing_models:
        reasons.append("missing models: " + ", ".join(missing_models))
    if missing_top_n:
        reasons.append("missing top_n: " + ", ".join(map(str, missing_top_n)))
    if duplicate_count:
        reasons.append(f"duplicate combinations: {duplicate_count}")
    if prediction_created_at_missing:
        reasons.append("missing prediction_created_at")
    if source_missing:
        reasons.append("missing source_issue")
    if target_missing:
        reasons.append("missing target_issue")
    if error_status:
        reasons.append("learned_status contains error")
    return {
        "target_issue": target_issue,
        "record_count": len(records),
        "model_count": len(models),
        "missing_models": missing_models,
        "missing_top_n": missing_top_n,
        "duplicate_count": duplicate_count,
        "status": "complete" if is_complete else "incomplete",
        "reason": "; ".join(reasons) if reasons else "complete live target",
    }


def _trend(recent_10: float, recent_50: float, sample_size: int) -> tuple[str, float]:
    if sample_size < 20:
        return "insufficient_data", 0
    delta = round((recent_10 or 0) - (recent_50 or 0), 2)
    if delta > 0.25:
        return "improving", delta
    if delta < -0.25:
        return "declining", delta
    return "stable", delta


def _cached_observation() -> dict | None:
    now = time.monotonic()
    with _OBSERVATION_CACHE_LOCK:
        cached = _OBSERVATION_CACHE.get("payload")
        expires_at = float(_OBSERVATION_CACHE.get("expires_at") or 0)
        if cached is None or expires_at <= now:
            return None
        payload = copy.deepcopy(cached)
        payload["cache"] = {
            "status": "hit",
            "ttl_seconds": OBSERVATION_CACHE_TTL_SECONDS,
            "expires_in_seconds": round(expires_at - now, 3),
        }
        return payload


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
    try:
        counts = get_learning_status_counts()
        observation = _cached_observation()
        records = (observation or {}).get("records") or {}
        targets = (observation or {}).get("targets") or {}
        quality = (observation or {}).get("quality") or {}
        readiness = (observation or {}).get("readiness") or {}
        status = "ok"
        if int(counts.get("failed_records") or 0) > 0:
            status = "warning"
        return {
            "status": (observation or {}).get("status", status),
            "engine_version": ENGINE_VERSION,
            "observation_version": OBSERVATION_VERSION,
            "total_records": int(counts.get("total_records") or records.get("total") or 0),
            "live_prediction_count": int(counts.get("live_prediction_count") or records.get("live") or 0),
            "historical_backtest_count": int(counts.get("historical_backtest_count") or records.get("historical") or 0),
            "learned_records": int(counts.get("learned_records") or records.get("learned") or 0),
            "pending_records": int(counts.get("pending_records") or records.get("pending") or 0),
            "pending_official_records": records.get("pending_official", 0),
            "pending_target_records": records.get("pending_target", 0),
            "resolved_pending_records": records.get("resolved_pending", 0),
            "missing_snapshot_records": int(counts.get("missing_snapshot_records") or records.get("missing_snapshot") or 0),
            "failed_records": int(counts.get("failed_records") or records.get("failed") or 0),
            "evaluation_error_records": quality.get("evaluation_error_count", 0),
            "latest_snapshot_issue": records.get("latest_snapshot_issue"),
            "latest_snapshot_at": records.get("latest_snapshot_at"),
            "latest_learned_issue": records.get("latest_learned_issue") or counts.get("latest_learned_issue"),
            "latest_learned_at": records.get("latest_learned_at") or counts.get("latest_learned_at"),
            "latest_official_issue": (observation or {}).get("pipeline", {}).get("official_latest_issue"),
            "official_lag_issues": (observation or {}).get("pipeline", {}).get("official_lag_issues"),
            "model_count": int(counts.get("model_count") or records.get("model_count") or 0),
            "live_target_count": targets.get("live_target_count", 0),
            "complete_live_target_count": targets.get("complete_live_target_count", 0),
            "incomplete_live_target_count": targets.get("incomplete_live_target_count", 0),
            "duplicate_risk_count": quality.get("duplicate_risk_count", 0),
            "snapshot_success_rate": quality.get("snapshot_success_rate", 0),
            "learning_success_rate": quality.get("learning_success_rate", 0),
            "readiness_status": readiness.get("status", "unknown"),
            "ready_for_phase_22_2": bool(readiness.get("ready")),
            "readiness_reasons": readiness.get("reasons", []),
            "observation_cache": (observation or {}).get("cache", {"status": "miss_not_computed"}),
        }
    except Exception as exc:
        logger.exception("learning status failed")
        return {
            "status": "error",
            "engine_version": ENGINE_VERSION,
            "observation_version": OBSERVATION_VERSION,
            "error": str(exc),
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
        "observation_version": OBSERVATION_VERSION,
        "window": window,
        "top_n": top_n,
        "sample_unit": "learning_history_records",
        "target_sample_note": "Use /api/learning/models for target_sample_count by model.",
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
    recent_20 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window=20)
    }
    recent_100 = {
        item["model_name"]: item
        for item in get_learning_model_performance(window=100)
    }
    live_records = _learning_scope_records(500)
    learned_records = [item for item in live_records if item.get("learned_status") == "learned"]
    target_samples_by_model: dict[str, set[str]] = {}
    learned_targets_by_model: dict[str, set[str]] = {}
    pending_targets_by_model: dict[str, set[str]] = {}
    top_counts_by_model: dict[str, dict[int, int]] = {}
    error_counts_by_model: dict[str, int] = {}
    latest_by_model: dict[str, dict] = {}
    for item in live_records:
        model_name = str(item.get("model_name") or "")
        if not model_name or model_name == "unknown":
            continue
        target = _target_key(item)
        target_samples_by_model.setdefault(model_name, set()).add(target)
        if item.get("learned_status") == "learned":
            learned_targets_by_model.setdefault(model_name, set()).add(target)
        else:
            pending_targets_by_model.setdefault(model_name, set()).add(target)
        top_n = int(item.get("top_n") or 0)
        top_counts_by_model.setdefault(model_name, {})
        top_counts_by_model[model_name][top_n] = top_counts_by_model[model_name].get(top_n, 0) + 1
        if item.get("learned_status") in ("failed", "error") or item.get("verification_status") in ("error", "evaluation_error"):
            error_counts_by_model[model_name] = error_counts_by_model.get(model_name, 0) + 1
        latest_by_model.setdefault(model_name, item)

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
        recent_10_hits = recent_10.get(name, {}).get("average_hits", 0)
        recent_50_hits = recent_50.get(name, {}).get("average_hits", 0)
        trend_status, trend_delta = _trend(
            float(recent_10_hits or 0),
            float(recent_50_hits or 0),
            int(base.get("sample_size", 0) or 0),
        )
        models.append(
            {
                "model_name": name,
                "model_version": base.get("model_version"),
                "record_count": sum(top_counts_by_model.get(name, {}).values()),
                "sample_size": base.get("sample_size", 0),
                "target_sample_count": len(target_samples_by_model.get(name, set())),
                "learned_target_count": len(learned_targets_by_model.get(name, set())),
                "pending_target_count": len(pending_targets_by_model.get(name, set())),
                "top_5_sample_count": top_counts_by_model.get(name, {}).get(5, 0),
                "top_10_sample_count": top_counts_by_model.get(name, {}).get(10, 0),
                "top_20_sample_count": top_counts_by_model.get(name, {}).get(20, 0),
                "top_5_average_hits": top_5.get(name, {}).get("average_hits", 0),
                "top_10_average_hits": top_10.get(name, {}).get("average_hits", 0),
                "top_20_average_hits": top_20.get(name, {}).get("average_hits", 0),
                "top_5_precision": top_5.get(name, {}).get("precision_score", 0),
                "top_10_precision": top_10.get(name, {}).get("precision_score", 0),
                "top_20_precision": top_20.get(name, {}).get("precision_score", 0),
                "top_5_rank_score": top_5.get(name, {}).get("rank_score", 0),
                "top_10_rank_score": top_10.get(name, {}).get("rank_score", 0),
                "top_20_rank_score": top_20.get(name, {}).get("rank_score", 0),
                "recent_10_average_hits": recent_10_hits,
                "recent_20_average_hits": recent_20.get(name, {}).get("average_hits", 0),
                "recent_50_average_hits": recent_50_hits,
                "recent_100_average_hits": recent_100.get(name, {}).get("average_hits", 0),
                "all_time_average_hits": base.get("average_hits", 0),
                "precision_score": base.get("precision_score", 0),
                "rank_score": base.get("rank_score", 0),
                "evaluation_error_count": error_counts_by_model.get(name, 0),
                "latest_issue": base.get("latest_issue") or (latest_by_model.get(name) or {}).get("issue"),
                "latest_learned_at": base.get("latest_learned_at"),
                "trend": trend_status,
                "trend_delta": trend_delta,
            }
        )
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "observation_version": OBSERVATION_VERSION,
        "sample_unit": "target_sample_count counts distinct live target issues; record_count counts Top N rows.",
        "models": models,
    }


def get_learning_history(**filters: Any) -> dict:
    return {
        "status": "ok",
        "engine_version": ENGINE_VERSION,
        "data": get_learning_records(**filters),
    }


def _build_learning_observation() -> dict:
    try:
        base_counts = get_learning_status_counts()
        records = _learning_scope_records(500)
        all_records = get_learning_records(limit=500)
        grouped_targets = _group_live_targets(records)
        target_rows = [_target_quality(target, items) for target, items in grouped_targets.items()]
        complete_targets = [item for item in target_rows if item["status"] == "complete"]
        incomplete_targets = [item for item in target_rows if item["status"] != "complete"]
        duplicate_risk_count = sum(item["duplicate_count"] for item in target_rows)
        evaluation_error_count = sum(
            1
            for item in all_records
            if item.get("learned_status") in ("failed", "error")
            or item.get("verification_status") in ("error", "evaluation_error")
        )
        learned_target_count = len(
            {
                _target_key(item)
                for item in records
                if item.get("learned_status") == "learned" and str(item.get("model_name") or "") != "unknown"
            }
        )
        missing_snapshot_records = [
            item for item in all_records if item.get("learned_status") == "missing_snapshot"
        ]
        live_count = int(base_counts.get("live_prediction_count") or 0)
        learned_records = int(base_counts.get("learned_records") or 0)
        pending_official = sum(1 for item in all_records if item.get("verification_status") == "pending_official")
        pending_target = sum(1 for item in all_records if item.get("verification_status") == "pending_target_issue")
        resolved_pending = sum(1 for item in all_records if item.get("learned_status") == "resolved_to_target")
        failed_records = sum(1 for item in all_records if item.get("learned_status") in ("failed", "error"))
        historical = sum(1 for item in all_records if item.get("prediction_type") == "historical_backtest")
        latest_snapshot = next((item for item in records if str(item.get("model_name") or "") != "unknown"), None)
        learned_items = [item for item in records if item.get("learned_status") == "learned" and str(item.get("model_name") or "") != "unknown"]
        latest_learned = learned_items[0] if learned_items else None
        model_names = {
            str(item.get("model_name"))
            for item in records
            if item.get("model_name") and str(item.get("model_name")) != "unknown"
        }

        official = official_statistics()
        catch_up = get_catch_up_status(fetch_source=False)
        analysis = analysis_engine_status()
        collector_latest = catch_up.get("database_latest_issue") or official.get("latest_kuaishou_issue")
        official_latest = official.get("latest_official_issue")
        official_lag = _safe_lag(collector_latest, official_latest)
        complete_rate = round(len(complete_targets) / len(grouped_targets), 4) if grouped_targets else 0
        missing_rate = round(len(missing_snapshot_records) / max(1, live_count), 4) if live_count else 0
        snapshot_success_rate = round(complete_rate * 100, 2)
        learning_success_rate = round((learned_target_count / len(grouped_targets)) * 100, 2) if grouped_targets else 0

        readiness_reasons = []
        ready = True
        readiness_status = "ready_for_phase_22_2"
        if evaluation_error_count > LEARNING_READINESS_THRESHOLDS["maximum_evaluation_errors"]:
            readiness_status = "error"
            readiness_reasons.append(f"evaluation errors detected: {evaluation_error_count}")
            ready = False
        if duplicate_risk_count > LEARNING_READINESS_THRESHOLDS["maximum_duplicate_risk"]:
            readiness_status = "warning" if readiness_status != "error" else readiness_status
            readiness_reasons.append(f"duplicate risk detected: {duplicate_risk_count}")
            ready = False
        if official_lag is not None and official_lag > LEARNING_READINESS_THRESHOLDS["maximum_official_lag"]:
            readiness_status = "waiting_official" if readiness_status not in ("error", "warning") else readiness_status
            readiness_reasons.append(f"official data is {official_lag} issues behind collector")
            ready = False
        if len(grouped_targets) == 0:
            readiness_status = "collecting"
            readiness_reasons.append("waiting for live prediction snapshots")
            ready = False
        if complete_rate < LEARNING_READINESS_THRESHOLDS["minimum_complete_rate"]:
            readiness_status = "warning" if readiness_status == "ready_for_phase_22_2" else readiness_status
            readiness_reasons.append(f"complete live target rate is {complete_rate:.2%}")
            ready = False
        if missing_rate > LEARNING_READINESS_THRESHOLDS["maximum_missing_rate"]:
            readiness_status = "warning" if readiness_status == "ready_for_phase_22_2" else readiness_status
            readiness_reasons.append(f"missing snapshot rate is {missing_rate:.2%}")
            ready = False
        if learned_target_count < LEARNING_READINESS_THRESHOLDS["minimum_learned_targets"]:
            readiness_status = "insufficient_samples" if readiness_status == "ready_for_phase_22_2" else readiness_status
            readiness_reasons.append(
                f"learned live target count {learned_target_count} is below {LEARNING_READINESS_THRESHOLDS['minimum_learned_targets']}"
            )
            ready = False
        model_summary = get_learning_models_summary().get("models", [])
        under_sampled = [
            item.get("model_name")
            for item in model_summary
            if int(item.get("learned_target_count") or 0) < LEARNING_READINESS_THRESHOLDS["minimum_model_samples"]
        ]
        if under_sampled:
            readiness_status = "insufficient_samples" if readiness_status == "ready_for_phase_22_2" else readiness_status
            readiness_reasons.append("models below learned sample threshold: " + ", ".join(map(str, under_sampled)))
            ready = False
        if not readiness_reasons:
            readiness_reasons.append("all readiness checks passed")

        return {
            "status": "ok",
            "engine_version": OBSERVATION_VERSION,
            "pipeline": {
                "collector_latest_issue": collector_latest,
                "official_latest_issue": official_latest,
                "analysis_latest_issue": analysis.get("latest_issue"),
                "learning_latest_snapshot_issue": (latest_snapshot or {}).get("issue"),
                "learning_latest_learned_issue": (latest_learned or {}).get("issue"),
                "official_lag_issues": official_lag,
            },
            "records": {
                "total": int(base_counts.get("total_records") or len(all_records)),
                "live": live_count,
                "historical": int(base_counts.get("historical_backtest_count") or historical),
                "learned": learned_records,
                "pending": int(base_counts.get("pending_records") or sum(1 for item in all_records if item.get("learned_status") == "pending")),
                "pending_official": pending_official,
                "pending_target": pending_target,
                "resolved_pending": resolved_pending,
                "missing_snapshot": len(missing_snapshot_records),
                "failed": failed_records,
                "model_count": len(model_names),
                "latest_snapshot_issue": (latest_snapshot or {}).get("issue"),
                "latest_snapshot_at": (latest_snapshot or {}).get("prediction_created_at"),
                "latest_learned_issue": (latest_learned or {}).get("issue"),
                "latest_learned_at": (latest_learned or {}).get("learned_at"),
            },
            "targets": {
                "live_target_count": len(grouped_targets),
                "complete_live_target_count": len(complete_targets),
                "incomplete_live_target_count": len(incomplete_targets),
                "incomplete_targets": incomplete_targets[:20],
            },
            "quality": {
                "snapshot_success_rate": snapshot_success_rate,
                "learning_success_rate": learning_success_rate,
                "duplicate_risk_count": duplicate_risk_count,
                "evaluation_error_count": evaluation_error_count,
                "missing_snapshot_rate": round(missing_rate * 100, 2),
                "complete_live_target_rate": round(complete_rate * 100, 2),
            },
            "readiness": {
                "status": readiness_status,
                "ready": bool(ready),
                "thresholds": LEARNING_READINESS_THRESHOLDS,
                "reasons": readiness_reasons,
            },
            "models": model_summary,
        }
    except Exception as exc:
        logger.exception("learning observation failed")
        return {
            "status": "error",
            "engine_version": OBSERVATION_VERSION,
            "error": str(exc),
            "pipeline": {},
            "records": {},
            "targets": {},
            "quality": {},
            "readiness": {
                "status": "error",
                "ready": False,
                "reasons": [str(exc)],
            },
            "models": [],
        }


def get_learning_observation(force_refresh: bool = False) -> dict:
    if not force_refresh:
        cached_payload = _cached_observation()
        if cached_payload is not None:
            return cached_payload

    payload = _build_learning_observation()
    if payload.get("status") != "error":
        with _OBSERVATION_CACHE_LOCK:
            _OBSERVATION_CACHE["payload"] = copy.deepcopy(payload)
            _OBSERVATION_CACHE["expires_at"] = time.monotonic() + OBSERVATION_CACHE_TTL_SECONDS
        payload["cache"] = {
            "status": "miss",
            "ttl_seconds": OBSERVATION_CACHE_TTL_SECONDS,
            "expires_in_seconds": OBSERVATION_CACHE_TTL_SECONDS,
        }
    else:
        payload["cache"] = {
            "status": "bypass_error",
            "ttl_seconds": OBSERVATION_CACHE_TTL_SECONDS,
            "expires_in_seconds": 0,
        }
    return payload
