from __future__ import annotations

import time

from database.analysis_store import get_analysis_history
from services.model_engine import run_all_models


def main() -> None:
    started = time.perf_counter()
    history_started = time.perf_counter()
    draws = get_analysis_history(100)
    history_ms = round((time.perf_counter() - history_started) * 1000.0, 2)

    models_started = time.perf_counter()
    payload = run_all_models(100, draws=draws)
    models_ms = round((time.perf_counter() - models_started) * 1000.0, 2)
    total_ms = round((time.perf_counter() - started) * 1000.0, 2)

    models = payload.get("models") or []
    print(
        "LEARNING_COMPUTE_BENCHMARK "
        f"read_only=true history_records={len(draws or [])} "
        f"learning_history_load_ms={history_ms} "
        f"learning_models_compute_ms={models_ms} total_ms={total_ms} "
        f"model_count={len(models)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
