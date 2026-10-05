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


def main() -> int:
    issue = str(os.getenv("AI_LIFECYCLE_ISSUE", "")).strip()
    if not issue.isdigit():
        print(json.dumps({"status": "error", "reason": "AI_LIFECYCLE_ISSUE must be numeric"}), flush=True)
        return 2

    started = time.perf_counter()
    rss_before = _rss_mb()

    from database.official_draw_store import get_official_draw_by_issue
    from services.prediction_lifecycle_orchestrator import (
        process_official_draw_lifecycle,
        shutdown_lifecycle_background_tasks,
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

    result = process_official_draw_lifecycle(
        draw,
        source="ai_lifecycle_worker",
        trigger="isolated_recovery",
        caller="scripts.ai_lifecycle_worker_once",
        create_next_prediction=allow_prediction,
        learning_synchronous=True,
    )

    shutdown_lifecycle_background_tasks(wait=True)
    gc.collect()
    payload = {
        "status": result.get("status"),
        "issue": issue,
        "verification_status": (result.get("verification") or {}).get("status"),
        "learning_status": (result.get("learning") or {}).get("status"),
        "prediction_status": (result.get("prediction") or {}).get("status"),
        "prediction_target_issue": (result.get("prediction") or {}).get("target_issue"),
        "create_next_prediction": allow_prediction,
        "next_draw_already_exists": next_draw is not None,
        "timings_ms": result.get("timings_ms"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "rss_mb_before": rss_before,
        "rss_mb_peak": _rss_mb(),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
