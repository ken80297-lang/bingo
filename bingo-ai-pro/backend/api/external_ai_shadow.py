"""Read-only external AI shadow status for the separate comparison page."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/external-ai-shadow", tags=["external-ai-shadow"])


@router.get("/latest")
def latest_shadow():
    from database.official_draw_store import get_latest_official_draw
    draw = get_latest_official_draw()
    issue = str(draw.get("issue", "")) if draw else ""
    result = {"latest_official_issue": issue or None, "target_issue": str(int(issue) + 1) if issue.isdigit() else None,
              "external_ai": {"status": "waiting", "numbers": [], "top5": [], "super_number": None},
              "verification": None}
    if not issue.isdigit():
        return result
    try:
        from database.postgres import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""select prediction_issue, recommend_numbers, top5, super_number, status,
                                      hit_count, top5_hit_count, super_hit, generated_at
                               from public.external_ai_shadow_predictions
                               where prediction_issue in (%s,%s) and provider='groq'
                               order by prediction_issue desc, generated_at desc""",
                            (issue, str(int(issue) + 1)))
                for target, numbers, top5, super_number, status, hits, top_hits, super_hit, generated_at in cur.fetchall():
                    if str(target) == str(int(issue) + 1):
                        result["external_ai"] = {"status": status, "numbers": numbers, "top5": top5,
                                                 "super_number": super_number, "generated_at": generated_at.isoformat()}
                    elif str(target) == issue and status == "verified":
                        result["verification"] = {"issue": issue, "hits": hits, "top5_hits": top_hits,
                                                  "super_hit": super_hit}
    except Exception:
        result["external_ai"]["status"] = "unavailable"
    return result
