from __future__ import annotations

import gc
import json
import os
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / 1024.0, 2)


def _shutdown_one_shot_resources() -> dict:
    results: dict[str, object] = {}

    def run(name: str, fn) -> None:
        try:
            results[name] = fn()
        except Exception as exc:
            results[name] = {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}

    from database import postgres
    from services.learning_engine import shutdown_learning_snapshot_executor
    from services.latest_sync import shutdown_latest_sync_background_tasks
    from services.prediction_lifecycle_orchestrator import shutdown_lifecycle_background_tasks
    from services.prediction_service import shutdown_prediction_background_tasks
    from services.shadow_dynamic_observer import shutdown_shadow_generate_executor, shutdown_shadow_verify_executor

    run("lifecycle", lambda: shutdown_lifecycle_background_tasks(wait=False))
    run("prediction", lambda: shutdown_prediction_background_tasks(wait=False))
    run("latest_sync", lambda: shutdown_latest_sync_background_tasks(wait=False, timeout_seconds=0))
    run("learning_snapshot", lambda: shutdown_learning_snapshot_executor(wait=False))
    run("shadow_verify", lambda: shutdown_shadow_verify_executor() or {"status": "stopped"})
    run("shadow_generate", lambda: shutdown_shadow_generate_executor() or {"status": "stopped"})
    run("dashboard_read_pool", lambda: postgres.close_dashboard_read_pool() or {"status": "closed"})
    run("prediction_lock_pool", lambda: postgres.close_prediction_lock_pool() or {"status": "closed"})
    run("prediction_write_pool", lambda: postgres.close_prediction_write_pool() or {"status": "closed"})
    return results


def main() -> int:
    issue = str(os.getenv("AI_LIFECYCLE_ISSUE", "")).strip()
    if not issue.isdigit():
        print(json.dumps({"status": "error", "reason": "AI_LIFECYCLE_ISSUE must be numeric"}), flush=True)
        return 2

    started = time.perf_counter()
    rss_before = _rss_mb()

    # Start the durable prediction write connection in the background before
    # lifecycle work reaches the save stage. The save itself remains synchronous.
    from database import postgres
    postgres.warm_prediction_write_pool()

    from database.official_draw_store import get_official_draw_by_issue
    from services.prediction_lifecycle_orchestrator import (
        process_official_draw_lifecycle,
    )

    draw = get_official_draw_by_issue(issue)
    if not draw:
        print(json.dumps({
            "status": "error",
            "reason": "official_draw_not_found",
            "issue": issue,
            "rss_mb_before": rss_before,
            "rss_mb_peak": _rss_mb(),
        }), flush=True)
        return 3

    next_issue = str(int(issue) + 1)
    next_draw = get_official_draw_by_issue(next_issue)
    allow_prediction = next_draw is None
    if os.getenv("AI_LIFECYCLE_CRON_CORE_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}:
        from datetime import timedelta
        from scripts.ai_lifecycle_cron_once import draw_datetime, prediction_is_timely

        draw_at = draw_datetime(draw)
        target_time = draw_at + timedelta(minutes=5) if draw_at else None
        if not prediction_is_timely(draw):
            allow_prediction = False
            print(json.dumps({"guard": "prediction_deadline_passed", "issue": issue,
                              "target_time": target_time.isoformat() if target_time else None,
                              "create_next_prediction": False}), flush=True)
    if next_draw is not None:
        print(json.dumps({
            "guard": "historical_target_already_drawn",
            "issue": issue,
            "next_issue": next_issue,
            "create_next_prediction": False,
        }, ensure_ascii=False, sort_keys=True), flush=True)

    result = process_official_draw_lifecycle(
        draw,
        source="ai_lifecycle_worker",
        trigger="isolated_recovery",
        caller="scripts.ai_lifecycle_worker_once",
        create_next_prediction=allow_prediction,
        learning_synchronous=True,
    )

    # Prediction creation already queues snapshot recovery on a single bounded
    # worker. Do not synchronously drain it again in this 512 MiB one-shot Cron:
    # doing the same recovery twice can overlap with the queued worker and push
    # the process over its memory limit after the durable prediction is saved.
    prediction_target_issue = (result.get("prediction") or {}).get("target_issue")
    snapshot_drain = {
        "status": "deferred",
        "reason": "prediction_service_bounded_worker",
        "target_issue": prediction_target_issue,
    }

    # Learning is synchronous in this worker. Cancel incidental background work
    # and close psycopg pools explicitly so Python finalization does not try to
    # join pool threads after the one-shot Cron has already done its durable
    # writes.
    shutdown_results = _shutdown_one_shot_resources()
    gc.collect()
    payload = {
        "status": result.get("status"),
        "issue": issue,
        "verification_status": (result.get("verification") or {}).get("status"),
        "learning_status": (result.get("learning") or {}).get("status"),
        "prediction_status": (result.get("prediction") or {}).get("status"),
        "prediction_target_issue": prediction_target_issue,
        "snapshot_drain": snapshot_drain,
        "create_next_prediction": allow_prediction,
        "next_draw_already_exists": next_draw is not None,
        "timings_ms": result.get("timings_ms"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "rss_mb_before": rss_before,
        "rss_mb_peak": _rss_mb(),
        "shutdown": shutdown_results,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
