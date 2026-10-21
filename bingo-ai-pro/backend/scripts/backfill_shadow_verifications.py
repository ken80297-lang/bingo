"""Backfill only existing frozen shadow predictions; no prediction regeneration.

Usage:
    python scripts/backfill_shadow_verifications.py             # dry run
    python scripts/backfill_shadow_verifications.py --apply     # write verification

Requires the configured database. Never run in the draw-critical Cron.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")

    from database import get_connection
    from database.shadow_dynamic_prediction_store import verify_shadow_dynamic_predictions

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select distinct s.prediction_issue, o.numbers
                   from shadow_dynamic_predictions s
                   join official_draw_history o on o.issue = s.prediction_issue
                   where s.status = 'pending'
                   order by s.prediction_issue
                   limit %s""",
                (args.limit,),
            )
            rows = cur.fetchall()
    summary = {"mode": "apply" if args.apply else "dry_run", "eligible_issues": len(rows), "verified_rows": 0, "errors": []}
    for issue, raw_numbers in rows:
        try:
            numbers = json.loads(raw_numbers) if isinstance(raw_numbers, str) else raw_numbers
            if not isinstance(numbers, list) or len(numbers) != 20 or len(set(numbers)) != 20:
                raise ValueError("official draw must contain 20 distinct numbers")
            if args.apply:
                result = verify_shadow_dynamic_predictions(str(issue), numbers)
                if result.get("status") != "ok" or result.get("storage") != "cloud":
                    raise RuntimeError(f"cloud verification failed: {result}")
                summary["verified_rows"] += result.get("updated", 0)
        except Exception as exc:
            summary["errors"].append({"issue": str(issue), "error": str(exc)})
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
