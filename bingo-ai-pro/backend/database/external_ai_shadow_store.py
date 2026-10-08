"""Durable storage for external AI shadow predictions; never touches production rows."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def save_shadow_prediction(*, based_on_issue: str, prediction_issue: str, model: str, result: dict) -> bool:
    if result.get("status") != "ok" or not based_on_issue.isdigit() or not prediction_issue.isdigit():
        return False
    if int(prediction_issue) != int(based_on_issue) + 1:
        return False
    from database.postgres import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into public.external_ai_shadow_predictions
                   (based_on_issue,prediction_issue,provider,model,recommend_numbers,top5,super_number)
                   values (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                   on conflict (prediction_issue,provider,model) do nothing""",
                (based_on_issue, prediction_issue, result["provider"], model,
                 json.dumps(result["numbers"]), json.dumps(result["top5"]), result["super_number"]),
            )
            return cur.rowcount == 1


def verify_pending_shadow_prediction(draw: dict) -> int:
    """Verify pending rows for exactly this draw, without changing earlier results."""
    from database.postgres import get_connection
    from services.external_ai_shadow_verification import verify_shadow_prediction
    issue = str(draw.get("issue", ""))
    if not issue.isdigit():
        return 0
    verified = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """select id, prediction_issue, recommend_numbers, top5, super_number, generated_at
                   from public.external_ai_shadow_predictions
                   where prediction_issue=%s and verified_at is null for update""",
                (issue,),
            )
            for row in cur.fetchall():
                row_id, target, numbers, top5, super_number, generated_at = row
                draw_time = draw.get("draw_time")
                if isinstance(draw_time, datetime):
                    draw_at = draw_time if draw_time.tzinfo else draw_time.replace(tzinfo=timezone.utc)
                else:
                    try:
                        draw_at = datetime.fromisoformat(str(draw_time).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                    if draw_at.tzinfo is None:
                        # Ambiguous naive timestamp: do not claim prospective verification.
                        continue
                if generated_at >= draw_at:
                    continue
                comparison = verify_shadow_prediction(
                    {"prediction_issue": target, "numbers": numbers, "top5": top5, "super_number": super_number}, draw
                )
                cur.execute(
                    """update public.external_ai_shadow_predictions
                       set verified_at=now(), actual_numbers=%s::jsonb, actual_super_number=%s,
                           hit_count=%s, top5_hit_count=%s, super_hit=%s, status='verified'
                       where id=%s and verified_at is null""",
                    (json.dumps(comparison["actual_numbers"]), comparison["actual_super_number"],
                     comparison["hit_count"], comparison["top5_hit_count"], comparison["super_hit"], row_id),
                )
                verified += cur.rowcount
    return verified
