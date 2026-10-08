"""Standalone fail-open shadow job, intentionally separate from the 512 MB lifecycle cron.

Run only on a separately configured schedule with DATABASE_URL and GROQ_API_KEY.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    if os.getenv("EXTERNAL_AI_SHADOW_ENABLED") != "1":
        print(json.dumps({"status": "skipped", "reason": "disabled"}))
        return 0
    from database.official_draw_store import get_latest_official_draw
    from database.external_ai_shadow_store import save_shadow_prediction, verify_pending_shadow_prediction
    from services.external_ai_shadow import propose_shadow_numbers
    draw = get_latest_official_draw()
    if not draw or not str(draw.get("issue", "")).isdigit():
        print(json.dumps({"status": "skipped", "reason": "no_official_draw"}))
        return 0
    # This job may be scheduled at any time; no synthetic draws or fake backfills.
    verified = verify_pending_shadow_prediction(draw)
    issue = str(draw["issue"])
    next_issue = str(int(issue) + 1)
    # Never create a prediction after its target has already been drawn.
    from database.official_draw_store import get_official_draw_by_issue
    if get_official_draw_by_issue(next_issue):
        print(json.dumps({"status": "skipped", "reason": "target_already_drawn", "verified": verified}))
        return 0
    result = propose_shadow_numbers(draw, {}, timeout=6.0)
    stored = False
    if result.get("status") == "ok":
        stored = save_shadow_prediction(
            based_on_issue=issue, prediction_issue=next_issue,
            model=os.getenv("GROQ_SHADOW_MODEL", "llama-3.3-70b-versatile"), result=result,
        )
    print(json.dumps({"status": result.get("status"), "reason": result.get("reason"),
                      "based_on_issue": issue, "prediction_issue": next_issue,
                      "verified": verified, "stored": stored}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
