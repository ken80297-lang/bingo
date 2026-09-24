from __future__ import annotations

import os

from fastapi import APIRouter, Request

from config.runtime_flags import get_scheduler_runtime_flags
from database.collector_store import get_collector_db_path_status
from database.prediction_history_store import classify_session_pooler_connection_failure
from database.prediction_history_store import get_card_two_history_timing_status
from database.prediction_history_store import identify_render_supabase_route
from database.prediction_history_store import run_card_two_autocommit_roundtrip_diagnostic
from database.prediction_history_store import run_card_two_connection_path_ab_benchmark
from database.prediction_history_store import run_card_two_connection_path_benchmark
from database.prediction_history_store import run_card_two_contention_isolation_benchmark
from database.prediction_history_store import run_card_two_ordering_benchmark
from database.prediction_history_store import run_card_two_roundtrip_diagnostic
from database.prediction_history_store import run_card_two_stepwise_latency_benchmark
from database.prediction_history_store import run_network_roundtrip_decomposition

router = APIRouter(prefix="/api", tags=["Runtime Diagnostics"])


def _render_metadata() -> dict[str, str | None]:
    return {
        "git_commit": os.getenv("RENDER_GIT_COMMIT"),
        "service_id": os.getenv("RENDER_SERVICE_ID"),
        "service_name": os.getenv("RENDER_SERVICE_NAME"),
        "instance_id": os.getenv("RENDER_INSTANCE_ID"),
    }


@router.get("/runtime-diagnostics")
def api_runtime_diagnostics(request: Request) -> dict:
    from services.player_dashboard import get_dashboard_component_diagnostics
    from services.player_dashboard import get_prediction_aggregate_component_diagnostics

    return {
        "status": "ok",
        "instance_started_at": getattr(request.app.state, "instance_started_at", None),
        "render": _render_metadata(),
        "scheduler_flags": get_scheduler_runtime_flags(),
        "collector_db_path": get_collector_db_path_status(),
        "card_two_history_timing": get_card_two_history_timing_status(),
        "dashboard_component_diagnostics": get_dashboard_component_diagnostics(),
        "prediction_aggregate_component_diagnostics": get_prediction_aggregate_component_diagnostics(),
    }


@router.post("/runtime-diagnostics/card-two-roundtrip")
def api_card_two_roundtrip_diagnostic() -> dict:
    return run_card_two_roundtrip_diagnostic()


@router.post("/runtime-diagnostics/card-two-autocommit-roundtrip")
def api_card_two_autocommit_roundtrip_diagnostic() -> dict:
    return run_card_two_autocommit_roundtrip_diagnostic()


@router.post("/runtime-diagnostics/card-two-connection-path-benchmark")
def api_card_two_connection_path_benchmark() -> dict:
    return run_card_two_connection_path_benchmark()


@router.post("/runtime-diagnostics/card-two-connection-path-ab-benchmark")
def api_card_two_connection_path_ab_benchmark() -> dict:
    return run_card_two_connection_path_ab_benchmark()


@router.post("/runtime-diagnostics/session-pooler-connection-classification")
def api_session_pooler_connection_classification() -> dict:
    return classify_session_pooler_connection_failure()


@router.post("/runtime-diagnostics/network-roundtrip-decomposition")
def api_network_roundtrip_decomposition() -> dict:
    return run_network_roundtrip_decomposition()


@router.post("/runtime-diagnostics/render-supabase-route")
def api_render_supabase_route() -> dict:
    return identify_render_supabase_route()


@router.post("/runtime-diagnostics/card-two-dashboard-context-benchmark")
def api_card_two_dashboard_context_benchmark() -> dict:
    from services.player_dashboard import run_card_two_dashboard_context_benchmark

    return run_card_two_dashboard_context_benchmark()


@router.post("/runtime-diagnostics/card-two-isolated-dashboard-context-benchmark")
def api_card_two_isolated_dashboard_context_benchmark() -> dict:
    from services.player_dashboard import run_isolated_card_two_dashboard_context_benchmark

    return run_isolated_card_two_dashboard_context_benchmark()


@router.post("/runtime-diagnostics/card-two-concurrency-culprit-benchmark")
def api_card_two_concurrency_culprit_benchmark() -> dict:
    from services.player_dashboard import run_card_two_concurrency_culprit_benchmark

    return run_card_two_concurrency_culprit_benchmark()


@router.post("/runtime-diagnostics/card-two-contention-isolation-benchmark")
def api_card_two_contention_isolation_benchmark() -> dict:
    return run_card_two_contention_isolation_benchmark()


@router.post("/runtime-diagnostics/card-two-ordering-benchmark")
def api_card_two_ordering_benchmark() -> dict:
    return run_card_two_ordering_benchmark()


@router.post("/runtime-diagnostics/card-two-stepwise-latency-benchmark")
def api_card_two_stepwise_latency_benchmark() -> dict:
    return run_card_two_stepwise_latency_benchmark()


@router.api_route("/runtime-diagnostics/learning-compute-benchmark", methods=["GET", "POST"])
def api_learning_compute_benchmark() -> dict:
    """Read-only benchmark for the V7 learning input load and model computation."""
    import time

    from database.analysis_store import get_analysis_history
    from services.model_engine import run_all_models

    started = time.perf_counter()
    history_started = time.perf_counter()
    draws = get_analysis_history(100)
    history_ms = round((time.perf_counter() - history_started) * 1000.0, 2)

    models_started = time.perf_counter()
    payload = run_all_models(100, draws=draws)
    models_ms = round((time.perf_counter() - models_started) * 1000.0, 2)
    models = payload.get("models") or []
    total_ms = round((time.perf_counter() - started) * 1000.0, 2)
    print(
        f"LEARNING_COMPUTE_BENCHMARK read_only=true history_records={len(draws or [])} "
        f"learning_history_load_ms={history_ms} learning_models_compute_ms={models_ms} total_ms={total_ms}",
        flush=True,
    )

    return {
        "status": "ok",
        "read_only": True,
        "history_records": len(draws or []),
        "learning_history_load_ms": history_ms,
        "learning_models_compute_ms": models_ms,
        "total_ms": total_ms,
        "models": [
            {
                "name": model.get("name") or model.get("model_name"),
                "candidate_count": len(model.get("candidates") or model.get("numbers") or []),
            }
            for model in models
        ],
    }
