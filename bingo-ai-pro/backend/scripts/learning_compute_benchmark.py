from __future__ import annotations

import time

from database.analysis_store import get_analysis_history_with_timing
from services.model_engine import run_all_models


def _run_history_probe(*, use_dashboard_read_pool: bool) -> tuple[list[dict], dict]:
    draws, timing = get_analysis_history_with_timing(
        100,
        use_dashboard_read_pool=use_dashboard_read_pool,
    )
    print(
        "LEARNING_HISTORY_BENCHMARK "
        f"read_only=true pool={str(use_dashboard_read_pool).lower()} "
        f"backend={timing.get('backend')} row_count={timing.get('row_count')} "
        f"connect_ms={timing.get('connect_ms')} pool_acquire_ms={timing.get('pool_acquire_ms')} "
        f"execute_ms={timing.get('execute_ms')} fetch_ms={timing.get('fetch_ms')} "
        f"transform_ms={timing.get('transform_ms')} "
        f"total_with_transform_ms={timing.get('total_with_transform_ms')}",
        flush=True,
    )
    return draws, timing


def main() -> None:
    started = time.perf_counter()
    legacy_draws, legacy_timing = _run_history_probe(use_dashboard_read_pool=False)
    pooled_draws, pooled_timing = _run_history_probe(use_dashboard_read_pool=True)

    models_started = time.perf_counter()
    payload = run_all_models(100, draws=pooled_draws)
    models_ms = round((time.perf_counter() - models_started) * 1000.0, 2)
    total_ms = round((time.perf_counter() - started) * 1000.0, 2)

    models = payload.get("models") or []
    print(
        "LEARNING_COMPUTE_BENCHMARK "
        f"read_only=true history_records={len(pooled_draws or [])} "
        f"legacy_total_ms={legacy_timing.get('total_with_transform_ms')} "
        f"pooled_total_ms={pooled_timing.get('total_with_transform_ms')} "
        f"learning_models_compute_ms={models_ms} total_probe_ms={total_ms} "
        f"model_count={len(models)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
