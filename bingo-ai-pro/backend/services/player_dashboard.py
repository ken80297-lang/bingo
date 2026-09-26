from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from database.collector_store import get_latest_kuaishou_summary as get_latest_kuaishou_snapshot
from database.analysis_store import get_latest_analysis_history
from database.official_draw_store import get_latest_official_draw_summary as get_latest_official_draw
from database.official_draw_store import get_official_draw_summary_by_issue as get_official_draw_by_issue
from database.operations_store import get_latest_operation_event
from database.prediction_history_store import get_prediction_history_summary_records as get_prediction_history_records
from database.prediction_history_store import get_latest_prediction_context
from database.prediction_history_store import get_previous_verification_summary_snapshot
from database.prediction_history_store import get_prediction_summary_for_source_target as get_prediction_for_source_target
from database.prediction_history_store import get_prediction_lifecycle_aggregates
from database.prediction_history_store import is_production_prediction
from config.production_scope import production_scope_payload
from database.release_store import get_current_release
from services.dashboard_card_schema import (
    confidence_percent,
    confidence_ratio,
    high_probability_numbers,
    odd_even_prediction,
    size_prediction,
    super_candidates,
    validation_diagnostics,
)
from services.prediction_refresh import prediction_refresh_status

try:
    from database.rule_snapshot_store import get_rule_snapshot, get_rule_snapshot_with_timing
except ModuleNotFoundError:  # pragma: no cover - deployed Phase 30 can run without pending rule snapshot files.
    get_rule_snapshot = None
    get_rule_snapshot_with_timing = None

try:
    from services.rule_snapshot import build_rule_snapshot, get_rule_registry
except ModuleNotFoundError:  # pragma: no cover - fallback keeps dashboard read layer deployable.
    def get_rule_registry() -> list[dict]:
        return [
            {"key": key, "label": label, "family": "dashboard", "source_fields": []}
            for key, label in (
                ("hot", "熱門"),
                ("cold", "冷門"),
                ("missing", "遺漏"),
                ("repeat", "重複"),
                ("tail", "尾數"),
                ("gap", "間距"),
                ("cluster", "群聚"),
                ("diagonal", "斜線"),
                ("super", "超級獎"),
                ("laowanjia", "老玩家"),
            )
        ]

    def build_rule_snapshot(
        analysis: dict | None,
        prediction: dict | None = None,
        *,
        source_issue: str | None = None,
        target_issue: str | None = None,
    ) -> dict:
        source = analysis or {}
        forecast = prediction or {}
        rules = []
        for item in get_rule_registry():
            key = item["key"]
            if key == "hot":
                candidates = _as_int_list(source.get("hot_numbers"))[:10]
            elif key == "cold":
                candidates = _as_int_list(source.get("cold_numbers"))[:10]
            elif key == "missing":
                candidates = _as_int_list(source.get("missing_numbers"))[:10]
            elif key == "repeat":
                candidates = _as_int_list(source.get("repeated_numbers"))[:10]
            elif key == "super":
                candidates = _as_int_list(
                    ((source.get("ai_score") or {}).get("super_number_trajectory_recovery") or {}).get("candidate_numbers")
                    or forecast.get("super_candidates")
                    or forecast.get("super_number_candidates")
                )[:10]
            else:
                candidates = _as_int_list(forecast.get("recommend_numbers") or forecast.get("main_numbers"))[:10]
            rules.append(
                {
                    "key": key,
                    "label": item["label"],
                    "status": "ready" if candidates else "insufficient",
                    "score": source.get(f"{key}_score"),
                    "confidence": source.get(f"{key}_score"),
                    "candidate_numbers": candidates,
                    "reason": "dashboard fallback",
                }
            )
        ready = [item for item in rules if item.get("status") == "ready"]
        return {
            "source_issue": source_issue,
            "target_issue": target_issue,
            "rules": rules,
            "aggregate": {
                "completed_count": len(ready),
                "total_count": len(rules),
                "primary_rules": [item["key"] for item in ready[:5]],
            },
        }

logger = logging.getLogger(__name__)

PLAYER_SUMMARY_TTL_SECONDS = 60
PLAYER_AGGREGATE_CACHE_TTL_SECONDS = 300
PLAYER_DASHBOARD_QUERY_TIMEOUT_SECONDS = 2
PLAYER_DASHBOARD_TOTAL_BUDGET_SECONDS = 4.5
PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS = 2.0
PLAYER_DASHBOARD_OPTIONAL_TIMEOUT_SECONDS = 1.0
# Aggregates are part of dashboard consistency, not a best-effort decoration. Production
# normally completes this query in ~1.6s, so give it a bounded window that still fits
# inside the 4.5s global dashboard budget.
PLAYER_DASHBOARD_AGGREGATE_TIMEOUT_SECONDS = 2.0
PLAYER_DASHBOARD_PREVIOUS_VERIFICATION_TIMEOUT_SECONDS = 2.0
PLAYER_DASHBOARD_HISTORY_LIMIT = 10
CARD_TWO_TITLE = "📖 AI 驗證與分析報告"
CARD_TWO_RULE_ORDER = [
    ("hot", "熱門"),
    ("cold", "冷門"),
    ("missing", "缺號"),
    ("repeat", "重號"),
    ("tail", "尾數"),
    ("gap", "間距"),
    ("cluster", "群聚"),
    ("diagonal", "斜線"),
    ("super", "超級獎"),
    ("laowanjia", "老玩家"),
    ("ladder", "階梯"),
    ("partial_ladder", "偏階"),
    ("extended_ladder", "延階"),
    ("reverse", "反號"),
    ("neighbor", "隔壁號"),
    ("guide", "引路牌"),
    ("integrated", "整合數"),
    ("sunset", "太陽下山"),
    ("momentum", "盤勢動能"),
    ("super_number_trajectory_recovery", "超獎軌跡回補"),
    ("cluster_aftershock_recovery", "群聚後連號回補"),
]
CARD_TWO_FINALIZED_DISALLOWED = {
    "provisional",
    "processing",
    "validating",
    "learning",
    "pending",
    "waiting_draw",
    "failed",
    "incomplete",
    "test",
    "legacy",
}
_PLAYER_SUMMARY_CACHE: dict[str, Any] = {"payload": None, "expires_at": 0.0}
_PLAYER_SUMMARY_CACHE_LOCK = threading.RLock()
_PLAYER_SUMMARY_BUILD_LOCK = threading.Lock()
_PLAYER_COMPONENT_CACHE_UPDATED_AT: dict[str, float] = {}
_PLAYER_COMPONENT_CACHE: dict[str, Any] = {
    "official_draw": None,
    "next_prediction_snapshot": None,
    "prediction_history": [],
    "card_two_history": [],
    "prediction_aggregates": {},
    "analysis": {},
    "kuaishou": {},
    "previous_verification": None,
    "card_two": None,
    "active_release": None,
    "production_scope": None,
    "rule_library": None,
}
PLAYER_CACHE_FILTER_VERSION = "production_prediction_v2"
_PLAYER_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="player-dashboard")
_PLAYER_IN_FLIGHT_LOCK = threading.Lock()
_PLAYER_COMPONENT_IN_FLIGHT: dict[str, Any] = {}
_PLAYER_ACTIVE_COMPONENTS: dict[int, dict[str, Any]] = {}
_PLAYER_ACTIVE_COMPONENTS_LOCK = threading.Lock()
_PLAYER_CACHE_GENERATION = 0
_PLAYER_DASHBOARD_GENERATION_CONTEXT: ContextVar[str | None] = ContextVar("player_dashboard_generation_id", default=None)
_PLAYER_RUNTIME_METRICS: dict[str, int] = {
    "submitted_count": 0,
    "skipped_busy_count": 0,
    "cache_hit_count": 0,
    "stale_fallback_count": 0,
    "timeout_count": 0,
}
_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT = 20
_PLAYER_COMPONENT_DIAGNOSTICS_LOCK = threading.RLock()
_PLAYER_COMPONENT_DIAGNOSTICS: dict[str, deque[dict[str, Any]]] = {
    "official_draw": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "kuaishou": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "next_prediction_snapshot": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "card_two_history": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "prediction_aggregates": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "analysis": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "active_release": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "previous_verification": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
    "card_two": deque(maxlen=_PLAYER_COMPONENT_DIAGNOSTIC_LIMIT),
}
_PLAYER_DASHBOARD_WAIT_ORDER_CONTEXT: ContextVar[int | None] = ContextVar("player_dashboard_wait_order", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dashboard_generation_id() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond:06d}"


def _cached_summary() -> dict | None:
    if not _PLAYER_SUMMARY_CACHE_LOCK.acquire(blocking=False):
        return None
    try:
        payload = _PLAYER_SUMMARY_CACHE.get("payload")
        expires_at = float(_PLAYER_SUMMARY_CACHE.get("expires_at") or 0)
    finally:
        _PLAYER_SUMMARY_CACHE_LOCK.release()
    if isinstance(payload, dict) and time.monotonic() < expires_at:
        if payload.get("cache_filter_version") != PLAYER_CACHE_FILTER_VERSION:
            return None
        latest = ((payload.get("next_prediction") or {}).get("prediction_issue"))
        based_on = ((payload.get("next_prediction") or {}).get("based_on_issue"))
        if latest and not is_production_prediction({"issue": based_on, "prediction_issue": latest, "recommend_numbers": (payload.get("next_prediction") or {}).get("recommend_numbers")}):
            return None
        next_prediction = payload.get("next_prediction") or {}
        if next_prediction.get("status") == "expired" or int(next_prediction.get("lag_issues") or 0) > 1:
            return None
        cached = deepcopy(payload)
        cached["cached"] = True
        _PLAYER_RUNTIME_METRICS["cache_hit_count"] += 1
        return cached
    return None


def _store_summary_cache(payload: dict) -> None:
    with _PLAYER_SUMMARY_CACHE_LOCK:
        _PLAYER_SUMMARY_CACHE["payload"] = deepcopy(payload)
        _PLAYER_SUMMARY_CACHE["expires_at"] = time.monotonic() + PLAYER_SUMMARY_TTL_SECONDS


def invalidate_player_dashboard_cache(reason: str | None = None) -> dict:
    global _PLAYER_CACHE_GENERATION
    with _PLAYER_SUMMARY_CACHE_LOCK:
        _PLAYER_SUMMARY_CACHE["payload"] = None
        _PLAYER_SUMMARY_CACHE["expires_at"] = 0.0
    for key in list(_PLAYER_COMPONENT_CACHE):
        _PLAYER_COMPONENT_CACHE[key] = None if not isinstance(_PLAYER_COMPONENT_CACHE[key], list) else []
    _PLAYER_COMPONENT_CACHE_UPDATED_AT.clear()
    with _PLAYER_IN_FLIGHT_LOCK:
        stale = [
            name
            for name, future in _PLAYER_COMPONENT_IN_FLIGHT.items()
            if future is not None and not future.done()
        ]
        _PLAYER_CACHE_GENERATION += 1
        _PLAYER_COMPONENT_IN_FLIGHT.clear()
    logger.info(
        "player dashboard cache invalidated reason=%s stale_in_flight=%s",
        reason or "unspecified",
        stale,
    )
    return {"status": "ok", "reason": reason, "stale_in_flight": stale}


def reload_latest_production_snapshot(official_draw: dict | None = None, reason: str | None = None) -> dict:
    official = official_draw or get_latest_official_draw()
    current = _current_draw(official)
    if not current:
        return {"status": "skipped", "reason": "latest_official_draw_unavailable"}
    prediction = _current_prediction_for_draw(current)
    detected_latest_issue = current.get("issue")
    next_prediction = (
        _prediction_from_history(
            prediction,
            current,
            detected_latest_issue,
            allow_slow_lookups=False,
        )
        or _pending_next_prediction(current, detected_latest_issue)
    )
    next_prediction["history"] = _load_component_cache("prediction_history_stats", {}) or {}
    next_prediction["rule_library"] = _load_component_cache("rule_library", _empty_rule_library()) or _empty_rule_library()
    next_prediction = _enrich_dashboard_card_v1(next_prediction, current)
    _store_component_cache("official_draw", official)
    _store_component_cache("next_prediction_snapshot", next_prediction)
    logger.info(
        "player dashboard latest production snapshot reloaded reason=%s issue=%s target_issue=%s",
        reason or "unspecified",
        current.get("issue"),
        next_prediction.get("prediction_issue") or next_prediction.get("target_issue"),
    )
    return {
        "status": "ok",
        "reason": reason,
        "issue": current.get("issue"),
        "target_issue": next_prediction.get("prediction_issue") or next_prediction.get("target_issue"),
        "recommend_count": len(_as_int_list(next_prediction.get("recommend_numbers"))),
    }


def _store_component_cache(name: str, payload: Any) -> bool:
    if name == "prediction_history" and isinstance(payload, list):
        payload = [item for item in payload if is_production_prediction(item)]
    existing = _PLAYER_COMPONENT_CACHE.get(name)
    allowed, reason = _component_cache_update_allowed(existing, payload)
    if not allowed:
        logger.warning(
            "player_dashboard_component_cache_update_skipped component=%s reason=%s existing_issue=%s incoming_issue=%s",
            name,
            reason,
            _component_cache_issue(existing),
            _component_cache_issue(payload),
        )
        return False
    _PLAYER_COMPONENT_CACHE[name] = deepcopy(payload)
    _PLAYER_COMPONENT_CACHE_UPDATED_AT[name] = time.monotonic()
    return True


def _load_fresh_component_cache(name: str, ttl_seconds: float, fallback=None):
    updated_at = _PLAYER_COMPONENT_CACHE_UPDATED_AT.get(name)
    if updated_at is None or time.monotonic() - updated_at >= ttl_seconds:
        return None
    return _load_component_cache(name, fallback)


def _load_component_cache(name: str, fallback=None):
    cached = _PLAYER_COMPONENT_CACHE.get(name)
    if cached is None:
        return fallback
    if name == "prediction_history" and isinstance(cached, list):
        cached = [item for item in cached if is_production_prediction(item)]
    return deepcopy(cached)


def _tracked_component_lifecycle(name: str, submitted_at: float, dashboard_generation_id: str | None) -> dict[str, Any] | None:
    if name not in _PLAYER_COMPONENT_DIAGNOSTICS:
        return None
    return {
        "component": name,
        "dashboard_generation_id": dashboard_generation_id,
        "submitted_at": submitted_at,
    }


def _round_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 1000, 2)


def _component_lifecycle_fields(lifecycle: dict[str, Any]) -> dict[str, Any]:
    submitted_at = lifecycle.get("submitted_at")
    execution_started_at = lifecycle.get("execution_started_at")
    wait_started_at = lifecycle.get("wait_started_at")
    wait_ended_at = lifecycle.get("wait_ended_at")
    execution_completed_at = lifecycle.get("execution_completed_at")
    fields = {
        "component": lifecycle.get("component"),
        "dashboard_generation_id": lifecycle.get("dashboard_generation_id"),
        "submitted_at": round(submitted_at, 6) if submitted_at is not None else None,
        "execution_started_at": round(execution_started_at, 6) if execution_started_at is not None else None,
        "wait_started_at": round(wait_started_at, 6) if wait_started_at is not None else None,
        "wait_ended_at": round(wait_ended_at, 6) if wait_ended_at is not None else None,
        "execution_completed_at": round(execution_completed_at, 6) if execution_completed_at is not None else None,
        "submit_to_start_ms": _round_ms(execution_started_at - submitted_at) if execution_started_at is not None and submitted_at is not None else None,
        "submit_to_wait_ms": _round_ms(wait_started_at - submitted_at) if wait_started_at is not None and submitted_at is not None else None,
        "wait_ms": _round_ms(wait_ended_at - wait_started_at) if wait_ended_at is not None and wait_started_at is not None else None,
        "execution_ms": _round_ms(execution_completed_at - execution_started_at) if execution_completed_at is not None and execution_started_at is not None else None,
        "completion_after_wait_ms": _round_ms(execution_completed_at - wait_ended_at) if execution_completed_at is not None and wait_ended_at is not None else None,
    }
    return fields


def _record_component_diagnostic(lifecycle: dict[str, Any] | None, **updates: Any) -> dict[str, Any] | None:
    if not lifecycle:
        return None
    component = lifecycle.get("component")
    if component not in _PLAYER_COMPONENT_DIAGNOSTICS:
        return None
    with _PLAYER_COMPONENT_DIAGNOSTICS_LOCK:
        record = lifecycle.get("diagnostic_record")
        if record is None:
            record = {}
            lifecycle["diagnostic_record"] = record
            _PLAYER_COMPONENT_DIAGNOSTICS[component].append(record)
        record.update(_component_lifecycle_fields(lifecycle))
        record.update({key: deepcopy(value) for key, value in updates.items() if value is not None})
        return record


def _component_wait_classification(lifecycle: dict[str, Any] | None, future_done_at_wait: bool, fallback_reason: str | None = None) -> str | None:
    if future_done_at_wait:
        return "completed_before_wait"
    if not lifecycle or lifecycle.get("execution_started_at") is None:
        if fallback_reason == "budget_exhausted":
            return "not_started_budget_exhausted"
        return "not_started"
    if lifecycle.get("execution_completed_at") is not None:
        return "completed_after_wait_started"
    if fallback_reason == "budget_exhausted":
        return "running_budget_exhausted"
    if fallback_reason == "timeout":
        return "running_timeout"
    return "running"


def get_dashboard_component_diagnostics() -> dict:
    with _PLAYER_COMPONENT_DIAGNOSTICS_LOCK:
        records = {
            component: list(component_records)
            for component, component_records in _PLAYER_COMPONENT_DIAGNOSTICS.items()
        }
    return {
        "limit": _PLAYER_COMPONENT_DIAGNOSTIC_LIMIT,
        "components": deepcopy(records),
    }


