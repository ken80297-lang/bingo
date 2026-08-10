from __future__ import annotations

import os
import logging
import time
from collections import OrderedDict, deque
from copy import deepcopy
from threading import Lock
from typing import Any

UNKNOWN_RESPONSE_BYTES = "unknown"
_INTERNAL_EXCLUDED_ENDPOINTS = {"/api/operations/metrics"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


MAX_ENDPOINT_KEYS = _env_int("TRAFFIC_METRICS_MAX_ENDPOINTS", 100)
WINDOW_SECONDS = _env_int("TRAFFIC_METRICS_WINDOW_SECONDS", 300)
MAX_WINDOW_EVENTS = _env_int("TRAFFIC_METRICS_MAX_WINDOW_EVENTS", 5000)
WARNING_AVG_RESPONSE_BYTES = _env_int("TRAFFIC_WARNING_AVG_RESPONSE_BYTES", 500 * 1024)
WARNING_TOTAL_RESPONSE_BYTES = _env_int("TRAFFIC_WARNING_TOTAL_RESPONSE_BYTES", 20 * 1024 * 1024)
WARNING_REQUESTS = _env_int("TRAFFIC_WARNING_REQUESTS", 100)
CRITICAL_TOTAL_RESPONSE_BYTES = _env_int("TRAFFIC_CRITICAL_TOTAL_RESPONSE_BYTES", 100 * 1024 * 1024)

_lock = Lock()
_since_start: OrderedDict[tuple[str, str | None], dict[str, Any]] = OrderedDict()
_window_events: deque[dict[str, Any]] = deque(maxlen=MAX_WINDOW_EVENTS)
_transition_status: dict[tuple[str, str | None], str] = {}
logger = logging.getLogger(__name__)


def reset_traffic_metrics() -> None:
    with _lock:
        _since_start.clear()
        _window_events.clear()
        _transition_status.clear()


def normalize_traffic_endpoint(path: str, route_path: str | None = None) -> str | None:
    endpoint = route_path or path
    if not endpoint.startswith("/api/"):
        return None
    return endpoint


def normalize_view_tag(view: str | None) -> str | None:
    value = str(view or "").strip().lower()
    if value in {"summary", "full"}:
        return value
    return None


def should_record_traffic(method: str, endpoint: str | None) -> bool:
    if method.upper() != "GET":
        return False
    if not endpoint:
        return False
    if endpoint in _INTERNAL_EXCLUDED_ENDPOINTS:
        return False
    return endpoint.startswith("/api/")


def _empty_metric() -> dict[str, Any]:
    return {
        "request_count": 0,
        "response_bytes_total": 0,
        "known_response_count": 0,
        "duration_total": 0.0,
        "max_response_bytes": 0,
        "max_duration_ms": 0.0,
        "status_2xx": 0,
        "status_4xx": 0,
        "status_5xx": 0,
        "unknown_response_bytes": 0,
    }


def _record_into(metric: dict[str, Any], status_code: int, duration_ms: float, response_bytes: int | None) -> None:
    metric["request_count"] += 1
    metric["duration_total"] += float(duration_ms)
    metric["max_duration_ms"] = max(metric["max_duration_ms"], float(duration_ms))
    if response_bytes is None:
        metric["unknown_response_bytes"] += 1
    else:
        metric["known_response_count"] += 1
        metric["response_bytes_total"] += int(response_bytes)
        metric["max_response_bytes"] = max(metric["max_response_bytes"], int(response_bytes))
    if 200 <= status_code <= 299:
        metric["status_2xx"] += 1
    elif 400 <= status_code <= 499:
        metric["status_4xx"] += 1
    elif status_code >= 500:
        metric["status_5xx"] += 1


def _evict_endpoint_if_needed(key: tuple[str, str | None]) -> tuple[str, str | None] | None:
    if key in _since_start:
        _since_start.move_to_end(key)
        return None
    while len(_since_start) >= MAX_ENDPOINT_KEYS:
        stale_key, _ = _since_start.popitem(last=False)
        _transition_status.pop(stale_key, None)
        return stale_key
    return None


def _compact_window(now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    while _window_events and float(_window_events[0]["timestamp"]) < cutoff:
        _window_events.popleft()


def record_traffic_request(
    *,
    method: str,
    endpoint: str | None,
    status_code: int,
    duration_ms: float,
    response_bytes: int | None,
    view: str | None = None,
    now: float | None = None,
) -> None:
    if not should_record_traffic(method, endpoint):
        return
    timestamp = time.time() if now is None else float(now)
    view_tag = normalize_view_tag(view)
    key = (str(endpoint), view_tag)
    event = {
        "timestamp": timestamp,
        "endpoint": key[0],
        "view": key[1],
        "status_code": int(status_code),
        "duration_ms": float(duration_ms),
        "response_bytes": response_bytes if response_bytes is None else int(response_bytes),
    }
    with _lock:
        _compact_window(timestamp)
        evicted_key = _evict_endpoint_if_needed(key)
        if evicted_key is not None:
            kept_events = [item for item in _window_events if (item["endpoint"], item.get("view")) != evicted_key]
            _window_events.clear()
            _window_events.extend(kept_events)
        metric = _since_start.setdefault(key, _empty_metric())
        _record_into(metric, int(status_code), float(duration_ms), response_bytes)
        alert_status = _metric_status(metric)
        if alert_status in {"warning", "critical"} and _transition_status.get(key) != alert_status:
            logger.warning(
                "traffic_alert status=%s endpoint=%s view=%s requests=%s response_bytes_total=%s",
                alert_status,
                key[0],
                key[1] or "-",
                metric["request_count"],
                metric["response_bytes_total"],
            )
        _transition_status[key] = alert_status
        _window_events.append(event)


def _metric_status(metric: dict[str, Any]) -> str:
    if int(metric.get("response_bytes_total") or 0) > CRITICAL_TOTAL_RESPONSE_BYTES:
        return "critical"
    request_count = int(metric.get("request_count") or 0)
    known_count = int(metric.get("known_response_count") or 0)
    avg_response = int(metric.get("response_bytes_total") or 0) / max(1, known_count)
    if (
        avg_response > WARNING_AVG_RESPONSE_BYTES
        or int(metric.get("response_bytes_total") or 0) > WARNING_TOTAL_RESPONSE_BYTES
        or request_count > WARNING_REQUESTS
    ):
        return "warning"
    return "normal"


def _summarize_metric(key: tuple[str, str | None], metric: dict[str, Any]) -> dict[str, Any]:
    request_count = int(metric.get("request_count") or 0)
    known_count = int(metric.get("known_response_count") or 0)
    errors = int(metric.get("status_4xx") or 0) + int(metric.get("status_5xx") or 0)
    endpoint, view = key
    item = {
        "endpoint": endpoint,
        "requests": request_count,
        "response_bytes_total": int(metric.get("response_bytes_total") or 0),
        "avg_response_bytes": round(int(metric.get("response_bytes_total") or 0) / max(1, known_count), 2),
        "max_response_bytes": int(metric.get("max_response_bytes") or 0),
        "avg_duration_ms": round(float(metric.get("duration_total") or 0) / max(1, request_count), 2),
        "max_duration_ms": round(float(metric.get("max_duration_ms") or 0), 2),
        "errors": errors,
        "status_2xx": int(metric.get("status_2xx") or 0),
        "status_4xx": int(metric.get("status_4xx") or 0),
        "status_5xx": int(metric.get("status_5xx") or 0),
        "unknown_response_bytes": int(metric.get("unknown_response_bytes") or 0),
        "alert_status": _metric_status(metric),
    }
    if view:
        item["view"] = view
    return item


def _build_window_metrics(events: list[dict[str, Any]]) -> OrderedDict[tuple[str, str | None], dict[str, Any]]:
    metrics: OrderedDict[tuple[str, str | None], dict[str, Any]] = OrderedDict()
    for event in events:
        key = (str(event["endpoint"]), event.get("view"))
        metric = metrics.setdefault(key, _empty_metric())
        _record_into(
            metric,
            int(event["status_code"]),
            float(event["duration_ms"]),
            event.get("response_bytes"),
        )
    return metrics


def traffic_metrics_snapshot(window_minutes: int | None = None, top_limit: int = 10) -> dict[str, Any]:
    now = time.time()
    window_seconds = max(60, int((window_minutes or WINDOW_SECONDS / 60) * 60))
    with _lock:
        _compact_window(now)
        since_start = deepcopy(_since_start)
        events = [event for event in _window_events if float(event["timestamp"]) >= now - window_seconds]
    window_metrics = _build_window_metrics(events)
    top = [_summarize_metric(key, metric) for key, metric in window_metrics.items()]
    top.sort(key=lambda item: (item["response_bytes_total"], item["requests"]), reverse=True)
    total_requests = sum(int(metric.get("request_count") or 0) for metric in window_metrics.values())
    total_bytes = sum(int(metric.get("response_bytes_total") or 0) for metric in window_metrics.values())
    alert_status = "normal"
    if any(item["alert_status"] == "critical" for item in top):
        alert_status = "critical"
    elif any(item["alert_status"] == "warning" for item in top):
        alert_status = "warning"
    return {
        "window_minutes": round(window_seconds / 60, 2),
        "total_requests": total_requests,
        "total_response_bytes": total_bytes,
        "endpoint_count": len(since_start),
        "max_endpoint_count": MAX_ENDPOINT_KEYS,
        "top_endpoints": top[: max(1, min(int(top_limit or 10), 25))],
        "alert_status": alert_status,
        "internal_excluded": sorted(_INTERNAL_EXCLUDED_ENDPOINTS),
        "storage": "process_memory",
        "response_bytes_source": "content-length",
    }
