from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.responses import JSONResponse, StreamingResponse

import app as app_module
from services import operations_center, traffic_observability


def setup_function():
    traffic_observability.reset_traffic_metrics()


def _top(endpoint: str, view: str | None = None) -> dict:
    top = traffic_observability.traffic_metrics_snapshot()["top_endpoints"]
    for item in top:
        if item["endpoint"] == endpoint and item.get("view") == view:
            return item
    raise AssertionError(f"missing metric for {endpoint} view={view}")


def test_get_request_records_count_bytes_and_duration():
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/official/history",
        status_code=200,
        duration_ms=12.5,
        response_bytes=2048,
    )

    item = _top("/api/official/history")

    assert item["requests"] == 1
    assert item["response_bytes_total"] == 2048
    assert item["avg_duration_ms"] == 12.5
    assert item["status_2xx"] == 1


def test_query_string_is_not_part_of_endpoint_key():
    endpoint = traffic_observability.normalize_traffic_endpoint(
        "/api/official/history",
        route_path="/api/official/history",
    )

    traffic_observability.record_traffic_request(
        method="GET",
        endpoint=endpoint,
        status_code=200,
        duration_ms=1,
        response_bytes=100,
    )
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint=endpoint,
        status_code=200,
        duration_ms=1,
        response_bytes=200,
    )

    item = _top("/api/official/history")

    assert item["requests"] == 2
    assert item["response_bytes_total"] == 300


def test_summary_full_view_tags_are_low_cardinality():
    for view in ("summary", "full", "debug", "issue=115000001"):
        traffic_observability.record_traffic_request(
            method="GET",
            endpoint="/api/prediction-history/history",
            status_code=200,
            duration_ms=1,
            response_bytes=10,
            view=view,
        )

    summary = _top("/api/prediction-history/history", "summary")
    full = _top("/api/prediction-history/history", "full")
    untagged = _top("/api/prediction-history/history")

    assert summary["requests"] == 1
    assert full["requests"] == 1
    assert untagged["requests"] == 2


def test_metrics_endpoint_is_not_counted_but_health_is_counted():
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/operations/metrics",
        status_code=200,
        duration_ms=1,
        response_bytes=999,
    )
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/health",
        status_code=200,
        duration_ms=1,
        response_bytes=42,
    )

    snapshot = traffic_observability.traffic_metrics_snapshot()

    assert snapshot["total_requests"] == 1
    assert snapshot["top_endpoints"][0]["endpoint"] == "/api/health"
    assert "/api/operations/metrics" in snapshot["internal_excluded"]


def test_post_requests_are_not_counted():
    traffic_observability.record_traffic_request(
        method="POST",
        endpoint="/api/collector/catch-up/run",
        status_code=200,
        duration_ms=1,
        response_bytes=100,
    )

    assert traffic_observability.traffic_metrics_snapshot()["total_requests"] == 0


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(traffic_observability, "MAX_ENDPOINT_KEYS", 3)
    traffic_observability.reset_traffic_metrics()

    for index in range(5):
        traffic_observability.record_traffic_request(
            method="GET",
            endpoint=f"/api/public/{index}",
            status_code=200,
            duration_ms=1,
            response_bytes=10,
        )

    snapshot = traffic_observability.traffic_metrics_snapshot()

    assert snapshot["endpoint_count"] == 3
    assert snapshot["max_endpoint_count"] == 3


def test_warning_and_critical_thresholds(monkeypatch):
    monkeypatch.setattr(traffic_observability, "WARNING_REQUESTS", 1)
    monkeypatch.setattr(traffic_observability, "WARNING_TOTAL_RESPONSE_BYTES", 50)
    monkeypatch.setattr(traffic_observability, "WARNING_AVG_RESPONSE_BYTES", 50)
    monkeypatch.setattr(traffic_observability, "CRITICAL_TOTAL_RESPONSE_BYTES", 100)
    traffic_observability.reset_traffic_metrics()

    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/heavy",
        status_code=200,
        duration_ms=1,
        response_bytes=60,
    )
    warning = _top("/api/heavy")
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/heavy",
        status_code=200,
        duration_ms=1,
        response_bytes=60,
    )
    critical = _top("/api/heavy")

    assert warning["alert_status"] == "warning"
    assert critical["alert_status"] == "critical"


