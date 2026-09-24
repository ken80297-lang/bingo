from __future__ import annotations

import logging
import copy
import threading
import time
from datetime import datetime
from typing import Any

from database.analysis_store import get_analysis_history
from database.adaptive_weight_store import (\n    get_adaptive_weights_by_source_issue,\n    get_latest_adaptive_weights,\n    save_adaptive_weights,\n)
from database.learning_store import (
    get_learning_model_performance,
    get_learning_records,
    get_learning_summary_records,
    get_learning_status_counts,
    upsert_learning_record,
)
from database.official_draw_store import get_official_draw_by_issue
from database.prediction_history_store import (
    get_prediction_history_records,
    get_prediction_history_statistics,
)
from services.analysis_engine import analysis_engine_status
from services.catch_up_service import get_catch_up_status
from services.operations_center import record_operation_event
from services.official_verification import official_statistics

logger = logging.getLogger(__name__)

ENGINE_VERSION = "22.1"
OBSERVATION_VERSION = "22.1.5"
OBSERVATION_CACHE_TTL_SECONDS = 30
LEARNING_STATUS_CACHE_TTL_SECONDS = 60
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
_LEARNING_STATUS_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}
_LEARNING_STATUS_CACHE_LOCK = threading.Lock()


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _default_learning_status_snapshot(reason: str) -> dict:
    return {
        "status": "unknown",
        "engine_version": ENGINE_VERSION,
        "observation_version": OBSERVATION_VERSION,
        "stale": True,
        "partial": True,
        "total_records": None,
        "live_prediction_count": None,
        "historical_backtest_count": None,
        "learned_records": None,
        "pending_records": None,
        "pending_official_records": None,
        "pending_target_records": None,
        "resolved_pending_records": None,
        "missing_snapshot_records": None,
        "failed_records": None,
        "evaluation_error_records": None,
        "latest_snapshot_issue": None,
        "latest_snapshot_at": None,
        "latest_learned_issue": None,
        "latest_learned_at": None,
        "latest_official_issue": None,
        "official_lag_issues": None,
        "model_count": None,
        "live_target_count": None,
        "complete_live_target_count": None,
        "incomplete_live_target_count": None,
        "duplicate_risk_count": None,
        "snapshot_success_rate": None,
        "learning_success_rate": None,
        "pending_learning": None,
        "verified_waiting_learning": None,
        "last_learning_time": None,
        "readiness_status": "unknown",
        "ready_for_phase_22_2": False,
        "readiness_reasons": [reason],
        "cache": {
            "status": reason,
            "ttl_seconds": LEARNING_STATUS_CACHE_TTL_SECONDS,
            "expires_in_seconds": 0,
        },
    }


def _cached_learning_status() -> dict | None:
    now = time.monotonic()
    with _LEARNING_STATUS_CACHE_LOCK:
        cached = _LEARNING_STATUS_CACHE.get("payload")
        expires_at = float(_LEARNING_STATUS_CACHE.get("expires_at") or 0)
        if cached is None or expires_at <= now:
            return None
        payload = copy.deepcopy(cached)
        payload["cache"] = {
            "status": "hit",
            "ttl_seconds": LEARNING_STATUS_CACHE_TTL_SECONDS,
            "expires_in_seconds": round(expires_at - now, 3),
        }
        return payload


def _store_learning_status_cache(payload: dict) -> None:
    if payload.get("status") == "error":
        return
    with _LEARNING_STATUS_CACHE_LOCK:
        _LEARNING_STATUS_CACHE["payload"] = copy.deepcopy(payload)
        _LEARNING_STATUS_CACHE["expires_at"] = time.monotonic() + LEARNING_STATUS_CACHE_TTL_SECONDS


def _stale_learning_status() -> dict | None:
    with _LEARNING_STATUS_CACHE_LOCK:
        cached = _LEARNING_STATUS_CACHE.get("payload")
        if cached is None:
            return None
        payload = copy.deepcopy(cached)
        payload["cache"] = {
            "status": "stale",
            "ttl_seconds": LEARNING_STATUS_CACHE_TTL_SECONDS,
            "expires_in_seconds": 0,
        }
        return payload