def get_prediction_aggregate_component_diagnostics() -> dict:
    with _PLAYER_COMPONENT_DIAGNOSTICS_LOCK:
        records = list(_PLAYER_COMPONENT_DIAGNOSTICS["prediction_aggregates"])
    return {
        "limit": _PLAYER_COMPONENT_DIAGNOSTIC_LIMIT,
        "recent": deepcopy(records),
    }


def _component_cache_issue(payload: Any) -> int | None:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.extend(
            [
                payload.get("issue"),
                payload.get("prediction_issue"),
                payload.get("target_issue"),
                payload.get("based_on_issue"),
                payload.get("displayed_target_issue"),
                payload.get("requested_target_issue"),
                payload.get("latest_issue"),
            ]
        )
    elif isinstance(payload, list) and payload:
        return _component_cache_issue(payload[0])
    values = [_as_int(value) for value in candidates if value not in (None, "")]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _component_cache_update_allowed(existing: Any, incoming: Any) -> tuple[bool, str]:
    if existing in (None, [], {}):
        return True, "empty_cache"
    existing_issue = _component_cache_issue(existing)
    incoming_issue = _component_cache_issue(incoming)
    if existing_issue is None and incoming_issue is None:
        return True, "unversioned"
    if incoming_issue is None:
        return False, "incoming_issue_uncomparable"
    if existing_issue is None:
        return True, "existing_issue_uncomparable"
    if incoming_issue < existing_issue:
        return False, "older_issue"
    if incoming_issue == existing_issue:
        existing_at = _parse_datetime(_parse_generated_at(existing))
        incoming_at = _parse_datetime(_parse_generated_at(incoming))
        if existing_at is not None and incoming_at is not None and incoming_at < existing_at:
            return False, "older_generated_at"
        return True, "same_issue_newer_or_unversioned_time"
    return True, "newer_or_same_issue"


def _parse_generated_at(payload: Any) -> str | None:
    if isinstance(payload, dict):
        return (
            payload.get("generated_at")
            or payload.get("predict_time")
            or payload.get("created_at")
            or payload.get("updated_at")
        )
    if isinstance(payload, list) and payload:
        return _parse_generated_at(payload[0])
    return None


def _age_seconds(generated_at: Any) -> float | None:
    if not generated_at:
        return None
    try:
        text = str(generated_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round(max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()), 3)
    except Exception:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _component_metadata(
    name: str,
    payload: Any,
    *,
    source: str,
    timed_out: bool,
    result: str,
    dashboard_generation_id: str | None,
) -> dict:
    issue = None
    source_issue = None
    target_issue = None
    stale = source != "live" or result in {"stale", "skipped", "timeout", "error"}
    if isinstance(payload, dict):
        issue = payload.get("issue") or payload.get("displayed_target_issue") or payload.get("latest_issue")
        source_issue = payload.get("source_issue") or payload.get("based_on_issue") or payload.get("issue")
        target_issue = (
            payload.get("target_issue")
            or payload.get("prediction_issue")
            or payload.get("displayed_target_issue")
            or payload.get("requested_target_issue")
        )
        stale = bool(payload.get("stale") or payload.get("is_stale") or stale)
    elif isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            issue = first.get("prediction_issue") or first.get("issue")
            source_issue = first.get("issue")
            target_issue = first.get("prediction_issue")
    generated_at = _parse_generated_at(payload)
    return {
        "component": name,
        "generation_id": dashboard_generation_id,
        "issue": str(issue) if issue is not None else None,
        "source_issue": str(source_issue) if source_issue is not None else None,
        "target_issue": str(target_issue) if target_issue is not None else None,
        "source": source,
        "generated_at": generated_at,
        "age_seconds": _age_seconds(generated_at),
        "stale": stale,
        "timed_out": timed_out,
        "result": result,
    }


def _deadline_remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _timed_default(name: str, started: float, result: str, source: str | None = None, **extra: Any) -> dict:
    payload = {
        "step": name,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "result": result,
    }
    if source:
        payload["source"] = source
    payload.update(extra)
    return payload


def _log_component_stage(component: str, stage: str, started: float, result: str = "success", **extra: Any) -> None:
    fields = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
    suffix = f" {fields}" if fields else ""
    logger.warning(
        "component_stage_latency component=%s stage=%s duration_ms=%s result=%s%s",
        component,
        stage,
        round((time.perf_counter() - started) * 1000, 2),
        result,
        suffix,
    )


def _timed_component_stage(component: str, stage: str, fn):
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        _log_component_stage(component, stage, started, "failed", error_type=type(exc).__name__)
        raise
    _log_component_stage(component, stage, started, "success")
    return result


def _record_prediction_transform_stage(
    diagnostics: dict[str, Any] | None,
    stage: str,
    started: float,
    *,
    result: str = "ok",
    **extra: Any,
) -> None:
    if diagnostics is None:
        return
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    stage_record = {
        "elapsed_ms": elapsed_ms,
        "result": result,
    }
    stage_record.update({key: value for key, value in extra.items() if value is not None})
    diagnostics.setdefault("transform_stages", {})[stage] = stage_record


def _public_step_name(name: str) -> str:
    return {
        "prediction_history": "history",
        "prediction_aggregates": "aggregates",
        "active_release": "release",
        "previous_verification": "verification",
    }.get(name, name)


def _submit_component(name: str, fn):
    submitted_at = time.perf_counter()
    dashboard_generation_id = _PLAYER_DASHBOARD_GENERATION_CONTEXT.get()
    lifecycle = _tracked_component_lifecycle(name, submitted_at, dashboard_generation_id)

    def timed_fn():
        started_at = time.perf_counter()
        if lifecycle is not None:
            lifecycle["execution_started_at"] = started_at
        thread_id = threading.get_ident()
        with _PLAYER_ACTIVE_COMPONENTS_LOCK:
            _PLAYER_ACTIVE_COMPONENTS[thread_id] = {
                "component": name,
                "thread_id": thread_id,
                "process_id": None,
                "started_at": started_at,
            }
        try:
            result = fn()
            if lifecycle is not None:
                lifecycle["execution_completed_at"] = time.perf_counter()
                if isinstance(result, dict):
                    lifecycle["db_timing"] = deepcopy(result.get("db_timing"))
                    lifecycle["diagnostics"] = deepcopy(result.get("diagnostics"))
                    lifecycle["query_count"] = result.get("query_count")
        except Exception as exc:
            if lifecycle is not None:
                lifecycle["execution_completed_at"] = time.perf_counter()
                lifecycle["error_type"] = type(exc).__name__
            logger.warning(
                "dashboard_component_latency component=%s queue_ms=%s execution_ms=%s result=failed error_type=%s",
                name,
                round((started_at - submitted_at) * 1000, 2),
                round((time.perf_counter() - started_at) * 1000, 2),
                type(exc).__name__,
            )
            raise
        finally:
            with _PLAYER_ACTIVE_COMPONENTS_LOCK:
                _PLAYER_ACTIVE_COMPONENTS.pop(thread_id, None)
        logger.warning(
            "dashboard_component_latency component=%s queue_ms=%s execution_ms=%s result=success",
            name,
            round((started_at - submitted_at) * 1000, 2),
            round((time.perf_counter() - started_at) * 1000, 2),
        )
        return result

    with _PLAYER_IN_FLIGHT_LOCK:
        existing = _PLAYER_COMPONENT_IN_FLIGHT.get(name)
        if existing is not None and not existing.done():
            _PLAYER_RUNTIME_METRICS["skipped_busy_count"] += 1
            return None, "busy"
        generation = _PLAYER_CACHE_GENERATION
        future = _PLAYER_EXECUTOR.submit(timed_fn)
        future._dashboard_generation_id = dashboard_generation_id
        future._dashboard_lifecycle = lifecycle
        _PLAYER_COMPONENT_IN_FLIGHT[name] = future
        _PLAYER_RUNTIME_METRICS["submitted_count"] += 1
    future.add_done_callback(
        lambda completed, component=name, submitted_generation=generation, submitted_at=submitted_at: _complete_component(
            component,
            completed,
            submitted_generation,
            submitted_at,
            getattr(completed, "_dashboard_generation_id", None),
        )
    )
    return future, "submitted"


def _complete_component(
    name: str,
    future,
    submitted_generation: int,
    submitted_at: float,
    dashboard_generation_id: str | None,
) -> None:
    with _PLAYER_IN_FLIGHT_LOCK:
        if submitted_generation != _PLAYER_CACHE_GENERATION:
            logger.info("player dashboard stale component result discarded component=%s", name)
            return
    try:
        result = future.result()
    except Exception:
        lifecycle = getattr(future, "_dashboard_lifecycle", None)
        if lifecycle is not None:
            lifecycle.setdefault("execution_completed_at", time.perf_counter())
            if lifecycle.get("initial_result") in {"timeout", "skipped"}:
                _record_component_diagnostic(
                    lifecycle,
                    late_result="failed",
                    late_error_type=lifecycle.get("error_type"),
                    late_completion_ms=_round_ms(lifecycle.get("execution_completed_at") - lifecycle.get("submitted_at"))
                    if lifecycle.get("execution_completed_at") is not None and lifecycle.get("submitted_at") is not None
                    else None,
                )
        logger.warning("player_dashboard_component_late_result_failed component=%s", name, exc_info=True)
        return
    lifecycle = getattr(future, "_dashboard_lifecycle", None)
    if lifecycle is not None:
        lifecycle.setdefault("execution_completed_at", time.perf_counter())
        if lifecycle.get("initial_result") in {"timeout", "skipped"}:
            _record_component_diagnostic(
                lifecycle,
                late_result="success",
                late_completion_ms=_round_ms(lifecycle.get("execution_completed_at") - lifecycle.get("submitted_at"))
                if lifecycle.get("execution_completed_at") is not None and lifecycle.get("submitted_at") is not None
                else None,
                late_db_timing=deepcopy(result.get("db_timing")) if isinstance(result, dict) else None,
                late_diagnostics=deepcopy(result.get("diagnostics")) if isinstance(result, dict) else None,
            )
    updated = _store_component_cache(name, result)
    logger.warning(
        "dashboard_late_component_completion component=%s generation_id=%s issue=%s elapsed_ms=%s cache_updated=%s cache_update_reason=%s",
        name,
        dashboard_generation_id or submitted_generation,
        _component_cache_issue(result),
        round((time.perf_counter() - submitted_at) * 1000, 2),
        updated,
        "updated" if updated else "guard_rejected",
    )


def _component_result(
    name: str,
    future,
    *,
    deadline: float,
    timeout_seconds: float,
    timings: list[dict],
    warnings: list[str],
    fallback=None,
    component_metadata: dict[str, dict] | None = None,
    dashboard_generation_id: str | None = None,
):
    started = time.perf_counter()
    lifecycle = getattr(future, "_dashboard_lifecycle", None) if future is not None else None
    wait_order_position = _PLAYER_DASHBOARD_WAIT_ORDER_CONTEXT.get()
    if wait_order_position is not None:
        _PLAYER_DASHBOARD_WAIT_ORDER_CONTEXT.set(wait_order_position + 1)
    if lifecycle is not None:
        lifecycle["wait_started_at"] = started
        if wait_order_position is not None:
            lifecycle["wait_order_position"] = wait_order_position
        lifecycle["timeout_requested_ms"] = round(timeout_seconds * 1000, 2)
    def remember(payload: Any, source: str, *, timed_out: bool = False, result: str = "ok") -> Any:
        if component_metadata is not None:
            component_metadata[name] = _component_metadata(
                name,
                payload,
                source=source,
                timed_out=timed_out,
                result=result,
                dashboard_generation_id=dashboard_generation_id,
            )
        if isinstance(payload, dict):
            payload = dict(payload)
            if component_metadata is not None:
                payload["_component_metadata"] = component_metadata.get(name)
                if source in {"cache", "fallback"}:
                    payload["source"] = source
                else:
                    payload.setdefault("source", "live")
                payload.setdefault("stale", source != "live")
        return payload

    if future is None:
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        warnings.append(f"{name} stale cache")
        timings.append(
            _timed_default(
                name,
                started,
                "stale",
                "last_good_cache",
                reason="worker_busy",
                in_flight_count=_player_in_flight_count(),
            )
        )
        return remember(_load_component_cache(name, fallback), "cache", result="stale")

    remaining = _deadline_remaining(deadline)
    future_done_at_wait = future.done()
    remaining_budget_at_wait_ms = round(remaining * 1000, 2)
    if remaining <= 0:
        if lifecycle is not None:
            lifecycle["wait_ended_at"] = time.perf_counter()
            lifecycle["initial_result"] = "skipped"
            _record_component_diagnostic(
                lifecycle,
                initial_result="skipped",
                source="last_good_cache",
                fallback_reason="budget_exhausted",
                skip_reason="budget_exhausted",
                timeout_requested_ms=round(timeout_seconds * 1000, 2),
                timeout_effective_ms=0.0,
                status="skipped",
                wait_classification=_component_wait_classification(lifecycle, future_done_at_wait, "budget_exhausted"),
                wait_order_position=wait_order_position,
                remaining_budget_at_wait_ms=remaining_budget_at_wait_ms,
                future_done_at_wait=future_done_at_wait,
            )
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        warnings.append(f"{name} skipped budget")
        timings.append(_timed_default(name, started, "skipped", "last_good_cache", reason="budget_exhausted"))
        return remember(_load_component_cache(name, fallback), "fallback", result="skipped")

    wait_seconds = max(0.0, min(timeout_seconds, remaining))
    timeout_effective_ms = round(wait_seconds * 1000, 2)
    try:
        result = future.result(timeout=wait_seconds)
        if lifecycle is not None:
            lifecycle["wait_ended_at"] = time.perf_counter()
            lifecycle["initial_result"] = "ok"
            _record_component_diagnostic(
                lifecycle,
                initial_result="ok",
                source="fresh",
                wait_order_position=wait_order_position,
                remaining_budget_at_wait_ms=remaining_budget_at_wait_ms,
                future_done_at_wait=future_done_at_wait,
                timeout_requested_ms=round(timeout_seconds * 1000, 2),
                timeout_effective_ms=timeout_effective_ms,
                status="ok",
                wait_classification=_component_wait_classification(lifecycle, future_done_at_wait),
                diagnostics=deepcopy(result.get("diagnostics")) if isinstance(result, dict) else None,
                db_timing=deepcopy(result.get("db_timing")) if isinstance(result, dict) else None,
                query_count=result.get("query_count") if isinstance(result, dict) else None,
            )
        _store_component_cache(name, result)
        timings.append(_timed_default(name, started, "ok", "fresh"))
        return remember(result, "live")
    except (TimeoutError, CancelledError) as exc:
        if lifecycle is not None:
            lifecycle["wait_ended_at"] = time.perf_counter()
            lifecycle["initial_result"] = "timeout"
        _PLAYER_RUNTIME_METRICS["timeout_count"] += 1
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        cancel_requested = future.cancel()
        timed_out = isinstance(exc, TimeoutError)
        if lifecycle is not None:
            _record_component_diagnostic(
                lifecycle,
                source="last_good_cache",
                timeout_seconds=round(wait_seconds, 3),
                wait_order_position=wait_order_position,
                remaining_budget_at_wait_ms=remaining_budget_at_wait_ms,
                future_done_at_wait=future_done_at_wait,
                future_running_at_timeout=future.running(),
                cancel_requested=cancel_requested,
                cancelled=future.cancelled(),
                timed_out=timed_out,
                fallback_reason="timeout",
                timeout_reason="component_timeout",
                timeout_requested_ms=round(timeout_seconds * 1000, 2),
                timeout_effective_ms=timeout_effective_ms,
                status="timeout",
                wait_classification=_component_wait_classification(lifecycle, future_done_at_wait, "timeout"),
                initial_result="timeout",
                in_flight_count=_player_in_flight_count(),
            )
        logger.warning(
            "player_dashboard_component_timeout component=%s timeout_seconds=%s fallback=last_good_cache future_running=%s cancel_requested=%s cancelled=%s",
            name,
            round(wait_seconds, 3),
            future.running(),
            cancel_requested,
            future.cancelled(),
        )
        warnings.append(f"{name} fallback cache")
        timings.append(
            _timed_default(
                name,
                started,
                "timeout",
                "last_good_cache",
                timeout_seconds=round(wait_seconds, 3),
                in_flight_count=_player_in_flight_count(),
                timed_out=timed_out,
                cancelled=future.cancelled(),
                future_running=future.running(),
                cancel_requested=cancel_requested,
            )
        )
        return remember(_load_component_cache(name, fallback), "cache", timed_out=True, result="timeout")
    except Exception as exc:
        if lifecycle is not None:
            lifecycle["wait_ended_at"] = time.perf_counter()
            lifecycle["initial_result"] = "error"
            _record_component_diagnostic(
                lifecycle,
                source="last_good_cache",
                fallback_reason="error",
                initial_result="error",
                timeout_requested_ms=round(timeout_seconds * 1000, 2),
                timeout_effective_ms=timeout_effective_ms,
                status="error",
                wait_classification=_component_wait_classification(lifecycle, future_done_at_wait, "error"),
                wait_order_position=wait_order_position,
                remaining_budget_at_wait_ms=remaining_budget_at_wait_ms,
                future_done_at_wait=future_done_at_wait,
                error_type=type(exc).__name__,
            )
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        logger.warning("player_dashboard_component_failed component=%s fallback=last_good_cache", name, exc_info=True)
        warnings.append(f"{name} fallback cache")
        timings.append(_timed_default(name, started, "error", "last_good_cache", exception_type=type(exc).__name__))
        return remember(_load_component_cache(name, fallback), "fallback", result="error")