def test_threshold_env_parser_falls_back_on_invalid_value(monkeypatch):
    monkeypatch.setenv("TRAFFIC_WARNING_REQUESTS", "not-an-int")

    assert traffic_observability._env_int("TRAFFIC_WARNING_REQUESTS", 100) == 100


def test_warning_logging_is_transition_only(monkeypatch, caplog):
    monkeypatch.setattr(traffic_observability, "WARNING_REQUESTS", 1)
    traffic_observability.reset_traffic_metrics()

    with caplog.at_level("WARNING", logger="services.traffic_observability"):
        for _ in range(3):
            traffic_observability.record_traffic_request(
                method="GET",
                endpoint="/api/burst",
                status_code=200,
                duration_ms=1,
                response_bytes=1,
            )

    assert [record.getMessage() for record in caplog.records].count(
        "traffic_alert status=warning endpoint=/api/burst view=- requests=2 response_bytes_total=2"
    ) == 1


def test_streaming_response_bytes_are_unknown_not_materialized():
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/stream",
        status_code=200,
        duration_ms=1,
        response_bytes=None,
    )

    item = _top("/api/stream")

    assert item["response_bytes_total"] == 0
    assert item["unknown_response_bytes"] == 1


def test_middleware_uses_content_length_without_reading_body():
    async def body():
        yield b"streamed"

    request = SimpleNamespace(
        method="GET",
        scope={"route": SimpleNamespace(path="/api/stream")},
        url=SimpleNamespace(path="/api/stream"),
        query_params={},
    )

    async def call_next(_request):
        return StreamingResponse(body())

    response = asyncio.run(app_module.record_get_traffic_metrics(request, call_next))
    item = _top("/api/stream")

    assert response.status_code == 200
    assert item["unknown_response_bytes"] == 1


def test_middleware_records_json_content_length_and_view():
    request = SimpleNamespace(
        method="GET",
        scope={"route": SimpleNamespace(path="/api/official/history")},
        url=SimpleNamespace(path="/api/official/history"),
        query_params={"view": "summary"},
    )

    async def call_next(_request):
        return JSONResponse({"status": "ok"})

    response = asyncio.run(app_module.record_get_traffic_metrics(request, call_next))
    item = _top("/api/official/history", "summary")

    assert response.status_code == 200
    assert item["response_bytes_total"] > 0


def test_middleware_records_exception_as_500_and_reraises():
    request = SimpleNamespace(
        method="GET",
        scope={"route": SimpleNamespace(path="/api/fail")},
        url=SimpleNamespace(path="/api/fail"),
        query_params={},
    )

    async def call_next(_request):
        raise RuntimeError("boom")

    try:
        asyncio.run(app_module.record_get_traffic_metrics(request, call_next))
        raise AssertionError("exception should be reraised")
    except RuntimeError:
        pass

    item = _top("/api/fail")

    assert item["status_5xx"] == 1
    assert item["unknown_response_bytes"] == 1


def test_operations_metrics_includes_traffic_without_database_writes(monkeypatch):
    monkeypatch.setattr(operations_center, "get_operation_metrics", lambda: {"status": "ok", "components": []})
    traffic_observability.record_traffic_request(
        method="GET",
        endpoint="/api/official/history",
        status_code=500,
        duration_ms=2,
        response_bytes=10,
    )

    payload = operations_center.operation_metrics()
    traffic = payload["traffic"]

    assert payload["status"] == "ok"
    assert traffic["total_requests"] == 1
    assert traffic["top_endpoints"][0]["errors"] == 1
