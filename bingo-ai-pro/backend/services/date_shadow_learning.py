from __future__ import annotations

import json
import os
import threading
from datetime import date

from database import get_connection
from database.analysis_store import build_analysis_record
from services.learning_engine import evaluate_shadow_rule_promotions
from database.learning_store import save_shadow_rule_promotions


def _date_draws(draw_date: str, history_limit: int = 120) -> tuple[list[dict], list[dict]]:
    parsed = date.fromisoformat(str(draw_date))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select issue, draw_time, numbers, super_number, source
                from official_draw_history
                where draw_date = %s and issue ~ '^[0-9]+$'
                  and jsonb_typeof(numbers) = 'array' and jsonb_array_length(numbers) = 20
                order by issue::bigint asc
                """,
                (parsed,), prepare=False,
            )
            day_rows = cur.fetchall()
            first_issue = str(day_rows[0][0]) if day_rows else None
            history_rows = []
            if first_issue:
                cur.execute(
                    """
                    select issue, draw_time, numbers, super_number, source
                    from official_draw_history
                    where issue ~ '^[0-9]+$' and issue::bigint < %s::bigint
                      and jsonb_typeof(numbers) = 'array' and jsonb_array_length(numbers) = 20
                    order by issue::bigint desc limit %s
                    """,
                    (first_issue, int(history_limit)), prepare=False,
                )
                history_rows = cur.fetchall()

    def pack(row):
        return {"issue": str(row[0]), "draw_time": row[1], "numbers": row[2] or [],
                "super_number": row[3], "source": row[4] or "taiwan_lottery"}
    return [pack(row) for row in day_rows], [pack(row) for row in history_rows]


def replay_shadow_rules_for_date(draw_date: str) -> dict:
    """Strict walk-forward replay: features for target T use only draws before T."""
    day_draws, prior_desc = _date_draws(draw_date)
    prior = list(reversed(prior_desc))
    records = []
    history = prior[:]
    for target in day_draws:
        if not history:
            history.append(target)
            continue
        source = history[-1]
        # build_analysis_record expects recent draws newest first. The source draw is the current
        # feature anchor; history passed to helpers contains only draws strictly before source.
        earlier_desc = list(reversed(history[:-1]))[:120]
        analysis = build_analysis_record(source, recent_draws=earlier_desc)
        records.append({
            "issue": target["issue"],
            "official_numbers": target["numbers"],
            "analysis_snapshot": analysis,
        })
        history.append(target)

    result = evaluate_shadow_rule_promotions(
        list(reversed(records)),
        source_issue=(day_draws[-1]["issue"] if day_draws else None),
    )
    result.update({
        "status": "ok",
        "draw_date": str(draw_date),
        "official_draw_count": len(day_draws),
        "replayed_target_count": len(records),
        "first_issue": day_draws[0]["issue"] if day_draws else None,
        "last_issue": day_draws[-1]["issue"] if day_draws else None,
        "prior_history_count": len(prior),
        "method": "strict_walk_forward_replay",
        "leakage_guard": "target T features anchored at T-1 and use only draws before T",
    })
    return result


def evaluate_shadow_rules_for_date(draw_date: str) -> dict:
    parsed = date.fromisoformat(str(draw_date))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select o.issue, o.numbers, l.analysis_snapshot
                from official_draw_history o
                join lateral (
                    select analysis_snapshot from learning_history l
                    where coalesce(l.target_issue, l.issue) = o.issue
                      and l.analysis_snapshot is not null and l.prediction_created_at is not null
                    order by l.prediction_created_at asc, l.id asc limit 1
                ) l on true
                where o.draw_date = %s and o.issue ~ '^[0-9]+$'
                  and jsonb_typeof(o.numbers) = 'array' and jsonb_array_length(o.numbers) = 20
                order by o.issue::bigint desc
                """, (parsed,), prepare=False,
            )
            rows = cur.fetchall()
    records = [{"issue": str(i), "official_numbers": n or [], "analysis_snapshot": a or {}} for i,n,a in rows]
    result = evaluate_shadow_rule_promotions(records, source_issue=(records[0]["issue"] if records else None))
    result.update({"status":"ok","draw_date":parsed.isoformat(),"official_targets_with_snapshot":len(records),
                   "first_issue":records[-1]["issue"] if records else None,"last_issue":records[0]["issue"] if records else None,
                   "method":"historical_snapshot_only","leakage_guard":"no future recomputation"})
    return result


def _bootstrap_date_evaluation() -> None:
    requested = str(os.getenv("SHADOW_DATE_BOOTSTRAP") or "").strip()
    if not requested: return
    try:
        payload = replay_shadow_rules_for_date(requested)
        print("shadow_date_bootstrap_result=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"shadow_date_bootstrap_failed date={requested} error_type={type(exc).__name__} error={exc}")


if os.getenv("SHADOW_DATE_BOOTSTRAP"):
    threading.Thread(target=_bootstrap_date_evaluation, name="shadow-date-bootstrap", daemon=True).start()