def _run_inline_step(
    name: str,
    fn,
    *,
    deadline: float,
    timings: list[dict],
    warnings: list[str],
    fallback=None,
    cache_name: str | None = None,
):
    started = time.perf_counter()
    if _deadline_remaining(deadline) <= 0:
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        warnings.append(f"{name} skipped budget")
        timings.append(_timed_default(name, started, "skipped", "last_good_cache", reason="budget_exhausted"))
        return _load_component_cache(cache_name or name, fallback)
    try:
        result = fn()
        if cache_name or name in _PLAYER_COMPONENT_CACHE:
            _store_component_cache(cache_name or name, result)
        timings.append(_timed_default(name, started, "ok", "fresh"))
        return result
    except Exception as exc:
        _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
        logger.warning("player_dashboard_inline_step_failed component=%s fallback=last_good_cache", name, exc_info=True)
        warnings.append(f"{name} fallback cache")
        timings.append(_timed_default(name, started, "error", "last_good_cache", exception_type=type(exc).__name__))
        return _load_component_cache(cache_name or name, fallback)


def _player_in_flight_count() -> int:
    with _PLAYER_IN_FLIGHT_LOCK:
        return sum(1 for future in _PLAYER_COMPONENT_IN_FLIGHT.values() if future is not None and not future.done())


def active_dashboard_components() -> list[dict[str, Any]]:
    now = time.perf_counter()
    with _PLAYER_ACTIVE_COMPONENTS_LOCK:
        return [
            {
                "component": item.get("component"),
                "thread_id": item.get("thread_id"),
                "active_ms": round((now - float(item.get("started_at") or now)) * 1000, 2),
            }
            for item in _PLAYER_ACTIVE_COMPONENTS.values()
        ]


def player_dashboard_runtime_metrics() -> dict:
    return {
        **dict(_PLAYER_RUNTIME_METRICS),
        "in_flight_count": _player_in_flight_count(),
        "max_workers": getattr(_PLAYER_EXECUTOR, "_max_workers", None),
    }


def run_card_two_dashboard_context_benchmark(repetitions: int = 7) -> dict:
    from database.prediction_history_store import get_card_two_history_timing_status

    repetitions = max(1, min(int(repetitions or 7), 7))
    seen = {
        event.get("recorded_at")
        for event in get_card_two_history_timing_status().get("recent", [])
        if event.get("type") == "dashboard_context"
    }
    samples = []
    for index in range(repetitions):
        invalidate_player_dashboard_cache("card_two_dashboard_context_benchmark")
        summary_started = time.perf_counter()
        summary = build_player_dashboard_summary()
        context = None
        wait_until = time.monotonic() + 8.0
        while time.monotonic() < wait_until:
            recent = get_card_two_history_timing_status().get("recent", [])
            for event in reversed(recent):
                if event.get("type") != "dashboard_context":
                    continue
                recorded_at = event.get("recorded_at")
                if recorded_at in seen:
                    continue
                context = deepcopy(event)
                seen.add(recorded_at)
                break
            if context is not None:
                break
            time.sleep(0.05)
        samples.append(
            {
                "sample": index + 1,
                "summary_status": summary.get("status"),
                "summary_ms": round((time.perf_counter() - summary_started) * 1000, 2),
                "timeout_steps": list(((summary.get("timing") or {}).get("timeout_steps")) or []),
                "in_flight_count": _player_in_flight_count(),
                "context": context,
            }
        )
    return {"status": "ok", "samples": samples}


def run_isolated_card_two_dashboard_context_benchmark(repetitions: int = 7) -> dict:
    from database.prediction_history_store import get_card_two_history_timing_status

    repetitions = max(1, min(int(repetitions or 7), 7))
    seen = {
        event.get("recorded_at")
        for event in get_card_two_history_timing_status().get("recent", [])
        if event.get("type") == "dashboard_context"
    }
    samples = []
    for index in range(repetitions):
        future, state = _submit_component(
            "card_two_history",
            lambda: _timed_component_stage(
                "card_two_history",
                "prediction_history_summary_records",
                lambda: get_prediction_history_records(100, diagnostic_component="card_two_history"),
            ),
        )
        result_count = None
        error_type = None
        if future is not None:
            try:
                result = future.result(timeout=8.0)
                result_count = len(result or [])
            except Exception as exc:
                error_type = type(exc).__name__
        context = None
        wait_until = time.monotonic() + 2.0
        while time.monotonic() < wait_until:
            recent = get_card_two_history_timing_status().get("recent", [])
            for event in reversed(recent):
                if event.get("type") != "dashboard_context":
                    continue
                recorded_at = event.get("recorded_at")
                if recorded_at in seen:
                    continue
                context = deepcopy(event)
                seen.add(recorded_at)
                break
            if context is not None:
                break
            time.sleep(0.05)
        samples.append(
            {
                "sample": index + 1,
                "submit_state": state,
                "result_count": result_count,
                "error_type": error_type,
                "in_flight_count": _player_in_flight_count(),
                "context": context,
            }
        )
    return {"status": "ok", "samples": samples}


def _card_two_history_component():
    return _timed_component_stage(
        "card_two_history",
        "prediction_history_summary_records",
        lambda: get_prediction_history_records(100, diagnostic_component="card_two_history"),
    )


def _dashboard_benchmark_context_after(seen: set) -> dict | None:
    from database.prediction_history_store import get_card_two_history_timing_status

    wait_until = time.monotonic() + 2.0
    while time.monotonic() < wait_until:
        recent = get_card_two_history_timing_status().get("recent", [])
        for event in reversed(recent):
            if event.get("type") != "dashboard_context":
                continue
            recorded_at = event.get("recorded_at")
            if recorded_at in seen:
                continue
            seen.add(recorded_at)
            return deepcopy(event)
        time.sleep(0.05)
    return None


def _resolve_dashboard_benchmark_inputs() -> dict:
    timings: list[dict] = []
    warnings: list[str] = []
    deadline = time.monotonic() + PLAYER_DASHBOARD_TOTAL_BUDGET_SECONDS
    card_two_history_future, _ = _submit_component(
        "card_two_history",
        lambda: _timed_component_stage(
            "card_two_history",
            "prediction_history_summary_records",
            lambda: get_prediction_history_records(100, diagnostic_component="card_two_history"),
        ),
    )

    card_one = get_player_card_one_snapshot(deadline=deadline, timings=timings, warnings=warnings)
    current = card_one.get("current") or {}
    detected_latest_issue = card_one.get("detected_latest_issue")
    next_prediction = card_one.get("next_prediction") or {}
    return {
        "current": current,
        "detected_latest_issue": detected_latest_issue,
        "previous_target_issue": next_prediction.get("based_on_issue") or current.get("issue"),
    }


def _dashboard_benchmark_component_callables(inputs: dict) -> dict[str, Any]:
    current = inputs.get("current") or {}
    detected_latest_issue = inputs.get("detected_latest_issue")
    previous_target_issue = inputs.get("previous_target_issue")

    def build_next_snapshot():
        record = _timed_component_stage(
            "next_prediction_snapshot",
            "current_prediction_lookup",
            lambda: _current_prediction_for_draw(current),
        )
        return _timed_component_stage(
            "next_prediction_snapshot",
            "prediction_from_history",
            lambda: _prediction_from_history(record, current, detected_latest_issue, allow_slow_lookups=False),
        )

    return {
        "active_release": get_current_release,
        "analysis": get_latest_analysis_history,
        "next_prediction_snapshot": build_next_snapshot,
        "prediction_aggregates": lambda: get_prediction_lifecycle_aggregates(diagnostic_component="prediction_aggregates"),
        "previous_verification": lambda: _build_previous_verification_snapshot(previous_target_issue),
    }


def _run_card_two_pairwise_condition(name: str, overlap_names: list[str], repetitions: int, inputs: dict, seen: set) -> dict:
    callables = _dashboard_benchmark_component_callables(inputs)
    samples = []
    for index in range(repetitions):
        wait_until = time.monotonic() + 8.0
        while active_dashboard_components() and time.monotonic() < wait_until:
            time.sleep(0.02)
        overlap_futures = [
            _submit_component(component, callables[component])[0]
            for component in overlap_names
            if component in callables
        ]
        time.sleep(0.005)
        future, state = _submit_component("card_two_history", _card_two_history_component)
        result_count = None
        error_type = None
        if future is not None:
            try:
                result = future.result(timeout=8.0)
                result_count = len(result or [])
            except Exception as exc:
                error_type = type(exc).__name__
        for overlap_future in overlap_futures:
            if overlap_future is None:
                continue
            try:
                overlap_future.result(timeout=8.0)
            except Exception:
                pass
        context = _dashboard_benchmark_context_after(seen)
        samples.append(
            {
                "sample": index + 1,
                "submit_state": state,
                "overlap_requested": list(overlap_names),
                "result_count": result_count,
                "error_type": error_type,
                "in_flight_count": _player_in_flight_count(),
                "context": context,
            }
        )
    return {"condition": name, "overlap_requested": list(overlap_names), "samples": samples}