def get_learning_status_snapshot() -> dict:
    """Return the last known learning status without database or external work."""
    acquired = _LEARNING_STATUS_CACHE_LOCK.acquire(blocking=False)
    if not acquired:
        return _default_learning_status_snapshot("lock_unavailable")
    try:
        cached = _LEARNING_STATUS_CACHE.get("payload")
        expires_at = float(_LEARNING_STATUS_CACHE.get("expires_at") or 0)
        if cached is None:
            return _default_learning_status_snapshot("cache_empty")
        payload = copy.deepcopy(cached)
    finally:
        _LEARNING_STATUS_CACHE_LOCK.release()

    now = time.monotonic()
    expired = expires_at <= now
    payload["stale"] = bool(expired)
    payload["partial"] = bool(payload.get("partial", False))
    payload["cache"] = {
        "status": "snapshot_stale" if expired else "snapshot_hit",
        "ttl_seconds": LEARNING_STATUS_CACHE_TTL_SECONDS,
        "expires_in_seconds": round(max(0.0, expires_at - now), 3),
    }
    return payload


def invalidate_learning_status_cache() -> None:
    with _LEARNING_STATUS_CACHE_LOCK:
        _LEARNING_STATUS_CACHE["payload"] = None
        _LEARNING_STATUS_CACHE["expires_at"] = 0.0


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


def _valid_learning_snapshot_record(record: dict) -> bool:
    model_name = str(record.get("model_name") or "")
    try:
        top_n = int(record.get("top_n") or 0)
        predicted_count = int(record.get("predicted_count") or len(record.get("predicted_numbers") or []))
    except Exception:
        return False
    return (
        model_name in EXPECTED_LIVE_MODELS
        and top_n in EXPECTED_TOP_N
        and predicted_count > 0
        and bool(record.get("predicted_numbers"))
        and bool(record.get("prediction_snapshot"))
    )


