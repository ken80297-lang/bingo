from __future__ import annotations

import json
import os
import threading
from datetime import date

from database import get_connection
from services.learning_engine import evaluate_shadow_rule_promotions


def evaluate_shadow_rules_for_date(draw_date: str) -> dict:
    """Evaluate retained shadow rules only from snapshots that existed before each target draw."""
    parsed = date.fromisoformat(str(draw_date))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select o.issue, o.numbers, l.analysis_snapshot
                from official_draw_history o
                join lateral (
                    select analysis_snapshot
                    from learning_history l
                    where coalesce(l.target_issue, l.issue) = o.issue
                      and l.analysis_snapshot is not null
                      and l.prediction_created_at is not null
                    order by l.prediction_created_at asc, l.id asc
                    limit 1
                ) l on true
                where o.draw_date = %s
                  and o.issue ~ '^[0-9]+$'
                  and jsonb_typeof(o.numbers) = 'array'
                  and jsonb_array_length(o.numbers) = 20
                order by o.issue::bigint desc
                """,
                (parsed,),
                prepare=False,
            )
            rows = cur.fetchall()

    records = [
        {"issue": str(issue), "official_numbers": numbers or [], "analysis_snapshot": analysis or {}}
        for issue, numbers, analysis in rows
    ]
    result = evaluate_shadow_rule_promotions(records, source_issue=(records[0]["issue"] if records else None))
    result.update(
        {
            "status": "ok",
            "draw_date": parsed.isoformat(),
            "official_targets_with_snapshot": len(records),
            "first_issue": records[-1]["issue"] if records else None,
            "last_issue": records[0]["issue"] if records else None,
            "method": "historical_snapshot_only",
            "leakage_guard": "prediction_created_at snapshot joined to its target issue; no future recomputation",
        }
    )
    return result


def _bootstrap_date_evaluation() -> None:
    requested = str(os.getenv("SHADOW_DATE_BOOTSTRAP") or "").strip()
    if not requested:
        return
    try:
        payload = evaluate_shadow_rules_for_date(requested)
        print("shadow_date_bootstrap_result=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"shadow_date_bootstrap_failed date={requested} error_type={type(exc).__name__} error={exc}")


os.environ["SHADOW_DATE_BOOTSTRAP"] = "2026-09-28"\nthreading.Thread(target=_bootstrap_date_evaluation, name="shadow-date-bootstrap", daemon=True).start()\n