def run_card_two_concurrency_culprit_benchmark(repetitions: int = 5) -> dict:
    repetitions = max(1, min(int(repetitions or 5), 5))
    from database.prediction_history_store import get_card_two_history_timing_status

    seen = {
        event.get("recorded_at")
        for event in get_card_two_history_timing_status().get("recent", [])
        if event.get("type") == "dashboard_context"
    }
    inputs = _resolve_dashboard_benchmark_inputs()
    conditions = [
        ("alone", []),
        ("active_release", ["active_release"]),
        ("analysis", ["analysis"]),
        ("next_prediction_snapshot", ["next_prediction_snapshot"]),
        ("prediction_aggregates", ["prediction_aggregates"]),
        ("previous_verification", ["previous_verification"]),
        ("level_2", ["previous_verification", "prediction_aggregates"]),
        ("level_3", ["previous_verification", "prediction_aggregates", "analysis"]),
    ]
    return {
        "status": "ok",
        "conditions": [
            _run_card_two_pairwise_condition(name, overlap, repetitions, inputs, seen)
            for name, overlap in conditions
        ],
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _as_int_list(values: Any) -> list[int]:
    result: list[int] = []
    if isinstance(values, str):
        try:
            import json

            parsed = json.loads(values)
            values = parsed if isinstance(parsed, list) else [values]
        except Exception:
            values = [values]
    for value in values or []:
        number = _as_int(value)
        if number is not None and 1 <= number <= 80 and number not in result:
            result.append(number)
    return sorted(result)


def _valid_production_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.upper().startswith("TEST") or text.startswith("99"):
        return None
    if not text.isdigit():
        return None
    return text


def _derive_next_issue(source_issue: Any) -> str | None:
    issue = _valid_production_issue(source_issue)
    if not issue:
        return None
    try:
        return str(int(issue) + 1)
    except Exception:
        return None


def _current_prediction_for_draw(current_draw: dict | None) -> dict | None:
    source_issue = (current_draw or {}).get("issue")
    target_issue = _derive_next_issue(source_issue)
    if not source_issue or not target_issue:
        return None
    try:
        record = get_prediction_for_source_target(str(source_issue), target_issue)
    except Exception:
        logger.exception("player dashboard exact latest prediction lookup failed")
        return None
    return record if is_production_prediction(record) else None


def _max_issue(*values: Any) -> str | None:
    issues = [_as_int(value) for value in values if value not in (None, "")]
    issues = [issue for issue in issues if issue is not None]
    return str(max(issues)) if issues else None


def _target_status(target_issue: Any, current_issue: Any) -> dict:
    target_text = _valid_production_issue(target_issue)
    current_text = _valid_production_issue(current_issue)
    target_int = _as_int(target_text)
    current_int = _as_int(current_text)
    if target_int is None:
        return {"is_current": False, "status": "unavailable"}
    if current_int is None:
        return {"is_current": False, "status": "unavailable"}
    if target_int > current_int:
        return {"is_current": True, "status": "ready"}
    if target_int == current_int:
        return {"is_current": False, "status": "waiting_refresh"}
    return {"is_current": False, "status": "expired"}


def _dashboard_prediction_freshness(target_issue: Any, current_issue: Any) -> dict:
    target_int = _as_int(_valid_production_issue(target_issue))
    current_int = _as_int(_valid_production_issue(current_issue))
    if target_int is None or current_int is None:
        return {
            "dashboard_status": "unavailable",
            "stale_status": "unknown",
            "stale_status_label": "unknown",
            "is_stale": True,
            "lag_issues": None,
            "expected_target_issue": None,
        }

    expected_target = current_int + 1
    lag = max(expected_target - target_int, 0)
    if lag == 0:
        return {
            "dashboard_status": "waiting_draw",
            "stale_status": "normal",
            "stale_status_label": "normal",
            "is_stale": False,
            "lag_issues": 0,
            "expected_target_issue": str(expected_target),
        }
    if lag == 1:
        return {
            "dashboard_status": "waiting_refresh",
            "stale_status": "waiting_sync",
            "stale_status_label": "waiting_sync",
            "is_stale": True,
            "lag_issues": 1,
            "expected_target_issue": str(expected_target),
        }
    return {
        "dashboard_status": "expired",
        "stale_status": "possibly_expired",
        "stale_status_label": "possibly_expired",
        "is_stale": True,
        "lag_issues": lag,
        "expected_target_issue": str(expected_target),
    }


def _parse_draw_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def _format_draw_time(value: Any) -> str | None:
    parsed = _parse_draw_datetime(value)
    if parsed:
        return parsed.strftime("%Y/%m/%d %H:%M:%S")
    return str(value) if value else None


def _based_on_time(based_on_issue: Any, based_draw: dict | None) -> dict:
    draw_time = _format_draw_time((based_draw or {}).get("draw_time"))
    if draw_time:
        return {
            "based_on_draw_time": draw_time,
            "based_on_time_source": "official_draw_time",
        }
    event = get_latest_operation_event("official_draw_saved", str(based_on_issue)) if based_on_issue else None
    event_time = _format_draw_time((event or {}).get("created_at"))
    if event_time:
        return {
            "based_on_draw_time": event_time,
            "based_on_time_source": "official_draw_saved_event",
        }
    return {
        "based_on_draw_time": None,
        "based_on_time_source": "unavailable",
    }


def _snapshot_based_on_time(record: dict, based_draw: dict | None) -> dict:
    stored = record.get("based_on_draw_time") or record.get("source_draw_time")
    stored_time = _format_draw_time(stored)
    if stored_time:
        return {
            "based_on_draw_time": stored_time,
            "based_on_time_source": "snapshot",
        }
    draw_time = _format_draw_time((based_draw or {}).get("draw_time"))
    if draw_time:
        return {
            "based_on_draw_time": draw_time,
            "based_on_time_source": "official_draw_time",
        }
    collected_at = (
        (based_draw or {}).get("collected_at")
        or (based_draw or {}).get("updated_at")
        or (based_draw or {}).get("created_at")
    )
    collected_time = _format_draw_time(collected_at)
    if collected_time:
        return {
            "based_on_draw_time": collected_time,
            "based_on_time_source": "official_draw_collected_at",
        }
    return {
        "based_on_draw_time": None,
        "based_on_time_source": "unavailable",
    }


def _expected_draw_time(next_data: dict, current_draw: dict | None) -> tuple[str | None, str]:
    stored = (
        next_data.get("expected_draw_time")
        or next_data.get("draw_time")
        or next_data.get("prediction_time")
    )
    if stored:
        return _format_draw_time(stored), "stored"
    current_time = (current_draw or {}).get("draw_time")
    parsed = _parse_draw_datetime(current_time)
    if parsed:
        return (parsed + timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M:%S"), "derived"
    return None, "unavailable"


def _big_small(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    big = sum(1 for number in numbers if number >= 41)
    small = len(numbers) - big
    if big > small:
        return "big"
    if small > big:
        return "small"
    return "balanced"


def _odd_even(numbers: list[int]) -> str | None:
    if not numbers:
        return None
    odd = sum(1 for number in numbers if number % 2)
    even = len(numbers) - odd
    if odd > even:
        return "odd"
    if even > odd:
        return "even"
    return "balanced"


def _hit_label(hit_count: int) -> str:
    if hit_count >= 7:
        return "excellent"
    if hit_count >= 5:
        return "strong"
    if hit_count >= 3:
        return "moderate"
    if hit_count >= 1:
        return "low"
    return "no_hit"


def _safe_draw_verification_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"verified", "official_verified", "pending", "pending_verification", "unknown"}:
        return status
    return "unknown"


def _current_draw(draw: dict | None) -> dict | None:
    if not draw:
        return None
    numbers = _as_int_list(draw.get("numbers"))
    verification_status = _safe_draw_verification_status(
        draw.get("verification_status") or draw.get("status")
    )
    return {
        "issue": draw.get("issue"),
        "draw_date": draw.get("draw_date"),
        "draw_time": draw.get("draw_time"),
        "numbers": numbers,
        "super_number": draw.get("super_number"),
        "verification_status": verification_status,
        "status_label": verification_status,
        "big_small": draw.get("big_small") or _big_small(numbers),
        "odd_even": draw.get("odd_even") or _odd_even(numbers),
        "source": draw.get("source") or "official",
        "source_scope": draw.get("source_scope") or draw.get("scope") or "production",
        "collected_at": draw.get("updated_at") or draw.get("created_at"),
    }


def _latest_official_draw_card(draw: dict | None) -> dict | None:
    if not draw:
        return None
    payload = dict(draw)
    raw_numbers = payload.get("numbers") if isinstance(payload.get("numbers"), list) else []
    numbers = _as_int_list(raw_numbers)
    status_text = str(payload.get("verification_status") or payload.get("status") or "").lower()
    source_text = str(payload.get("source") or "").lower()
    scope_text = str(payload.get("source_scope") or payload.get("scope") or "").lower()
    issue = _valid_production_issue(payload.get("issue"))
    super_number = _as_int(payload.get("super_number"))
    verified = (
        issue is not None
        and status_text in {"verified", "official_verified"}
        and not any(token in source_text for token in ("backup", "fallback", "pending", "legacy", "test"))
        and scope_text == "production"
        and len(raw_numbers) == 20
        and len(numbers) == 20
        and super_number in numbers
        and "pending" not in status_text
    )
    if verified:
        safe_status = "verified"
    elif status_text in {"verified", "official_verified"}:
        safe_status = "unknown"
    else:
        safe_status = _safe_draw_verification_status(
            payload.get("verification_status") or payload.get("status")
        )
    payload.update(
        {
            "issue": issue or payload.get("issue"),
            "numbers": numbers,
            "verification_status": safe_status,
            "source_scope": payload.get("source_scope") or payload.get("scope") or "unknown",
        }
    )
    return payload


def _pairs(numbers: list[int], diff: int) -> list[list[int]]:
    number_set = set(numbers)
    return [[n, n + diff] for n in sorted(numbers) if n + diff in number_set]


def _tails(numbers: list[int]) -> list[int]:
    return sorted({number % 10 for number in numbers})


def _tail_groups(numbers: list[int]) -> list[dict]:
    return [
        {"tail": tail, "label": f"{tail}尾", "numbers": [number for number in numbers if number % 10 == tail]}
        for tail in _tails(numbers)
    ]


def _twins(numbers: list[int]) -> list[int]:
    twin_numbers = {11, 22, 33, 44, 55, 66, 77}
    return [number for number in numbers if number in twin_numbers]


def _patch_numbers(numbers: list[int]) -> list[int]:
    candidates = []
    for number in numbers:
        for diff in (1, 2, 10):
            for value in (number - diff, number + diff):
                if 1 <= value <= 80 and value not in numbers and value not in candidates:
                    candidates.append(value)
    return sorted(candidates[:8])


def _prediction_from_history(
    record: dict | None,
    current_draw: dict | None,
    detected_latest_issue: Any = None,
    *,
    allow_slow_lookups: bool = True,
    transform_diagnostics: dict[str, Any] | None = None,
) -> dict | None:
    transform_total_started = time.perf_counter()
    if not record:
        if transform_diagnostics is not None:
            transform_diagnostics["transform_total_ms"] = round((time.perf_counter() - transform_total_started) * 1000, 2)
            transform_diagnostics["transform_accounted_ms"] = 0.0
            transform_diagnostics["transform_unaccounted_ms"] = transform_diagnostics["transform_total_ms"]
        return None

    stage_started = time.perf_counter()
    numbers = _as_int_list(record.get("recommend_numbers"))
    _record_prediction_transform_stage(
        transform_diagnostics,
        "prediction_number_parsing",
        stage_started,
        number_count=len(numbers),
        io_type="python",
        query_count=0,
    )
    if not numbers:
        if transform_diagnostics is not None:
            total_ms = round((time.perf_counter() - transform_total_started) * 1000, 2)
            accounted_ms = round(sum(stage.get("elapsed_ms") or 0 for stage in transform_diagnostics.get("transform_stages", {}).values()), 2)
            transform_diagnostics["transform_total_ms"] = total_ms
            transform_diagnostics["transform_accounted_ms"] = accounted_ms
            transform_diagnostics["transform_unaccounted_ms"] = round(max(total_ms - accounted_ms, 0.0), 2)
        return None

    stage_started = time.perf_counter()
    database_latest_issue = (current_draw or {}).get("issue")
    current_issue = detected_latest_issue or database_latest_issue
    based_on_issue = (
        record.get("issue")
        or current_issue
    )
    target_issue = (
        record.get("prediction_issue")
        or record.get("target_issue")
    )
    _record_prediction_transform_stage(
        transform_diagnostics,
        "record_context_normalization",
        stage_started,
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    if _valid_production_issue(target_issue):
        target_issue_source = "stored"
    else:
        derived = _derive_next_issue(based_on_issue)
        target_issue = derived
        target_issue_source = "derived_from_source_issue" if derived else "unavailable"
    status = _target_status(target_issue, current_issue)
    freshness = _dashboard_prediction_freshness(target_issue, current_issue)
    _record_prediction_transform_stage(
        transform_diagnostics,
        "issue_status_freshness",
        stage_started,
        target_issue_source=target_issue_source,
        io_type="python",
        query_count=0,
    )
    if freshness.get("lag_issues") is not None and freshness.get("lag_issues") > 1:
        if transform_diagnostics is not None:
            total_ms = round((time.perf_counter() - transform_total_started) * 1000, 2)
            accounted_ms = round(sum(stage.get("elapsed_ms") or 0 for stage in transform_diagnostics.get("transform_stages", {}).values()), 2)
            transform_diagnostics["transform_total_ms"] = total_ms
            transform_diagnostics["transform_accounted_ms"] = accounted_ms
            transform_diagnostics["transform_unaccounted_ms"] = round(max(total_ms - accounted_ms, 0.0), 2)
        return None

    stage_started = time.perf_counter()
    refresh = prediction_refresh_status(current_draw, record)
    expected_time, expected_source = _expected_draw_time(record, current_draw)
    _record_prediction_transform_stage(
        transform_diagnostics,
        "verification_status_processing",
        stage_started,
        helper_calls=["prediction_refresh_status", "_expected_draw_time"],
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    if based_on_issue and str(based_on_issue) == str((current_draw or {}).get("issue") or ""):
        based_draw = current_draw
    else:
        based_draw = get_official_draw_by_issue(based_on_issue) if based_on_issue and allow_slow_lookups else None
    can_resolve_based_time = (
        allow_slow_lookups
        or based_draw is not None
        or bool((based_draw or {}).get("draw_time"))
        or bool((based_draw or {}).get("collected_at"))
    )
    if allow_slow_lookups:
        based_time = _based_on_time(based_on_issue, based_draw) if can_resolve_based_time else _snapshot_based_on_time(record, based_draw)
        hidden_lookup_count = int(
            bool(based_on_issue and str(based_on_issue) != str((current_draw or {}).get("issue") or "") and allow_slow_lookups)
        )
        operation_event_lookup_count = int(bool(can_resolve_based_time and based_on_issue and not (based_draw or {}).get("draw_time")))
        based_time_query_count = hidden_lookup_count + operation_event_lookup_count
        based_time_io_type = "db_possible" if based_time_query_count else "python"
        based_time_helpers = ["get_official_draw_by_issue", "_based_on_time", "get_latest_operation_event"]
    else:
        based_time = _snapshot_based_on_time(record, based_draw)
        based_time_query_count = 0
        based_time_io_type = "python"
        based_time_helpers = ["_snapshot_based_on_time"]
    _record_prediction_transform_stage(
        transform_diagnostics,
        "based_on_time_lookup",
        stage_started,
        helper_calls=based_time_helpers,
        hidden_db_lookup_count=based_time_query_count,
        slow_lookup_allowed=allow_slow_lookups,
        time_source=based_time["based_on_time_source"],
        io_type=based_time_io_type,
        query_count=based_time_query_count,
    )

    stage_started = time.perf_counter()
    recommendation_warning = None
    if len(numbers) < 20:
        recommendation_warning = f"目前僅產生 {len(numbers)} 個有效推薦號碼"
    _record_prediction_transform_stage(
        transform_diagnostics,
        "recommendation_warning",
        stage_started,
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    metadata_payload = {
        "super_number": record.get("super_number"),
        "confidence": record.get("confidence") or 0,
        "release_version": record.get("release_version"),
        "git_commit_hash": record.get("git_commit_hash"),
        "production_generation": record.get("production_generation"),
        "feature_version": record.get("feature_version"),
        "phase": record.get("phase"),
        "production_start_issue": record.get("production_start_issue"),
        "production_start_at": record.get("production_start_at"),
        "model_scores": record.get("model_scores") or {},
        "winning_model": record.get("winning_model"),
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "read_layer": record.get("read_layer") or {},
        "reasons": record.get("reasons") or [],
    }
    _record_prediction_transform_stage(
        transform_diagnostics,
        "metadata_extraction",
        stage_started,
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    twins = _twins(numbers)
    consecutive = record.get("consecutive") or _pairs(numbers, 1)
    patch_numbers = record.get("patch_numbers") or _patch_numbers(numbers)
    tails = record.get("tails") or _tails(numbers)
    tail_groups = _tail_groups(numbers)
    big_small = record.get("big_small") or _big_small(numbers)
    odd_even = record.get("odd_even") or _odd_even(numbers)
    _record_prediction_transform_stage(
        transform_diagnostics,
        "rule_strategy_processing",
        stage_started,
        helper_calls=["_twins", "_pairs", "_patch_numbers", "_tails", "_tail_groups", "_big_small", "_odd_even"],
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    alerts = _alerts(numbers, metadata_payload["super_number"])
    _record_prediction_transform_stage(
        transform_diagnostics,
        "alert_processing",
        stage_started,
        helper_calls=["_alerts", "_pairs", "_patch_numbers"],
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    payload = {
        "target_issue": target_issue,
        "prediction_issue": target_issue,
        "target_issue_source": target_issue_source,
        "source_issue": based_on_issue,
        "based_on_issue": based_on_issue,
        "based_on_draw_time": based_time["based_on_draw_time"],
        "based_on_time_source": based_time["based_on_time_source"],
        "based_on_draw_exists": bool(based_draw),
        "latest_official_issue": current_issue,
        "database_latest_issue": database_latest_issue,
        "expected_target_issue": freshness.get("expected_target_issue") or refresh.get("expected_target_issue"),
        "is_current": status["is_current"],
        "status": freshness["dashboard_status"],
        "target_status": status["status"],
        "stale_status": freshness["stale_status"],
        "stale_status_label": freshness["stale_status_label"],
        "refresh_status": refresh.get("refresh_status"),
        "refresh_reason": refresh.get("refresh_reason"),
        "last_refresh_attempt": refresh.get("last_refresh_attempt"),
        "last_refresh_success": refresh.get("last_refresh_success"),
        "is_stale": freshness["is_stale"],
        "lag_issues": freshness["lag_issues"],
        "expected_draw_time": expected_time,
        "target_draw_time": expected_time,
        "expected_draw_time_source": expected_source,
        "generated_at": record.get("predict_time") or record.get("created_at"),
        "main_numbers": numbers,
        "recommend_numbers": numbers,
        "recommendation_warning": recommendation_warning,
        "backup_numbers": [],
        "twins": twins,
        "consecutive": consecutive,
        "patch_numbers": patch_numbers,
        "tails": tails,
        "tail_groups": tail_groups,
        "big_small": big_small,
        "odd_even": odd_even,
        "model_version": "V7",
        "alerts": alerts,
        "history": {},
        "laowanjia": {},
        **metadata_payload,
    }
    _record_prediction_transform_stage(
        transform_diagnostics,
        "final_payload_construction",
        stage_started,
        io_type="python",
        query_count=0,
    )
    if transform_diagnostics is not None:
        total_ms = round((time.perf_counter() - transform_total_started) * 1000, 2)
        accounted_ms = round(sum(stage.get("elapsed_ms") or 0 for stage in transform_diagnostics.get("transform_stages", {}).values()), 2)
        transform_diagnostics["transform_total_ms"] = total_ms
        transform_diagnostics["transform_accounted_ms"] = accounted_ms
        transform_diagnostics["transform_unaccounted_ms"] = round(max(total_ms - accounted_ms, 0.0), 2)
    return payload


def _attach_next_prediction_diagnostics(
    payload: dict | None,
    diagnostics: dict[str, Any],
    transform_started: float,
    diagnostic_started: float,
) -> dict | None:
    transform_ms = round((time.perf_counter() - transform_started) * 1000, 2)
    diagnostics["transform_ms"] = transform_ms
    diagnostics.setdefault("transform_total_ms", transform_ms)
    transform_stages = diagnostics.get("transform_stages") or {}
    accounted_ms = round(sum(stage.get("elapsed_ms") or 0 for stage in transform_stages.values()), 2)
    diagnostics.setdefault("transform_accounted_ms", accounted_ms)
    diagnostics.setdefault("transform_unaccounted_ms", round(max(diagnostics["transform_total_ms"] - diagnostics["transform_accounted_ms"], 0.0), 2))
    diagnostics["total_execution_observed_ms"] = round((time.perf_counter() - diagnostic_started) * 1000, 2)
    diagnostics["stages"].append(
        {
            "stage": "prediction_from_history",
            "duration_ms": transform_ms,
            "db_timing": None,
            "query_count": 0,
        }
    )
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["diagnostics"] = diagnostics
    return payload


def _alert_level(value: int) -> dict:
    value = max(0, min(5, int(value or 0)))
    return {"stars": value, "percent": value * 20}


def _alerts(numbers: list[int], super_number: int | None) -> dict:
    consecutive = len(_pairs(numbers, 1))
    twins = len(_pairs(numbers, 2))
    cluster = max(
        sum(1 for number in numbers if start <= number <= start + 9)
        for start in range(1, 81, 10)
    ) if numbers else 0
    patch = len(_patch_numbers(numbers))
    return {
        "cluster_alert": _alert_level(cluster - 2),
        "patch_alert": _alert_level(patch // 2),
        "twin_alert": _alert_level(twins),
        "consecutive_alert": _alert_level(consecutive),
        "super_alert": _alert_level(3 if super_number else 1),
    }
def _pending_next_prediction(current_draw: dict | None, detected_latest_issue: Any = None) -> dict:
    database_latest_issue = (current_draw or {}).get("issue")
    current_issue = detected_latest_issue or database_latest_issue
    expected_target = _derive_next_issue(current_issue)
    return {
        "target_issue": expected_target,
        "prediction_issue": expected_target,
        "target_issue_source": "expected_from_latest_issue",
        "source_issue": current_issue,
        "based_on_issue": current_issue,
        "latest_official_issue": current_issue,
        "database_latest_issue": database_latest_issue,
        "expected_target_issue": expected_target,
        "is_current": False,
        "status": "prediction_pending",
        "target_status": "pending",
        "stale_status": "prediction_pending",
        "stale_status_label": "prediction_pending",
        "refresh_status": "prediction_pending",
        "refresh_reason": "latest_prediction_missing_or_expired",
        "is_stale": True,
        "stale": True,
        "lag_issues": None,
        "main_numbers": [],
        "recommend_numbers": [],
        "recommendation_warning": "Latest production prediction is pending for the newest official draw.",
        "production_valid": False,
        "source": "fallback",
        "read_layer": {"query_name": "production_latest_prediction_pending", "production_filtered": True},
        "reasons": ["Latest production prediction is pending for the newest official draw."],
        "alerts": {},
        "history": {},
        "laowanjia": {},
    }


def _enrich_dashboard_card_v1(next_prediction: dict, current_draw: dict | None) -> dict:
    payload = dict(next_prediction or {})
    numbers = _as_int_list(payload.get("recommend_numbers") or payload.get("main_numbers"))
    record = {**payload, "recommend_numbers": numbers}
    size_payload = size_prediction(numbers, record)
    odd_even_payload = odd_even_prediction(numbers, record)
    high_probability = high_probability_numbers(record, numbers, payload.get("rule_library"))
    super_candidate_list = [number for number in super_candidates(record) if number in numbers]
    payload.update(
        {
            "main_numbers": numbers,
            "recommend_numbers": numbers,
            "numbers": numbers,
            "high_probability_numbers": high_probability["numbers"],
            "top_five": high_probability["numbers"],
            "high_probability_details": high_probability["details"],
            "high_probability_source": high_probability["source"],
            "high_probability_fallback_used": high_probability["fallback_used"],
            "high_probability_fallback_reason": high_probability.get("fallback_reason"),
            "size_prediction": size_payload,
            "odd_even_prediction": odd_even_payload,
            "confidence": confidence_ratio(payload.get("confidence") or payload.get("confidence_percent") or 0),
            "confidence_percent": confidence_percent(payload.get("confidence_percent") or payload.get("confidence") or 0),
            "super_candidates": super_candidate_list,
            "super_candidate": super_candidate_list[0] if super_candidate_list else None,
        }
    )
    diagnostics_card = {
        **payload,
        "current_draw": current_draw or {},
    }
    payload["diagnostics"] = {
        **(payload.get("diagnostics") or {}),
        "dashboard_card_v1": validation_diagnostics(diagnostics_card),
        "fallbacks": {
            "high_probability": high_probability.get("fallback_reason"),
            "size_prediction": size_payload.get("fallback_reason"),
            "odd_even_prediction": odd_even_payload.get("fallback_reason"),
        },
    }
    return payload


def _official_super_in_numbers(draw: dict | None, numbers: list[int]) -> int | None:
    super_number = _as_int((draw or {}).get("super_number"))
    return super_number if super_number in numbers else None

def _unavailable_previous_result(requested_target_issue: Any) -> dict:
    return {
        "previous_result_mode": "unavailable",
        "requested_target_issue": requested_target_issue,
        "displayed_target_issue": None,
        "target_issue": None,
        "verified_issue": None,
        "predicted_numbers": [],
        "draw_numbers": [],
        "official_numbers": [],
        "matched_numbers": [],
        "missed_numbers": [],
        "hit_count": 0,
        "prediction_count": 0,
        "hit_denominator": 20,
        "prediction_status": "unavailable",
        "verification_status": "unavailable",
        "learning_used": False,
        "learning_status": "unavailable",
    }


def _verification(
    record: dict | None,
    draw: dict | None,
    diagnostics: dict[str, Any] | None = None,
    *,
    allow_slow_lookups: bool = True,
) -> dict | None:
    if not record:
        return None
    stage_started = time.perf_counter()
    predicted = _as_int_list(record.get("recommend_numbers"))
    draw_numbers = _as_int_list(record.get("winning_numbers") or (draw or {}).get("numbers"))
    _record_prediction_transform_stage(
        diagnostics,
        "prediction_number_processing",
        stage_started,
        predicted_count=len(predicted),
        draw_count=len(draw_numbers),
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    matched_set = set(draw_numbers)
    matched = [number for number in predicted if number in matched_set]
    missed = [number for number in predicted if number not in matched_set]
    _record_prediction_transform_stage(
        diagnostics,
        "matching_comparison",
        stage_started,
        matched_count=len(matched),
        missed_count=len(missed),
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    predicted_super = _as_int(record.get("super_number"))
    actual_super = _as_int((draw or {}).get("super_number"))
    if actual_super is None:
        actual_super = _as_int(record.get("actual_super"))
    _record_prediction_transform_stage(
        diagnostics,
        "super_number_processing",
        stage_started,
        io_type="python",
        query_count=0,
    )

    stage_started = time.perf_counter()
    draw_time_payload = (
        _based_on_time(record.get("prediction_issue"), draw)
        if allow_slow_lookups
        else _snapshot_based_on_time(record, draw)
    )
    hidden_time_lookup = bool(
        allow_slow_lookups
        and record.get("prediction_issue")
        and not (draw or {}).get("draw_time")
    )
    _record_prediction_transform_stage(
        diagnostics,
        "based_on_time_helper",
        stage_started,
        helper_calls=["_based_on_time", "get_latest_operation_event"] if allow_slow_lookups else ["_snapshot_based_on_time"],
        hidden_db_lookup_count=int(hidden_time_lookup),
        slow_lookup_allowed=allow_slow_lookups,
        time_source=draw_time_payload.get("based_on_time_source"),
        io_type="db_possible" if hidden_time_lookup else "python",
        query_count=int(hidden_time_lookup),
    )

    stage_started = time.perf_counter()
    payload = {
        "target_issue": record.get("prediction_issue"),
        "prediction_status": record.get("prediction_status"),
        "prediction_created_at": record.get("predict_time") or record.get("created_at"),
        "draw_time": draw_time_payload["based_on_draw_time"] or _format_draw_time(record.get("draw_time")),
        "draw_time_source": draw_time_payload["based_on_time_source"],
        "predicted_numbers": predicted,
        "draw_numbers": draw_numbers,
        "official_numbers": draw_numbers,
        "matched_numbers": matched,
        "missed_numbers": missed,
        "hit_count": len(matched),
        "prediction_count": len(predicted),
        "hit_denominator": len(predicted),
        "hit_label": _hit_label(len(matched)),
        "super_number_predicted": predicted_super,
        "super_number_actual": actual_super,
        "official_super_number": actual_super,
        "super_number_hit": bool(
            predicted_super is not None
            and actual_super is not None
            and predicted_super == actual_super
        ),
        "verified_issue": record.get("verified_issue") or (record.get("prediction_issue") if draw_numbers else None),
        "verified_at": record.get("verified_at") or (record.get("updated_at") if draw_numbers else None),
        "learning_used": bool(record.get("learning_used")),
        "learning_status": "completed" if record.get("learning_used") else "waiting",
        "learned_at": record.get("learned_at"),
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "verification_status": "verified" if draw_numbers else "pending",
    }
    _record_prediction_transform_stage(
        diagnostics,
        "result_build",
        stage_started,
        io_type="python",
        query_count=0,
    )
    return payload


def _history_item(record: dict) -> dict:
    predicted = _as_int_list(record.get("recommend_numbers"))
    official = _as_int_list(record.get("winning_numbers"))
    matched = _as_int_list(record.get("matched_numbers"))
    if official and not matched:
        official_set = set(official)
        matched = [number for number in predicted if number in official_set]
    missed = _as_int_list(record.get("missed_numbers"))
    if official and not missed:
        official_set = set(official)
        missed = [number for number in predicted if number not in official_set]
    return {
        "id": record.get("id"),
        "issue": record.get("issue"),
        "based_on_issue": record.get("issue"),
        "target_issue": record.get("prediction_issue"),
        "prediction_issue": record.get("prediction_issue"),
        "created_at": record.get("predict_time") or record.get("created_at"),
        "prediction_created_at": record.get("predict_time") or record.get("created_at"),
        "verified_at": record.get("verified_at"),
        "learning_used": bool(record.get("learning_used")),
        "learned_at": record.get("learned_at"),
        "recommend_numbers": predicted,
        "winning_numbers": official,
        "matched_numbers": matched,
        "missed_numbers": missed,
        "hit_count": record.get("hit_count") if record.get("hit_count") is not None else len(matched),
        "prediction_count": record.get("prediction_count") or len(predicted),
        "super_number": record.get("super_number"),
        "official_super_number": record.get("actual_super"),
        "super_number_hit": bool(record.get("super_number_hit") or record.get("super_hit")),
        "prediction_status": record.get("prediction_status"),
        "verification_status": "verified" if official or record.get("prediction_status") == "verified" else "pending",
        "learning_status": "completed" if record.get("learning_used") else "waiting",
        "source": record.get("source") or "production_history",
        "trigger": record.get("trigger") or "production_read_layer",
        "production_valid": is_production_prediction(record),
        "release_version": record.get("release_version"),
        "git_commit_hash": record.get("git_commit_hash"),
        "production_generation": record.get("production_generation"),
        "model_version": record.get("model_version"),
        "feature_version": record.get("feature_version"),
    }


RULE_LIBRARY_NAMES = [
    (item["key"], item["label"])
    for item in get_rule_registry()
]

def _rule_snapshot_item_to_dashboard(item: dict) -> dict:
    status = item.get("status")
    impact = "中"
    if status in {"insufficient", "experimental", "disabled"}:
        impact = "資料不足"
    else:
        try:
            impact = "高" if float(item.get("score") or 0) >= 70 else "中"
        except Exception:
            impact = "中"

    return {
        "key": item.get("key"),
        "name": item.get("label"),
        "status": status,
        "score": item.get("score"),
        "confidence": item.get("confidence"),
        "reason": item.get("reason"),
        "impact": impact,
        "candidate_numbers": item.get("candidate_numbers") or [],
    }


def _rule_library(analysis: dict | None, prediction: dict) -> dict:
    source = analysis or {}
    snapshot = _rule_snapshot_for_dashboard(source, prediction)
    snapshot_rules = snapshot.get("rules") or []
    rules = [_rule_snapshot_item_to_dashboard(item) for item in snapshot_rules]
    completed = sum(1 for item in rules if item.get("status") == "ready")
    labels_by_key = {key: label for key, label in RULE_LIBRARY_NAMES}

    def score_value(item: dict) -> float:
        try:
            return float(item.get("score") or 0)
        except Exception:
            return 0.0

    snapshot_primary = [
        labels_by_key.get(key, key)
        for key in ((snapshot.get("aggregate") or {}).get("primary_rules") or [])
    ]
    primary = [
        item["name"]
        for item in sorted(
            rules,
            key=lambda item: (item.get("status") == "ready", score_value(item)),
            reverse=True,
        )
        if item.get("status") == "ready"
    ][:5]
    if snapshot_primary:
        primary = snapshot_primary
    return {
        "title": "AI 推薦依據",
        "completed_count": completed,
        "total_count": len(snapshot_rules) or len(RULE_LIBRARY_NAMES),
        "summary": f"本期主要依據：{'、'.join(primary[:3])}" if primary else "尚未建立完整分析摘要",
        "primary_rules": primary,
        "rules": rules,
        "laowanjia_index": source.get("laowanjia_score"),
        "hot_zones": source.get("hot_zone") or [],
        "cold_zone": source.get("cold_zone"),
        "star_prediction": {
            "three_star": source.get("three_star"),
            "four_star": source.get("four_star"),
            "five_star": source.get("five_star"),
            "six_star": source.get("six_star"),
        },
        "super_trajectory": ((source.get("ai_score") or {}).get("super_number_trajectory_recovery") or {}),
        "cluster_recovery": ((source.get("ai_score") or {}).get("cluster_aftershock_recovery") or {}),
    }


def _rule_snapshot_for_dashboard(
    analysis: dict,
    prediction: dict,
    diagnostics: dict | None = None,
) -> dict:
    source_issue = _valid_production_issue(
        analysis.get("issue") or prediction.get("issue") or prediction.get("based_on_issue")
    )
    target_issue = _valid_production_issue(
        prediction.get("prediction_issue") or prediction.get("target_issue")
    )
    if source_issue and get_rule_snapshot is not None:
        lookup_started = time.perf_counter()
        lookup_timing: dict[str, Any] = {"query_count": 1, "db_calls": 1, "connection_path": "direct_connection"}
        stored = None
        try:
            if diagnostics is not None and callable(get_rule_snapshot_with_timing):
                stored, lookup_timing = get_rule_snapshot_with_timing(
                    source_issue=source_issue,
                    target_issue=target_issue,
                    use_dashboard_read_pool=False,
                )
            else:
                stored = get_rule_snapshot(source_issue=source_issue, target_issue=target_issue)
            snapshot = (stored or {}).get("snapshot_json") if isinstance(stored, dict) else None
            _card_two_diag_stage(
                diagnostics,
                "_card_two_rules.rule_snapshot_lookup",
                lookup_started,
                **dict(lookup_timing or {}),
                found=bool(stored),
                snapshot_valid=isinstance(snapshot, dict) and bool(snapshot.get("rules")),
                source_issue=source_issue,
                target_issue=target_issue,
                hidden_db_calls=int((lookup_timing or {}).get("db_calls") or 0),
            )
            if isinstance(snapshot, dict) and snapshot.get("rules"):
                return snapshot
        except Exception:
            logger.exception("dashboard rule snapshot lookup failed")
            _card_two_diag_stage(
                diagnostics,
                "_card_two_rules.rule_snapshot_lookup",
                lookup_started,
                **dict(lookup_timing or {}),
                found=False,
                snapshot_valid=False,
                source_issue=source_issue,
                target_issue=target_issue,
                error=True,
            )
    build_started = time.perf_counter()
    snapshot = build_rule_snapshot(
        analysis,
        prediction,
        source_issue=source_issue,
        target_issue=target_issue,
    )
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.rule_snapshot_build_fallback",
        build_started,
        query_count=0,
        db_calls=0,
        rule_count=len((snapshot or {}).get("rules") or []),
        source_issue=source_issue,
        target_issue=target_issue,
    )
    return snapshot


def _card_two_empty(requested_issue: Any = None) -> dict:
    issue = _valid_production_issue(requested_issue)
    return {
        "title": CARD_TWO_TITLE,
        "available": False,
        "report_status": "unavailable",
        "status_text": "尚無已完成的最終分析報告",
        "issue": issue,
        "requested_issue": issue,
        "prediction_numbers": [],
        "official_numbers": [],
        "matched_numbers": [],
        "hit_count": None,
        "prediction_count": None,
        "super_number": None,
        "super_number_hit": None,
        "super_number_status_text": "資料不足",
        "size_result": {"status_text": "資料不足"},
        "odd_even_result": {"status_text": "資料不足"},
        "actual_consecutive_groups": {
            "three_star": [],
            "four_star": [],
            "five_star": [],
            "six_star": [],
        },
        "finalized_at": None,
        "rules": [],
        "fallback_text": "AI 正在等待足夠的正式開獎與驗證資料。",
    }


def _card_two_status_text(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"success", "ready", "completed", "ok", "hit"}:
        return "成功"
    if normalized in {"partial", "partial_hit"}:
        return "部分命中"
    if normalized in {"insufficient", "missing", "unavailable"}:
        return "資料不足"
    if normalized in {"experimental", "disabled", "not_run", "skipped"}:
        return "未執行"
    if normalized in {"miss", "no_hit", "failed"}:
        return "未命中"
    return "成功" if normalized == "verified" else "資料不足"


def _card_two_score_text(value: Any) -> str:
    try:
        score = float(value)
    except Exception:
        return "已完成評估"
    if 0 <= score <= 1:
        return f"{score * 100:.1f}%".replace(".0%", "%")
    if 0 <= score <= 10:
        return f"{score:.1f} / 10".replace(".0 / 10", " / 10")
    if 10 < score <= 100:
        return f"{score:.1f}%".replace(".0%", "%")
    return "已完成評估"


def _card_two_summary(rule_name: str, matched: list[int], status_text: str) -> str:
    if matched:
        return f"{rule_name}規則命中 {len(matched)} 個號碼：{'、'.join(f'{n:02d}' for n in matched)}。"
    if status_text == "未命中":
        return f"{rule_name}規則本期未命中，保留為後續觀察。"
    return f"{rule_name}規則已完成正式評估。"


def _card_two_rule_item(item: dict, official_numbers: list[int]) -> dict | None:
    if not isinstance(item, dict):
        return None
    key = item.get("rule_key") or item.get("key")
    label_by_key = dict(CARD_TWO_RULE_ORDER)
    label = item.get("rule_name_zh") or item.get("label") or label_by_key.get(key)
    if not key or key not in label_by_key:
        return None
    candidates = _as_int_list(
        item.get("candidates")
        or item.get("candidate_numbers")
        or item.get("numbers")
        or []
    )
    matched = _as_int_list(item.get("matched_numbers"))
    if not matched and candidates and official_numbers:
        official_set = set(official_numbers)
        matched = [number for number in candidates if number in official_set]
    matched = [number for number in matched if number in set(candidates) and number in set(official_numbers)]
    if not candidates and not matched and item.get("score") is None and item.get("confidence") is None:
        return None
    raw_status = item.get("status")
    if raw_status in (None, "", "ready"):
        status_text = "成功" if matched else "未命中"
    else:
        status_text = _card_two_status_text(raw_status)
    score_value = item.get("score")
    if score_value is None:
        score_value = item.get("confidence")
    summary = str(item.get("summary") or item.get("reason") or "").strip()
    if not summary or summary == "dashboard fallback":
        summary = _card_two_summary(str(label), matched, status_text)
    return {
        "rule_key": key,
        "rule_name_zh": label,
        "candidates": candidates,
        "actual_numbers": matched,
        "matched_numbers": matched,
        "status": raw_status or ("success" if matched else "no_hit"),
        "status_text": status_text,
        "score": score_value,
        "score_text": _card_two_score_text(score_value),
        "summary": summary,
        "evidence_summary": "",
    }


def _card_two_rules(
    analysis: dict | None,
    prediction: dict,
    official_numbers: list[int],
    diagnostics: dict | None = None,
) -> list[dict]:
    total_started = time.perf_counter()
    try:
        try:
            snapshot = _rule_snapshot_for_dashboard(analysis or {}, prediction, diagnostics=diagnostics)
        except TypeError as exc:
            if "diagnostics" not in str(exc):
                raise
            snapshot = _rule_snapshot_for_dashboard(analysis or {}, prediction)
    except Exception:
        logger.exception("dashboard card two rule snapshot failed")
        _card_two_diag_stage(diagnostics, "_card_two_rules.total", total_started, query_count=0, db_calls=0, error=True)
        return []
    parse_started = time.perf_counter()
    by_key = {
        (item.get("rule_key") or item.get("key")): item
        for item in (snapshot.get("rules") or [])
        if isinstance(item, dict)
    }
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.rule_parsing",
        parse_started,
        rule_count=len((snapshot.get("rules") or [])),
        keyed_rule_count=len(by_key),
        query_count=0,
        db_calls=0,
    )
    rules: list[dict] = []
    selection_ms = 0.0
    evaluation_ms = 0.0
    format_ms = 0.0
    for key, label in CARD_TWO_RULE_ORDER:
        selection_started = time.perf_counter()
        item = by_key.get(key)
        if not item:
            selection_ms += (time.perf_counter() - selection_started) * 1000
            continue
        selection_ms += (time.perf_counter() - selection_started) * 1000
        evaluation_started = time.perf_counter()
        converted = _card_two_rule_item({**item, "rule_name_zh": item.get("label") or label}, official_numbers)
        evaluation_ms += (time.perf_counter() - evaluation_started) * 1000
        if converted:
            format_started = time.perf_counter()
            rules.append(converted)
            format_ms += (time.perf_counter() - format_started) * 1000
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.rule_selection_filtering",
        time.perf_counter(),
        elapsed_ms=round(selection_ms, 2),
        selected_rule_count=len(rules),
        query_count=0,
        db_calls=0,
    )
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.rule_evaluation",
        time.perf_counter(),
        elapsed_ms=round(evaluation_ms, 2),
        query_count=0,
        db_calls=0,
    )
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.format_payload",
        time.perf_counter(),
        elapsed_ms=round(format_ms, 2),
        query_count=0,
        db_calls=0,
    )
    snapshot_stage = ((diagnostics or {}).get("stages") or {}).get("_card_two_rules.rule_snapshot_lookup") or {}
    _card_two_diag_stage(
        diagnostics,
        "_card_two_rules.total",
        total_started,
        query_count=int(snapshot_stage.get("query_count") or 0),
        db_calls=int(snapshot_stage.get("db_calls") or 0),
        rule_count=len(rules),
    )
    return rules


def _card_two_analysis_by_issue(issue: Any) -> dict | None:
    record, _timing = _card_two_analysis_by_issue_with_timing(issue)
    return record


def _card_two_analysis_by_issue_with_timing(
    issue: Any,
    *,
    use_dashboard_read_pool: bool = False,
) -> tuple[dict | None, dict]:
    if not issue:
        return None, {"query_count": 0, "db_calls": 0}
    try:
        from database import analysis_store as analysis_store_module
    except ImportError as exc:
        logger.warning("dashboard card two analysis fallback unavailable: %s", exc)
        return None, {"query_count": 0, "db_calls": 0, "error_type": type(exc).__name__}
    timed_lookup = getattr(analysis_store_module, "get_analysis_history_by_issue_with_timing", None)
    if callable(timed_lookup):
        try:
            record, timing = timed_lookup(str(issue), use_dashboard_read_pool=use_dashboard_read_pool)
            timing = dict(timing or {})
            timing.setdefault("query_count", 1)
            timing.setdefault("db_calls", 1)
            if use_dashboard_read_pool:
                timing.setdefault("connection_path", "dashboard_read_pool")
            return record, timing
        except Exception as exc:
            logger.exception("dashboard card two timed analysis lookup failed")
            return None, {"query_count": 1, "db_calls": 1, "error_type": type(exc).__name__}
    lookup = getattr(analysis_store_module, "get_analysis_history_by_issue", None)
    if not callable(lookup):
        logger.warning("dashboard card two analysis fallback helper missing")
        return None, {"query_count": 0, "db_calls": 0, "error_type": "missing_lookup"}
    try:
        started = time.perf_counter()
        record = lookup(str(issue))
        return record, {
            "query_count": 1,
            "db_calls": 1,
            "total_ms": round((time.perf_counter() - started) * 1000, 2),
            "timing_source": "fallback_total_only",
        }
    except Exception as exc:
        logger.exception("dashboard card two analysis lookup failed")
        return None, {"query_count": 1, "db_calls": 1, "error_type": type(exc).__name__}


def _card_two_diag_stage(diagnostics: dict | None, stage: str, started: float, **extra: Any) -> None:
    if diagnostics is None:
        return
    payload = {"elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    payload.update(extra)
    diagnostics.setdefault("stages", {})[stage] = payload


def _card_two_record_actual_numbers(record: dict | None) -> list[int]:
    source = record or {}
    return _as_int_list(
        source.get("winning_numbers")
        or source.get("actual_numbers")
        or source.get("official_numbers")
        or source.get("draw_numbers")
    )


def _card_two_current_official_draw(issue: Any, current_draw: dict | None) -> dict | None:
    if not issue or not current_draw:
        return None
    if _valid_production_issue((current_draw or {}).get("issue")) != str(issue):
        return None
    numbers = _as_int_list((current_draw or {}).get("numbers"))
    super_number = _as_int((current_draw or {}).get("super_number"))
    if len(numbers) != 20 or super_number is None:
        return None
    return current_draw


def _is_card_two_finalized_candidate(record: dict, current_issue: Any = None) -> bool:
    if not is_production_prediction(record):
        return False
    issue = _valid_production_issue(record.get("prediction_issue") or record.get("target_issue"))
    if not issue:
        return False
    status = str(record.get("prediction_status") or "").strip().lower()
    if status in CARD_TWO_FINALIZED_DISALLOWED:
        return False
    if status != "verified":
        return False
    if not record.get("learning_used"):
        return False
    prediction_numbers = _as_int_list(record.get("recommend_numbers"))
    official_numbers = _card_two_record_actual_numbers(record)
    if len(prediction_numbers) != 20 or len(official_numbers) != 20:
        return False
    if current_issue and _as_int(current_issue) is not None:
        issue_int = _as_int(issue)
        current_int = _as_int(current_issue)
        if issue_int is None or current_int is None or current_int < issue_int:
            return False
    return True


def _card_two_distribution_result(predicted: Any, actual: Any) -> dict:
    predicted_label = str(predicted or "").strip().lower()
    actual_label = str(actual or "").strip().lower()
    if not predicted_label or not actual_label:
        return {
            "predicted": predicted_label or None,
            "actual": actual_label or None,
            "hit": None,
            "status_text": "資料不足",
        }
    hit = predicted_label == actual_label
    return {
        "predicted": predicted_label,
        "actual": actual_label,
        "hit": hit,
        "status_text": "命中" if hit else "未命中",
    }


def _consecutive_combinations(numbers: list[int], size: int) -> list[list[int]]:
    unique = sorted(set(numbers))
    if size < 2:
        return []
    combos: list[list[int]] = []
    run: list[int] = []
    previous: int | None = None
    for number in unique:
        if previous is None or number == previous + 1:
            run.append(number)
        else:
            if len(run) >= size:
                combos.extend(run[index : index + size] for index in range(0, len(run) - size + 1))
            run = [number]
        previous = number
    if len(run) >= size:
        combos.extend(run[index : index + size] for index in range(0, len(run) - size + 1))
    return combos


def _card_two_actual_consecutive_groups(official_numbers: list[int]) -> dict:
    return {
        "three_star": _consecutive_combinations(official_numbers, 3),
        "four_star": _consecutive_combinations(official_numbers, 4),
        "five_star": _consecutive_combinations(official_numbers, 5),
        "six_star": _consecutive_combinations(official_numbers, 6),
    }


def get_latest_finalized_analysis_report(
    history_records: list[dict] | None = None,
    current_draw: dict | None = None,
    target_issue: Any = None,
    diagnostics: dict | None = None,
) -> dict | None:
    total_started = time.perf_counter()
    source_started = time.perf_counter()
    records = history_records if history_records is not None else get_prediction_history_records(100)
    _card_two_diag_stage(
        diagnostics,
        "get_latest_finalized_analysis_report.source_records",
        source_started,
        source="provided" if history_records is not None else "query",
        row_count=len(records or []),
        query_count=0 if history_records is not None else 1,
    )
    current_issue = (current_draw or {}).get("issue")
    requested_target = _valid_production_issue(target_issue)
    filter_started = time.perf_counter()
    candidates = [
        record
        for record in (records or [])
        if _is_card_two_finalized_candidate(record, current_issue)
    ]
    if requested_target:
        candidates = [
            record
            for record in candidates
            if _valid_production_issue(record.get("prediction_issue") or record.get("target_issue")) == requested_target
        ]
    if not candidates:
        _card_two_diag_stage(
            diagnostics,
            "get_latest_finalized_analysis_report.filter_candidates",
            filter_started,
            candidate_count=0,
            requested_target=requested_target,
        )
        _card_two_diag_stage(diagnostics, "get_latest_finalized_analysis_report.total", total_started, query_count=0)
        return None
    _card_two_diag_stage(
        diagnostics,
        "get_latest_finalized_analysis_report.filter_candidates",
        filter_started,
        candidate_count=len(candidates),
        requested_target=requested_target,
    )
    select_started = time.perf_counter()
    selected = max(
        candidates,
        key=lambda item: (
            _as_int(item.get("prediction_issue")) or 0,
            str(item.get("verified_at") or item.get("updated_at") or item.get("created_at") or ""),
            int(item.get("id") or 0),
        ),
    )
    _card_two_diag_stage(diagnostics, "get_latest_finalized_analysis_report.select_latest", select_started)
    _card_two_diag_stage(diagnostics, "get_latest_finalized_analysis_report.total", total_started, query_count=0)
    return selected


def _card_two_from_record(
    record: dict | None,
    current_draw: dict | None = None,
    requested_issue: Any = None,
    diagnostics: dict | None = None,
    use_dashboard_read_pool: bool = False,
    include_rules: bool = True,
) -> dict:
    total_started = time.perf_counter()
    if not record:
        result = _card_two_empty(requested_issue)
        _card_two_diag_stage(diagnostics, "_card_two_from_record.total", total_started, query_count=0, db_calls=0)
        if diagnostics is not None:
            result["diagnostics"] = {"card_two_execution": diagnostics}
            result["query_count"] = 0
        return result
    extract_started = time.perf_counter()
    issue = _valid_production_issue(record.get("prediction_issue") or record.get("target_issue"))
    requested = _valid_production_issue(requested_issue)
    if requested and issue != requested:
        result = _card_two_empty(requested)
        _card_two_diag_stage(diagnostics, "_card_two_from_record.total", total_started, query_count=0, db_calls=0)
        if diagnostics is not None:
            result["diagnostics"] = {"card_two_execution": diagnostics}
            result["query_count"] = 0
        return result
    prediction_numbers = _as_int_list(record.get("recommend_numbers"))
    official_numbers = _card_two_record_actual_numbers(record)
    official_super = _as_int(record.get("actual_super") or record.get("official_super_number"))
    _card_two_diag_stage(
        diagnostics,
        "_card_two_from_record.field_extraction",
        extract_started,
        prediction_count=len(prediction_numbers),
        official_count=len(official_numbers),
    )
    official_draw = None
    if issue and (len(official_numbers) != 20 or official_super is None):
        official_lookup_started = time.perf_counter()
        official_draw = _card_two_current_official_draw(issue, current_draw) if use_dashboard_read_pool else None
        official_lookup_source = "current_draw" if official_draw else "lookup"
        query_count = 0 if official_draw else 1
        db_calls = 0 if official_draw else 1
        if official_draw is None:
            try:
                official_draw = _timed_component_stage(
                    "card_two",
                    "official_draw_lookup",
                    lambda: get_official_draw_by_issue(issue),
                )
            except Exception:
                logger.exception("dashboard card two official draw lookup failed")
                official_draw = None
        if len(official_numbers) != 20:
            official_numbers = _as_int_list((official_draw or {}).get("numbers"))
        if official_super is None:
            official_super = _as_int((official_draw or {}).get("super_number"))
        _card_two_diag_stage(
            diagnostics,
            "_card_two_from_record.official_draw_lookup",
            official_lookup_started,
            query_count=query_count,
            db_calls=db_calls,
            found=bool(official_draw),
            source=official_lookup_source,
            current_issue=(current_draw or {}).get("issue"),
        )
    if not issue or len(prediction_numbers) != 20 or len(official_numbers) != 20:
        result = _card_two_empty(requested_issue or issue)
        _card_two_diag_stage(diagnostics, "_card_two_from_record.total", total_started, query_count=0, db_calls=0)
        if diagnostics is not None:
            result["diagnostics"] = {"card_two_execution": diagnostics}
            result["query_count"] = 0
        return result
    number_started = time.perf_counter()
    official_set = set(official_numbers)
    matched_numbers = [number for number in prediction_numbers if number in official_set]
    if official_super is None or not (1 <= official_super <= 80):
        super_hit: bool | None = None
        super_text = "資料不足"
    else:
        super_hit = official_super in set(prediction_numbers)
        super_text = "命中" if super_hit else "未命中"
    actual_big_small = _big_small(official_numbers)
    actual_odd_even = _odd_even(official_numbers)
    size_result = _card_two_distribution_result(record.get("big_small"), actual_big_small)
    odd_even_result = _card_two_distribution_result(record.get("odd_even"), actual_odd_even)
    consecutive_groups = _card_two_actual_consecutive_groups(official_numbers)
    _card_two_diag_stage(
        diagnostics,
        "_card_two_from_record.number_processing",
        number_started,
        matched_count=len(matched_numbers),
    )
    rules: list[dict] = []
    if include_rules:
        analysis_started = time.perf_counter()
        if use_dashboard_read_pool:
            analysis, analysis_timing = _timed_component_stage(
                "card_two",
                "analysis_lookup",
                lambda: _card_two_analysis_by_issue_with_timing(record.get("issue"), use_dashboard_read_pool=True),
            )
        else:
            analysis = _timed_component_stage(
                "card_two",
                "analysis_lookup",
                lambda: _card_two_analysis_by_issue(record.get("issue")),
            )
            analysis_timing = {"query_count": 1 if record.get("issue") else 0, "db_calls": 1 if record.get("issue") else 0}
        _card_two_diag_stage(
            diagnostics,
            "_card_two_from_record.analysis_lookup",
            analysis_started,
            **dict(analysis_timing or {}),
        )
        rules_started = time.perf_counter()
        rules = _card_two_rules(analysis, record, official_numbers, diagnostics=diagnostics)
        _card_two_diag_stage(diagnostics, "_card_two_from_record.rule_processing", rules_started, rule_count=len(rules))
    else:
        _card_two_diag_stage(
            diagnostics,
            "_card_two_from_record.rules_deferred",
            time.perf_counter(),
            query_count=0,
            db_calls=0,
            rule_count=0,
        )
    payload_started = time.perf_counter()
    result = {
        "title": CARD_TWO_TITLE,
        "available": True,
        "report_status": "finalized",
        "status_text": "最終分析",
        "issue": issue,
        "requested_issue": requested or issue,
        "actual_issue": issue,
        "source_issue": record.get("issue"),
        "prediction_numbers": prediction_numbers,
        "official_numbers": official_numbers,
        "matched_numbers": matched_numbers,
        "hit_count": len(matched_numbers),
        "prediction_count": len(prediction_numbers),
        "super_number": official_super,
        "super_number_hit": super_hit,
        "super_number_status_text": super_text,
        "size_result": size_result,
        "odd_even_result": odd_even_result,
        "actual_consecutive_groups": consecutive_groups,
        "finalized_at": record.get("learned_at") or record.get("verified_at") or record.get("updated_at"),
        "rules": rules,
        "fallback_text": None,
        "data_source": {
            "store": "prediction_history",
            "prediction_snapshot": "recommend_numbers",
            "validation_record": "prediction_history",
            "rule_library_snapshot": "rule_snapshot_store_or_analysis_history",
            "selector": "get_latest_finalized_analysis_report",
        },
    }
    _card_two_diag_stage(diagnostics, "_card_two_from_record.payload_build", payload_started)
    if diagnostics is not None:
        stages = diagnostics.get("stages", {})
        query_count = sum(
            int(stage.get("query_count") or 0)
            for name, stage in stages.items()
            if isinstance(stage, dict) and not str(name).endswith(".total")
        )
        db_calls = sum(
            int(stage.get("db_calls") or 0)
            for name, stage in stages.items()
            if isinstance(stage, dict) and not str(name).endswith(".total")
        )
        _card_two_diag_stage(
            diagnostics,
            "_card_two_from_record.total",
            total_started,
            query_count=query_count,
            db_calls=db_calls,
        )
        result["diagnostics"] = {"card_two_execution": diagnostics}
        result["query_count"] = query_count
    return result


def _data_counts(
    history_records: list[dict],
    stats: dict | None = None,
    aggregates: dict | None = None,
) -> dict:
    aggregates = aggregates or {}
    verified_records = [
        item for item in history_records or []
        if item.get("winning_numbers") or item.get("prediction_status") == "verified" or item.get("verified_at")
    ]
    record_count = len(history_records or [])
    return {
        "draw_count": record_count,
        "analysis_count": record_count,
        "prediction_count": aggregates.get("total_prediction_count", record_count),
        "valid_prediction_count": aggregates.get("valid_prediction_count", record_count),
        "valid_target_count": aggregates.get("valid_target_count"),
        "null_target_count": aggregates.get("null_target_count"),
        "has_official_result_count": aggregates.get("has_official_result_count"),
        "verified_prediction_count": aggregates.get("completed_verified_count", (stats or {}).get("verified_prediction_count", len(verified_records))),
        "statistics_sample_count": aggregates.get("valid_sample_count", (stats or {}).get("sample_size", len(verified_records))),
        "learning_sample_count": aggregates.get("learned_distinct_target_count", sum(1 for item in history_records if item.get("learning_used"))),
        "today_draw_count": 0,
        "statistics_scope": "all_history_aggregates",
    }


def _history_stats(history_records: list[dict]) -> dict:
    verified = [
        item for item in history_records or []
        if item.get("winning_numbers") or item.get("prediction_status") == "verified" or item.get("verified_at")
    ]
    total = len(verified)
    if not total:
        return {
            "status": "empty",
            "message": "尚未累積已驗證預測紀錄，系統會持續保存後續推薦。",
            "sample_size": 0,
            "three_star_rate": 0,
            "four_star_rate": 0,
            "five_star_rate": 0,
            "super_hit_rate": 0,
            "average_hits": 0,
            "pending_learning": 0,
            "verified_waiting_learning": 0,
        }
    pending_learning = sum(
        1 for item in verified if not item.get("learning_used")
    )
    return {
        "status": "ok",
        "sample_size": total,
        "three_star_rate": round(sum(1 for item in verified if item.get("three_star_hit")) / total * 100, 2),
        "four_star_rate": round(sum(1 for item in verified if item.get("four_star_hit")) / total * 100, 2),
        "five_star_rate": round(sum(1 for item in verified if (item.get("hit_count") or 0) >= 5) / total * 100, 2),
        "super_hit_rate": round(sum(1 for item in verified if item.get("super_hit") or item.get("super_number_hit")) / total * 100, 2),
        "average_hits": round(sum(item.get("hit_count") or 0 for item in verified) / total, 2),
        "pending_learning": pending_learning,
        "verified_waiting_learning": pending_learning,
    }

def _empty_rule_library() -> dict:
    return {
        "title": "AI rule library",
        "completed_count": 0,
        "total_count": len(RULE_LIBRARY_NAMES),
        "summary": "rule snapshot unavailable",
        "primary_rules": [],
        "rules": [],
        "stale": True,
    }


def _card_one_payload(
    *,
    current_draw: dict | None,
    latest_official_draw: dict | None,
    next_prediction: dict,
    rule_library: dict | None,
) -> dict:
    prediction_numbers = _as_int_list(
        next_prediction.get("recommend_numbers") or next_prediction.get("main_numbers")
    )
    official_numbers = _as_int_list((latest_official_draw or current_draw or {}).get("numbers"))
    high_probability = _as_int_list(next_prediction.get("high_probability_numbers"))[:5]
    official_super_number = _official_super_in_numbers(latest_official_draw or current_draw, official_numbers)
    return {
        "title": "🎯 最新開獎與 AI 推薦",
        "current_draw": current_draw,
        "latest_official_draw": latest_official_draw,
        "next_prediction": next_prediction,
        "rule_library": rule_library or _empty_rule_library(),
        "official_numbers": official_numbers,
        "official_super_number": official_super_number,
        "prediction_numbers": prediction_numbers,
        "high_probability_numbers": high_probability,
        "super_candidates": [],
        "official_numbers_complete": len(official_numbers) == 20,
        "prediction_numbers_complete": len(prediction_numbers) == 20,
        "high_probability_complete": len(high_probability) == 5,
        "super_candidates_complete": official_super_number is not None,
        "status": next_prediction.get("status") or "unknown",
        "source_issue": next_prediction.get("based_on_issue"),
        "target_issue": next_prediction.get("prediction_issue") or next_prediction.get("target_issue"),
        "generated_at": next_prediction.get("generated_at"),
        "confidence_percent": next_prediction.get("confidence_percent"),
        "diagnostics": (next_prediction.get("diagnostics") or {}).get("dashboard_card_v1", {}),
    }


def _card_three_payload(
    *,
    current_draw: dict | None,
    next_prediction: dict,
    prediction_stats: dict,
    sync: dict,
    active_release: dict | None,
    warnings: list[str],
    partial: bool,
) -> dict:
    active_release = active_release if isinstance(active_release, dict) else {}
    sections = {
        "latest_processing": {
            "current_issue": (current_draw or {}).get("issue"),
            "last_successful_collection": sync.get("last_successful_collection"),
        },
        "ai_flow": {
            "next_prediction_status": next_prediction.get("status") or "unknown",
        },
        "system_health": {
            "status": "partial" if partial else "ok",
            "is_synced": sync.get("is_synced"),
            "lag_count": sync.get("lag_count", 0),
            "warnings": warnings,
        },
        "learning_status": {
            "status": "ready" if (prediction_stats or {}).get("sample_size") else "waiting_data",
            "sample_size": (prediction_stats or {}).get("sample_size", 0),
            "average_hits": (prediction_stats or {}).get("average_hits", 0),
            "pending_learning": (prediction_stats or {}).get("pending_learning", 0),
        },
        "version_info": {
            "release_version": next_prediction.get("release_version") or active_release.get("release_version"),
        },
    }
    return {
        "status": "partial" if partial else "ok",
        "sections": sections,
        "system": {
            "warnings": warnings,
        },
    }


def _last_summary_cache() -> dict | None:
    if not _PLAYER_SUMMARY_CACHE_LOCK.acquire(blocking=False):
        return None
    try:
        payload = _PLAYER_SUMMARY_CACHE.get("payload")
        expires_at = float(_PLAYER_SUMMARY_CACHE.get("expires_at") or 0)
        if not isinstance(payload, dict):
            return None
        cached = deepcopy(payload)
        cached["cached"] = True
        cached["stale"] = True
        cached["partial"] = True
        cached["cache_age_seconds"] = round(max(0.0, time.monotonic() - (expires_at - PLAYER_SUMMARY_TTL_SECONDS)), 3)
        return cached
    finally:
        _PLAYER_SUMMARY_CACHE_LOCK.release()


def get_player_card_one_snapshot(
    *,
    deadline: float,
    timings: list[dict],
    warnings: list[str],
    component_metadata: dict[str, dict] | None = None,
    dashboard_generation_id: str | None = None,
) -> dict:
    started = time.perf_counter()
    official_future, official_state = _submit_component(
        "official_draw",
        get_latest_official_draw,
    )
    official = _component_result(
        "official_draw",
        official_future,
        deadline=deadline,
        timeout_seconds=PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS,
        timings=timings,
        warnings=warnings,
        component_metadata=component_metadata,
        dashboard_generation_id=dashboard_generation_id,
    )
    current = _current_draw(official)

    kuaishou_future, _ = _submit_component(
        "kuaishou",
        get_latest_kuaishou_snapshot,
    )
    kuaishou = _component_result(
        "kuaishou",
        kuaishou_future,
        deadline=deadline,
        timeout_seconds=PLAYER_DASHBOARD_OPTIONAL_TIMEOUT_SECONDS,
        timings=timings,
        warnings=warnings,
        fallback={},
        component_metadata=component_metadata,
        dashboard_generation_id=dashboard_generation_id,
    ) or {}
    detected_latest_issue = _max_issue((current or {}).get("issue"), (kuaishou or {}).get("issue"))

    next_prediction = None
    if current:
        def build_next_snapshot():
            diagnostic_started = time.perf_counter()
            diagnostics: dict[str, Any] = {
                "stages": [],
                "query_count": 0,
            }
            context_started = time.perf_counter()
            context = _timed_component_stage(
                "next_prediction_snapshot",
                "latest_prediction_context_lookup",
                lambda: get_latest_prediction_context(
                    allow_fallback_lookup=False,
                    include_timing=True,
                    use_dashboard_read_pool=True,
                ),
            )
            context_db_timing = (context or {}).get("db_timing") if isinstance(context, dict) else None
            if context_db_timing:
                diagnostics["query_count"] += int((context or {}).get("query_count") or 1)
            diagnostics["stages"].append(
                {
                    "stage": "latest_prediction_context_lookup",
                    "duration_ms": round((time.perf_counter() - context_started) * 1000, 2),
                    "db_timing": deepcopy(context_db_timing),
                    "query_count": (context or {}).get("query_count") if isinstance(context, dict) else None,
                }
            )
            context_draw = (context or {}).get("draw") or current
            if str((context_draw or {}).get("issue") or "") != str((current or {}).get("issue") or ""):
                record = None
                context_draw = current
            else:
                record = (context or {}).get("prediction")
            transform_started = time.perf_counter()
            return _timed_component_stage(
                "next_prediction_snapshot",
                "prediction_from_history",
                lambda: _attach_next_prediction_diagnostics(
                    _prediction_from_history(
                        record,
                        context_draw,
                        detected_latest_issue,
                        allow_slow_lookups=False,
                        transform_diagnostics=diagnostics,
                    ),
                    diagnostics,
                    transform_started,
                    diagnostic_started,
                ),
            )

        prediction_future, _ = _submit_component(
            "next_prediction_snapshot",
            build_next_snapshot,
        )
        next_prediction = _component_result(
            "next_prediction_snapshot",
            prediction_future,
            deadline=deadline,
            timeout_seconds=PLAYER_DASHBOARD_CARD_ONE_TIMEOUT_SECONDS,
            timings=timings,
            warnings=warnings,
            component_metadata=component_metadata,
            dashboard_generation_id=dashboard_generation_id,
        )
    else:
        next_prediction = _load_component_cache("next_prediction_snapshot")
        if next_prediction:
            _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
            warnings.append("next_prediction_snapshot stale cache")
            timings.append(_timed_default("next_prediction_snapshot", time.perf_counter(), "stale", "last_good_cache", reason="official_draw_unavailable"))

    next_prediction = (
        next_prediction
        or _load_component_cache("next_prediction_snapshot")
        or _pending_next_prediction(current, detected_latest_issue)
    )
    next_prediction["history"] = _load_component_cache("prediction_history_stats", {}) or {}
    next_prediction["rule_library"] = _load_component_cache("rule_library", _empty_rule_library()) or _empty_rule_library()
    next_prediction = _enrich_dashboard_card_v1(next_prediction, current)

    timings.append(
        _timed_default(
            "card_one_core",
            started,
            "ok" if current else "stale",
            "fast_snapshot",
            official_submit_state=official_state,
        )
    )
    return {
        "official": official,
        "current": current,
        "next_prediction": next_prediction,
        "detected_latest_issue": detected_latest_issue,
    }


def _build_previous_verification_snapshot(previous_target_issue: Any) -> dict:
    total_started = time.perf_counter()
    diagnostics: dict[str, Any] = {
        "query_count": 0,
        "sql_execute_count": 0,
        "db_checkout_count": 0,
        "hidden_db_round_trips": 0,
        "transform_stages": {},
    }
    combined_started = time.perf_counter()
    combined = _timed_component_stage(
        "previous_verification",
        "previous_verification_combined_lookup",
        lambda: get_previous_verification_summary_snapshot(
            str(previous_target_issue),
            include_metadata_lookup=False,
        ),
    )
    combined_elapsed_ms = round((time.perf_counter() - combined_started) * 1000, 2)
    db_timing = combined.get("db_timing") or {}
    row_transform_diagnostics = combined.get("row_transform_diagnostics") or {}
    db_total_ms = db_timing.get("total_ms") if isinstance(db_timing.get("total_ms"), (int, float)) else 0.0
    store_transform_ms = (
        row_transform_diagnostics.get("total_ms")
        if isinstance(row_transform_diagnostics.get("total_ms"), (int, float))
        else round(max(combined_elapsed_ms - float(db_total_ms or 0.0), 0.0), 2)
    )
    diagnostics["transform_stages"]["combined_lookup"] = {
        "elapsed_ms": combined_elapsed_ms,
        "io_type": "db",
        "query_count": 1,
        "db_checkout_count": 1,
    }
    diagnostics["transform_stages"]["db_query"] = {
        "elapsed_ms": round(float(db_total_ms or 0.0), 2),
        "io_type": "db",
        "query_count": 1,
        "db_checkout_count": 1,
        "connect_ms": db_timing.get("connect_ms"),
        "pool_acquire_ms": db_timing.get("pool_acquire_ms"),
        "execute_ms": db_timing.get("execute_ms"),
        "fetch_ms": db_timing.get("fetch_ms"),
        "connection_release_ms": db_timing.get("connection_release_ms"),
        "connection_hash": db_timing.get("connection_hash"),
    }
    diagnostics["transform_stages"]["store_row_transform"] = {
        "elapsed_ms": store_transform_ms,
        "io_type": "python",
        "query_count": 0,
        "returned_row_count": row_transform_diagnostics.get("returned_row_count"),
        "transformed_row_count": row_transform_diagnostics.get("transformed_row_count"),
        "json_decode_count": row_transform_diagnostics.get("json_decode_count"),
        "json_load_call_count": row_transform_diagnostics.get("json_load_call_count"),
        "number_processing_call_count": row_transform_diagnostics.get("number_processing_call_count"),
        "metadata_lookup_count": row_transform_diagnostics.get("metadata_lookup_count"),
        "metadata_lookup_skipped": row_transform_diagnostics.get("metadata_lookup_skipped"),
        "helper_counts": row_transform_diagnostics.get("helper_counts"),
    }
    for stage_name, stage in (row_transform_diagnostics.get("stages") or {}).items():
        diagnostics["transform_stages"][f"store_row_transform.{stage_name}"] = stage
    diagnostics["row_transform_diagnostics"] = row_transform_diagnostics
    diagnostics["query_count"] = 1
    diagnostics["sql_execute_count"] = 1
    diagnostics["db_checkout_count"] = 1
    diagnostics["connection_hash"] = db_timing.get("connection_hash")
    verified_record = combined.get("record")
    previous_result_mode = combined.get("mode") or "unavailable"
    displayed_target_issue = (verified_record or {}).get("prediction_issue")
    verification_draw = combined.get("draw")
    verification_started = time.perf_counter()
    verification_diagnostics: dict[str, Any] = {"transform_stages": {}}
    previous_verification = (
        _verification(
            verified_record,
            verification_draw,
            verification_diagnostics,
            allow_slow_lookups=False,
        )
        if verified_record
        else _unavailable_previous_result(previous_target_issue)
    )
    verification_elapsed_ms = round((time.perf_counter() - verification_started) * 1000, 2)
    diagnostics["transform_stages"]["verification_payload"] = {
        "elapsed_ms": verification_elapsed_ms,
        "io_type": "python",
        "query_count": sum(
            int(stage.get("query_count") or 0)
            for stage in (verification_diagnostics.get("transform_stages") or {}).values()
        ),
    }
    for stage_name, stage in (verification_diagnostics.get("transform_stages") or {}).items():
        diagnostics["transform_stages"][stage_name] = stage
    hidden_db_round_trips = sum(
        int(stage.get("hidden_db_lookup_count") or 0)
        for stage in diagnostics["transform_stages"].values()
    )
    diagnostics["hidden_db_round_trips"] = hidden_db_round_trips
    diagnostics["query_count"] = 1 + hidden_db_round_trips
    diagnostics["sql_execute_count"] = 1 + hidden_db_round_trips
    diagnostics["db_checkout_count"] = 1 + hidden_db_round_trips
    total_ms = round((time.perf_counter() - total_started) * 1000, 2)
    accounted_ms = round(
        (db_timing.get("connect_ms") or db_timing.get("pool_acquire_ms") or 0)
        + (db_timing.get("execute_ms") or 0)
        + (db_timing.get("fetch_ms") or 0)
        + diagnostics["transform_stages"]["store_row_transform"]["elapsed_ms"]
        + sum(
            stage.get("elapsed_ms") or 0
            for name, stage in diagnostics["transform_stages"].items()
            if name
            not in {
                "combined_lookup",
                "db_query",
                "store_row_transform",
                "verification_payload",
            }
        ),
        2,
    )
    diagnostics["total_execution_observed_ms"] = total_ms
    diagnostics["post_db_ms"] = round(
        diagnostics["transform_stages"]["store_row_transform"]["elapsed_ms"] + verification_elapsed_ms,
        2,
    )
    diagnostics["accounted_ms"] = accounted_ms
    diagnostics["unaccounted_ms"] = round(max(total_ms - accounted_ms, 0.0), 2)
    previous_verification["previous_result_mode"] = previous_result_mode
    previous_verification["requested_target_issue"] = previous_target_issue
    previous_verification["displayed_target_issue"] = displayed_target_issue
    previous_verification["db_timing"] = db_timing
    previous_verification["diagnostics"] = {
        "previous_verification_execution": diagnostics,
    }
    previous_verification["query_count"] = diagnostics["query_count"]
    return previous_verification


def _dashboard_health(
    component_metadata: dict[str, dict],
    *,
    official_issue: Any,
    next_prediction: dict | None,
    aggregates: dict | None,
    card_two_history: list[dict] | None,
    generation_id: str,
) -> dict:
    next_prediction = next_prediction or {}
    aggregates = aggregates or {}
    card_two_history = card_two_history or []
    official_text = str(official_issue) if official_issue is not None else None
    prediction_source_issue = next_prediction.get("source_issue") or next_prediction.get("based_on_issue")
    prediction_target_issue = next_prediction.get("target_issue") or next_prediction.get("prediction_issue")
    aggregate_issue = aggregates.get("latest_issue")
    card_two_issue = (card_two_history[0].get("prediction_issue") if card_two_history else None)
    checks = [
        official_text,
        str(prediction_source_issue) if prediction_source_issue is not None else None,
        str(aggregate_issue) if aggregate_issue is not None else None,
        str(card_two_issue) if card_two_issue is not None else None,
    ]
    numeric_checks = [_as_int(item) for item in checks if item]
    issue_consistent = True
    if official_text and prediction_source_issue and str(official_text) != str(prediction_source_issue):
        issue_consistent = False

    # Historical verification/aggregate/Card Two may legitimately trail after a
    # latest-only gap jump. They describe the newest completed prediction
    # lifecycle, not the freshness of the official draw. Only compare lifecycle
    # components with each other; current freshness is defined by official vs
    # prediction source.
    # Verification and aggregates describe the newest completed lifecycle.
    # Card Two history can already contain the current prediction target, so it
    # is not a reliable member of that historical consistency group after a
    # latest-only gap jump. Validate it against either the current target or,
    # when it is historical, the completed lifecycle independently.
    completed_lifecycle_checks = [
        _as_int(item)
        for item in (aggregate_issue,)
        if item
    ]
    if len(completed_lifecycle_checks) > 1:
        issue_consistent = issue_consistent and (
            max(completed_lifecycle_checks) - min(completed_lifecycle_checks) <= 1
        )
    card_two_int = _as_int(card_two_issue)
    prediction_target_int = _as_int(prediction_target_issue)
    if card_two_int is not None and prediction_target_int is not None and card_two_int != prediction_target_int:
        if completed_lifecycle_checks:
            issue_consistent = issue_consistent and (
                min(abs(card_two_int - item) for item in completed_lifecycle_checks) <= 1
            )
    live_component_names = [name for name, item in component_metadata.items() if item.get("source") == "live"]
    cached_component_names = [name for name, item in component_metadata.items() if item.get("source") == "cache"]
    fallback_component_names = [name for name, item in component_metadata.items() if item.get("source") == "fallback"]
    failed_component_names = [name for name, item in component_metadata.items() if item.get("result") == "error"]
    stale_component_names = [name for name, item in component_metadata.items() if item.get("stale")]
    live_components = len(live_component_names)
    cached_components = len(cached_component_names)
    fallback_components = len(fallback_component_names)
    failed_components = len(failed_component_names)
    stale_components = len(stale_component_names)
    status = "healthy"
    if failed_components:
        status = "broken"
    elif not issue_consistent:
        status = "degraded"
    elif cached_components or fallback_components:
        status = "stale"
    elif stale_components:
        status = "degraded"
    logger.warning(
        "dashboard_consistency generation_id=%s official_issue=%s prediction_source_issue=%s prediction_target_issue=%s aggregate_issue=%s card_two_issue=%s prediction_source=%s aggregate_source=%s consistent=%s status=%s cached=%s fallback=%s stale=%s",
        generation_id,
        official_text,
        prediction_source_issue,
        prediction_target_issue,
        aggregate_issue,
        card_two_issue,
        (component_metadata.get("next_prediction_snapshot") or {}).get("source"),
        (component_metadata.get("prediction_aggregates") or {}).get("source"),
        issue_consistent,
        status,
        cached_component_names,
        fallback_component_names,
        stale_component_names,
    )
    return {
        "status": status,
        "live_components": live_components,
        "cached_components": cached_components,
        "fallback_components": fallback_components,
        "failed_components": failed_components,
        "live_component_names": live_component_names,
        "cached_component_names": cached_component_names,
        "fallback_component_names": fallback_component_names,
        "failed_component_names": failed_component_names,
        "stale_component_names": stale_component_names,
        "issue_consistent": issue_consistent,
        "dashboard_generation_id": generation_id,
        "official_issue": official_text,
        "prediction_source_issue": str(prediction_source_issue) if prediction_source_issue is not None else None,
        "prediction_target_issue": str(prediction_target_issue) if prediction_target_issue is not None else None,
        "aggregate_issue": str(aggregate_issue) if aggregate_issue is not None else None,
        "card_two_issue": str(card_two_issue) if card_two_issue is not None else None,
    }


def build_player_dashboard_summary() -> dict:
    cached = _cached_summary()
    if cached:
        return cached

    if not _PLAYER_SUMMARY_BUILD_LOCK.acquire(blocking=False):
        stale = _last_summary_cache()
        if stale:
            _PLAYER_RUNTIME_METRICS["stale_fallback_count"] += 1
            stale.setdefault("warnings", [])
            stale["warnings"] = list(stale["warnings"]) + ["summary build in flight"]
            return stale
        _PLAYER_SUMMARY_BUILD_LOCK.acquire()
    try:
        return _build_player_dashboard_summary_uncached()
    finally:
        _PLAYER_SUMMARY_BUILD_LOCK.release()


def _build_player_dashboard_summary_uncached() -> dict:
    cached = _cached_summary()
    if cached:
        return cached

    total_start = time.perf_counter()
    deadline = time.monotonic() + PLAYER_DASHBOARD_TOTAL_BUDGET_SECONDS
    warnings: list[str] = []
    timings: list[dict] = []
    generated_at = _now()
    dashboard_generation_id = _dashboard_generation_id()
    component_metadata: dict[str, dict] = {}
    generation_token = _PLAYER_DASHBOARD_GENERATION_CONTEXT.set(dashboard_generation_id)
    wait_order_token = _PLAYER_DASHBOARD_WAIT_ORDER_CONTEXT.set(1)
    try:
        return _build_player_dashboard_summary_payload(
            total_start=total_start,
            deadline=deadline,
            warnings=warnings,
            timings=timings,
            generated_at=generated_at,
            dashboard_generation_id=dashboard_generation_id,
            component_metadata=component_metadata,
        )
    finally:
        _PLAYER_DASHBOARD_WAIT_ORDER_CONTEXT.reset(wait_order_token)
        _PLAYER_DASHBOARD_GENERATION_CONTEXT.reset(generation_token)


def _build_player_dashboard_summary_payload(
    *,
    total_start: float,
    deadline: float,
    warnings: list[str],
    timings: list[dict],
    generated_at: str,
    dashboard_generation_id: str,
    component_metadata: dict[str, dict],
) -> dict:

    # Start Card One first so the authoritative official draw gets first access
    # to the small dashboard read pool. Aggregate/history work is intentionally
    # deferred until after Card One to avoid starving official_draw.
    card_one = get_player_card_one_snapshot(
        deadline=deadline,
        timings=timings,
        warnings=warnings,
        component_metadata=component_metadata,
        dashboard_generation_id=dashboard_generation_id,
    )
    current = card_one["current"]
    official = card_one["official"]
    next_prediction = card_one["next_prediction"]
    detected_latest_issue = card_one["detected_latest_issue"]

    cached_aggregates = _load_fresh_component_cache(
        "prediction_aggregates",
        PLAYER_AGGREGATE_CACHE_TTL_SECONDS,
    )
    if cached_aggregates:
        aggregates_future = None
    else:
        aggregates_future, _ = _submit_component(
            "prediction_aggregates",
            lambda: _timed_component_stage(
                "prediction_aggregates",
                "prediction_lifecycle_aggregates",
                lambda: get_prediction_lifecycle_aggregates(
                    diagnostic_component="prediction_aggregates",
                    use_dashboard_read_pool=True,
                ),
            ),
        )

    card_two_history_future, _ = _submit_component(
        "card_two_history",
        lambda: _timed_component_stage(
            "card_two_history",
            "prediction_history_summary_records",
            lambda: get_prediction_history_records(PLAYER_DASHBOARD_HISTORY_LIMIT, diagnostic_component="card_two_history", include_event_metadata=False),
        ),
    )

    if detected_latest_issue and (current or {}).get("issue") and str(detected_latest_issue) != str((current or {}).get("issue")):
        next_prediction["sync_status"] = "database_behind"
        next_prediction["recommendation_warning"] = (
            f"Production sync stale: database latest issue {(current or {}).get('issue')} "
            f"is behind detected issue {detected_latest_issue}."
        )
    previous_target_issue = next_prediction.get("based_on_issue")
    if not current:
        stale_steps = [
            _public_step_name(item["step"])
            for item in timings
            if item.get("result") in {"stale", "skipped", "error"}
        ]
        timeout_steps = [
            _public_step_name(item["step"])
            for item in timings
            if item.get("result") == "timeout"
        ]
        skipped_busy_steps = [
            _public_step_name(item["step"])
            for item in timings
            if item.get("reason") == "worker_busy"
        ]
        partial = bool(stale_steps or timeout_steps or skipped_busy_steps)
        prediction_stats = {
            "status": "empty",
            "message": "尚未取得最新正式開獎資料。",
            "sample_size": 0,
            "three_star_rate": 0,
            "four_star_rate": 0,
            "five_star_rate": 0,
            "super_hit_rate": 0,
            "average_hits": 0,
            "pending_learning": 0,
            "verified_waiting_learning": 0,
            "history_limit": PLAYER_DASHBOARD_HISTORY_LIMIT,
            "stale": True,
        }
        aggregates = {"stale": True}
        production_scope = {}
        active_release = {}
        sync = {
            "database_latest_issue": None,
            "official_latest_issue": detected_latest_issue,
            "detected_latest_issue": detected_latest_issue,
            "latest_kuaishou_issue": None,
            "lag_count": 0,
            "is_synced": False,
            "last_successful_collection": None,
            "collection_duration_seconds": None,
        }
        data_counts = _data_counts([], prediction_stats, aggregates)
        rule_library = next_prediction.get("rule_library") or _empty_rule_library()
        previous_verification = _unavailable_previous_result(next_prediction.get("based_on_issue"))
        latest_official_draw = _latest_official_draw_card(official)
        card_one_payload = _card_one_payload(
            current_draw=current,
            latest_official_draw=latest_official_draw,
            next_prediction=next_prediction,
            rule_library=rule_library,
        )
        card_two = _card_two_empty(next_prediction.get("based_on_issue"))
        card_three_payload = _card_three_payload(
            current_draw=current,
            next_prediction=next_prediction,
            prediction_stats=prediction_stats,
            sync=sync,
            active_release=active_release,
            warnings=warnings,
            partial=partial,
        )
        meta = {
            "generated_at": generated_at,
            "dashboard_generation_id": dashboard_generation_id,
            "partial": partial,
            "warnings": warnings,
            "timeout_steps": timeout_steps,
            "stale_steps": stale_steps,
            "skipped_busy_steps": skipped_busy_steps,
            "schema_version": "player_summary_cards_v1",
            "components": component_metadata,
        }
        health = _dashboard_health(
            component_metadata,
            official_issue=detected_latest_issue,
            next_prediction=next_prediction,
            aggregates=aggregates,
            card_two_history=[],
            generation_id=dashboard_generation_id,
        )
        return {
            "status": "ok",
            "generated_at": generated_at,
            "meta": meta,
            "health": health,
            "cache_filter_version": PLAYER_CACHE_FILTER_VERSION,
            "production_filtered": True,
            "production_scope": production_scope,
            "active_release": active_release,
            "current_draw": current,
            "latest_official_draw": latest_official_draw,
            "sync": sync,
            "card_one": card_one_payload,
            "next_prediction": next_prediction,
            "card_two": card_two,
            "card_three": card_three_payload,
            "previous_verification": previous_verification,
            "prediction_history": [],
            "data_counts": data_counts,
            "history": prediction_stats,
            "aggregates": aggregates,
            "rule_library": rule_library,
            "warnings": warnings,
            "partial": partial,
            "stale": partial,
            "stale_steps": stale_steps,
            "timeout_steps": timeout_steps,
            "skipped_busy_steps": skipped_busy_steps,
            "timing": {
                "total_duration_ms": round((time.perf_counter() - total_start) * 1000, 2),
                "steps": timings,
                "cache_hits": _PLAYER_RUNTIME_METRICS["cache_hit_count"],
                "in_flight_count": _player_in_flight_count(),
                "runtime_metrics": player_dashboard_runtime_metrics(),
            },
        }

    analysis_future, _ = _submit_component("analysis", get_latest_analysis_history)

    if cached_aggregates:
        aggregate_cache_updated_at = _PLAYER_COMPONENT_CACHE_UPDATED_AT.get("prediction_aggregates")
        logger.warning(
            "dashboard_component_cache_hit component=prediction_aggregates age_ms=%s ttl_seconds=%s",
            round(max(0.0, time.monotonic() - aggregate_cache_updated_at) * 1000, 2)
            if aggregate_cache_updated_at is not None
            else None,
            PLAYER_AGGREGATE_CACHE_TTL_SECONDS,
        )
        aggregates = dict(cached_aggregates)
        component_metadata["prediction_aggregates"] = _component_metadata(
            "prediction_aggregates",
            aggregates,
            source="live",
            timed_out=False,
            result="fresh_cache",
            dashboard_generation_id=dashboard_generation_id,
        )
        aggregates["_component_metadata"] = component_metadata["prediction_aggregates"]
        aggregates["source"] = "live"
        aggregates["stale"] = False
        timings.append(_timed_default("prediction_aggregates", time.perf_counter(), "ok", "fresh_cache"))
    else:
        aggregates = _component_result(
            "prediction_aggregates",
            aggregates_future,
            deadline=deadline,
            timeout_seconds=PLAYER_DASHBOARD_AGGREGATE_TIMEOUT_SECONDS,
            timings=timings,
            warnings=warnings,
            fallback={},
            component_metadata=component_metadata,
            dashboard_generation_id=dashboard_generation_id,
        ) or {}
    previous_verification = _unavailable_previous_result(previous_target_issue)
    previous_verification["previous_result_mode"] = "stale_unavailable"
    previous_verification.setdefault("requested_target_issue", previous_target_issue)
    previous_verification.setdefault("displayed_target_issue", None)

    card_two_history = _component_result(
        "card_two_history",
        card_two_history_future,
        deadline=deadline,
        timeout_seconds=PLAYER_DASHBOARD_OPTIONAL_TIMEOUT_SECONDS,
        timings=timings,
        warnings=warnings,
        fallback=_load_component_cache("card_two_history", []),
        component_metadata=component_metadata,
        dashboard_generation_id=dashboard_generation_id,
    ) or []
    history_records = card_two_history[:PLAYER_DASHBOARD_HISTORY_LIMIT]
    _store_component_cache("prediction_history", history_records)
    analysis = _component_result(
        "analysis",
        analysis_future,
        deadline=deadline,
        timeout_seconds=PLAYER_DASHBOARD_OPTIONAL_TIMEOUT_SECONDS,
        timings=timings,
        warnings=warnings,
        fallback={},
        component_metadata=component_metadata,
        dashboard_generation_id=dashboard_generation_id,
    ) or {}
    active_release = {
        key: next_prediction.get(key)
        for key in (
            "release_version",
            "git_commit_hash",
            "git_commit_short",
            "production_generation",
            "production_start_issue",
            "production_start_at",
            "model_version",
            "feature_version",
            "phase",
        )
        if next_prediction.get(key) is not None
    }
    kuaishou = _load_component_cache("kuaishou", {}) or {}
    production_scope = _run_inline_step(
        "production_scope",
        production_scope_payload,
        deadline=deadline,
        timings=timings,
        warnings=warnings,
        fallback={},
        cache_name="production_scope",
    ) or {}

    prediction_stats = _history_stats(history_records)
    prediction_stats["history_limit"] = PLAYER_DASHBOARD_HISTORY_LIMIT
    prediction_stats["stale"] = any(
        item.get("step") == "card_two_history" and item.get("result") != "ok"
        for item in timings
    )
    production_history = [_history_item(item) for item in history_records if is_production_prediction(item)]

    rule_library = _rule_library(analysis, next_prediction)
    _store_component_cache("rule_library", rule_library)
    next_prediction["rule_library"] = rule_library
    next_prediction = _enrich_dashboard_card_v1(next_prediction, current)

    def build_card_two_snapshot():
        diagnostics: dict[str, Any] = {"dependency_inputs": {
            "card_two_history_count": len(card_two_history or []),
            "current_issue": (current or {}).get("issue"),
            "previous_target_issue": previous_target_issue,
        }}
        return _card_two_from_record(
            _timed_component_stage(
                "card_two",
                "finalized_analysis_report",
                lambda: get_latest_finalized_analysis_report(
                    card_two_history,
                    current,
                    previous_target_issue,
                    diagnostics=diagnostics,
                ),
            ),
            current,
            previous_target_issue,
            diagnostics=diagnostics,
            use_dashboard_read_pool=True,
            include_rules=False,
        )

    card_two = _run_inline_step(
        "card_two",
        build_card_two_snapshot,
        deadline=deadline,
        timings=timings,
        warnings=warnings,
        fallback=_card_two_empty(previous_target_issue),
        cache_name="card_two",
    ) or _card_two_empty(previous_target_issue)

    database_issue = (current or {}).get("issue")
    official_issue = detected_latest_issue or (current or {}).get("issue")
    database_int = _as_int(database_issue)
    official_int = _as_int(official_issue)
    lag_count = max((official_int or 0) - (database_int or 0), 0) if database_int and official_int else 0

    stale_steps = [
        _public_step_name(item["step"])
        for item in timings
        if item.get("result") in {"stale", "skipped", "error"}
    ]
    timeout_steps = [_public_step_name(item["step"]) for item in timings if item.get("result") == "timeout"]
    skipped_busy_steps = [_public_step_name(item["step"]) for item in timings if item.get("reason") == "worker_busy"]
    partial = bool(stale_steps or timeout_steps or skipped_busy_steps)

    sync = {
        "database_latest_issue": database_issue,
        "official_latest_issue": detected_latest_issue or official_issue,
        "detected_latest_issue": detected_latest_issue,
        "latest_kuaishou_issue": (kuaishou or {}).get("issue"),
        "lag_count": lag_count,
        "is_synced": lag_count == 0 and str(detected_latest_issue or official_issue or "") == str(database_issue or ""),
        "last_successful_collection": (current or {}).get("collected_at"),
        "collection_duration_seconds": None,
    }
    data_counts = _data_counts(history_records, prediction_stats, aggregates)
    latest_official_draw = _latest_official_draw_card(official)
    previous_verification.setdefault("requested_target_issue", previous_target_issue)
    previous_verification.setdefault("displayed_target_issue", None)
    card_one_payload = _card_one_payload(
        current_draw=current,
        latest_official_draw=latest_official_draw,
        next_prediction=next_prediction,
        rule_library=rule_library,
    )
    card_three_payload = _card_three_payload(
        current_draw=current,
        next_prediction=next_prediction,
        prediction_stats=prediction_stats,
        sync=sync,
        active_release=active_release,
        warnings=warnings,
        partial=partial,
    )
    meta = {
        "generated_at": generated_at,
        "dashboard_generation_id": dashboard_generation_id,
        "partial": partial,
        "warnings": warnings,
        "timeout_steps": timeout_steps,
        "stale_steps": stale_steps,
        "skipped_busy_steps": skipped_busy_steps,
        "schema_version": "player_summary_cards_v1",
        "components": component_metadata,
    }
    health = _dashboard_health(
        component_metadata,
        official_issue=detected_latest_issue or official_issue,
        next_prediction=next_prediction,
        aggregates=aggregates,
        card_two_history=card_two_history,
        generation_id=dashboard_generation_id,
    )

    payload = {
        "status": "ok",
        "generated_at": generated_at,
        "meta": meta,
        "health": health,
        "cache_filter_version": PLAYER_CACHE_FILTER_VERSION,
        "production_filtered": True,
        "production_scope": production_scope,
        "active_release": active_release,
        "sync": sync,
        "card_one": card_one_payload,
        "next_prediction": next_prediction,
        "card_two": card_two,
        "card_three": card_three_payload,
        "previous_verification": previous_verification,
        "prediction_history": production_history,
        "data_counts": data_counts,
        "history": prediction_stats,
        "aggregates": aggregates if isinstance(aggregates, dict) else {},
        "rule_library": rule_library,
        "warnings": warnings,
        "partial": partial,
        "stale": partial,
        "stale_steps": stale_steps,
        "timeout_steps": timeout_steps,
        "skipped_busy_steps": skipped_busy_steps,
        "timing": {
            "total_duration_ms": round((time.perf_counter() - total_start) * 1000, 2),
            "steps": timings,
            "cache_hits": _PLAYER_RUNTIME_METRICS["cache_hit_count"],
            "in_flight_count": _player_in_flight_count(),
            "runtime_metrics": player_dashboard_runtime_metrics(),
        },
    }
    if current or ("official_draw" not in timeout_steps and "official_draw" not in skipped_busy_steps):
        _store_summary_cache(payload)
    return payload