def _is_complete_learning_record_set(records: list[dict]) -> bool:
    valid_records = [record for record in records if _valid_learning_snapshot_record(record)]
    valid_combos = {
        (str(record.get("model_name") or ""), int(record.get("top_n") or 0))
        for record in valid_records
    }
    expected_combos = {
        (model_name, top_n)
        for model_name in EXPECTED_LIVE_MODELS
        for top_n in EXPECTED_TOP_N
    }
    return (
        len(records) == EXPECTED_RECORDS_PER_TARGET
        and len(valid_records) == EXPECTED_RECORDS_PER_TARGET
        and valid_combos == expected_combos
    )


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
    return get_learning_summary_records(limit=limit, prediction_type="live_prediction")


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
        valid_records = [record for record in records if _valid_learning_snapshot_record(record)]
        if _is_complete_learning_record_set(records):
            first = valid_records[0]
            return {
                "status": "ok",
                "issue": str(issue),
                "prediction_snapshot": first.get("prediction_snapshot") or {},
                "analysis_snapshot": first.get("analysis_snapshot") or {},
                "learning_records": valid_records,
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
    valid_existing = [record for record in existing if _valid_learning_snapshot_record(record)]
    if _is_complete_learning_record_set(existing):
        return {
            "status": "ok",
            "skipped": True,
            "message": "complete live prediction snapshot already exists",
            "source_issue": source_issue,
            "target_issue": target_issue,
            "history_cutoff_issue": valid_existing[0].get("history_cutoff_issue"),
            "prediction_created_at": valid_existing[0].get("prediction_created_at"),
            "records": len(valid_existing),
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

    model_names = [
        model_name
        for model_name in EXPECTED_LIVE_MODELS
        if model_name != "ensemble" and model_name in model_scores
    ]
    # Recovery is allowed to produce formal learned rows only when the
    # immutable prediction_history row contains all five V7 model outputs.
    # Older fast-path rows often contain only production_fast_path (or no
    # per-model scores); those rows are evidence of a prediction, not evidence
    # of six-model learning.
    expected_base_models = [name for name in EXPECTED_LIVE_MODELS if name != "ensemble"]
    if set(model_names) != set(expected_base_models):
        return []

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

    ensemble_numbers = fallback_numbers
    for top_n in TOP_N_VALUES:
        top_numbers = ensemble_numbers[:top_n]
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
                "model_name": "ensemble",
                "model_version": DEFAULT_MODEL_VERSION,
                "prediction_type": "live_prediction",
                "predicted_numbers": top_numbers,
                "predicted_scores": {"recovered_from": "prediction_history"},
                "model_weight": {"weight": 1.0},
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


V7_ADAPTIVE_MODELS = ("laowanjia", "hotcold", "missing", "pattern", "balance")
V7_ADAPTIVE_MIN_SAMPLES = 20
V7_ADAPTIVE_WINDOW = 100


def update_v7_adaptive_weights(source_issue: str) -> dict:
    existing = get_adaptive_weights_by_source_issue(str(source_issue))
    if existing:
        return {
            "status": "ok",
            "reason": "already_updated",
            "source_issue": str(source_issue),
            "version": existing.get("version"),
            "weight_id": existing.get("id"),
        }

    # Build the adaptive window only from strict, complete 18/18 verified+learned
    # target issues. Fetch enough rows for 100 complete targets (18 each) and
    # reject every target that does not satisfy the same snapshot contract used
    # by the realtime learning path.
    strict_rows = get_learning_records(
        limit=500,
        prediction_type="live_prediction",
        verification_status="verified",
        learned_status="learned",
    )
    by_issue: dict[str, list[dict]] = {}
    for row in strict_rows:
        by_issue.setdefault(str(row.get("issue") or ""), []).append(row)
    complete_issues = [
        issue for issue, rows in by_issue.items()
        if issue and _is_complete_learning_record_set(rows)
    ]
    complete_issues = sorted(complete_issues, key=lambda value: int(value) if value.isdigit() else -1, reverse=True)
    complete_issues = complete_issues[:V7_ADAPTIVE_WINDOW]
    if len(complete_issues) < V7_ADAPTIVE_MIN_SAMPLES:
        return {"status": "skipped", "reason": "insufficient_complete_targets", "complete_targets": len(complete_issues)}

    selected = [row for issue in complete_issues for row in by_issue[issue]]
    top20 = [row for row in selected if int(row.get("top_n") or 0) == 20 and row.get("model_name") in V7_ADAPTIVE_MODELS]
    by_model: dict[str, list[dict]] = {name: [] for name in V7_ADAPTIVE_MODELS}
    for row in top20:
        by_model[str(row.get("model_name"))].append(row)
    minimum = min(len(by_model[name]) for name in V7_ADAPTIVE_MODELS)
    if minimum < V7_ADAPTIVE_MIN_SAMPLES:
        return {"status": "skipped", "reason": "insufficient_samples", "minimum_samples": minimum}

    scores = {
        name: sum(float(row.get("hit_count") or 0) for row in by_model[name]) / len(by_model[name])
        for name in V7_ADAPTIVE_MODELS
    }
    peer_mean = sum(scores.values()) / len(scores) if scores else 0.0
    raw = {name: 1.0 + ((score - peer_mean) / max(1.0, peer_mean)) * 0.5 for name, score in scores.items()}
    raw = {name: max(0.8, min(1.2, value)) for name, value in raw.items()}
    normalizer = sum(raw.values()) / len(raw) or 1.0
    weights = {name: round(value / normalizer, 6) for name, value in raw.items()}

    previous = get_latest_adaptive_weights() or {}
    version = int(previous.get("version") or 0) + 1
    payload = {
        "version": version,
        "strategy": "v7_models",
        "window": len(complete_issues),
        "laowanjia_weight": weights["laowanjia"],
        "hot_cold_weight": weights["hotcold"],
        "missing_weight": weights["missing"],
        "pattern_weight": weights["pattern"],
        "balance_weight": weights["balance"],
        "tail_weight": None,
        "random_weight": None,
        "average_hits": round(peer_mean, 4),
        "hit_rate": round(peer_mean / 20.0, 6),
        "source_evaluation_id": int(str(source_issue)),
        "is_active": True,
    }
    saved = save_adaptive_weights(payload)
    if saved.get("status") != "ok" or saved.get("storage") != "cloud":
        return {"status": "error", "reason": "adaptive_weight_cloud_save_required", "save": saved}
    return {
        "status": "ok",
        "source_issue": str(source_issue),
        "version": version,
        "complete_targets": len(complete_issues),
        "weights": weights,
        "save": saved,
    }

