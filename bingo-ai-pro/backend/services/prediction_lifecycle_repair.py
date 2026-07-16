from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from database.prediction_history_store import (
    _cloud_enabled,
    _cloud_connection,
    _json_dumps,
    _normalize_numbers,
    _sqlite_connection,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _valid_issue(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or not text.isdigit():
        return None
    if text.startswith("99") or text.upper().startswith("TEST"):
        return None
    return text


def _json_loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _query(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> list[Any]:
    if _cloud_enabled():
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params, prepare=False)
                return cur.fetchall()
    with _sqlite_connection() as conn:
        return conn.execute(sqlite_sql or sql.replace("%s", "?"), params).fetchall()


def _execute(sql: str, params: tuple = (), sqlite_sql: str | None = None) -> int:
    if _cloud_enabled():
        with _cloud_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params, prepare=False)
                count = cur.rowcount or 0
            conn.commit()
            return count
    with _sqlite_connection() as conn:
        cursor = conn.execute(sqlite_sql or sql.replace("%s", "?"), params)
        return cursor.rowcount or 0


def _prediction_rows() -> list[dict]:
    rows = _query(
        """
        select
            p.id, p.issue, p.prediction_issue, p.recommend_numbers,
            p.super_number, p.winning_numbers, p.matched_numbers, p.missed_numbers,
            p.hit_count, p.prediction_count, p.super_number_hit,
            p.prediction_status, p.verified_at, p.learning_used,
            o.numbers as official_numbers, o.super_number as official_super_number
        from prediction_history p
        left join official_draw_history o on o.issue = p.prediction_issue
        order by p.id asc
        """
    )
    output = []
    for row in rows:
        output.append(
            {
                "id": row[0],
                "based_on_issue": row[1],
                "target_issue": row[2],
                "recommend_numbers": _normalize_numbers(_json_loads(row[3])),
                "super_number": row[4],
                "winning_numbers": _normalize_numbers(_json_loads(row[5])),
                "matched_numbers": _normalize_numbers(_json_loads(row[6])),
                "missed_numbers": _normalize_numbers(_json_loads(row[7])),
                "hit_count": row[8],
                "prediction_count": row[9],
                "super_number_hit": bool(row[10]),
                "prediction_status": row[11],
                "verified_at": row[12],
                "learning_used": bool(row[13]),
                "official_numbers": _normalize_numbers(_json_loads(row[14])),
                "official_super_number": row[15],
            }
        )
    return output


def _verification_payload(row: dict, verified_at: str) -> dict | None:
    recommended = _normalize_numbers(row.get("recommend_numbers"))
    official = _normalize_numbers(row.get("official_numbers") or row.get("winning_numbers"))
    if not recommended or not official:
        return None
    if len(official) != 20:
        return None
    matched = sorted(set(recommended) & set(official))
    missed = [number for number in recommended if number not in set(official)]
    prediction_count = len(recommended)
    hit_rate = round(len(matched) / max(1, prediction_count), 4)
    predicted_super = row.get("super_number")
    official_super = row.get("official_super_number")
    super_hit = bool(predicted_super is not None and official_super is not None and int(predicted_super) == int(official_super))
    return {
        "winning_numbers": official,
        "matched_numbers": matched,
        "missed_numbers": missed,
        "hit_count": len(matched),
        "prediction_count": prediction_count,
        "hit_rate": hit_rate,
        "super_number_hit": super_hit,
        "verified_issue": row.get("target_issue"),
        "verified_at": verified_at,
    }


def _is_complete_verified(row: dict) -> bool:
    return bool(
        row.get("target_issue")
        and row.get("prediction_status") == "verified"
        and row.get("verified_at")
        and len(row.get("winning_numbers") or []) == 20
        and row.get("matched_numbers") is not None
        and row.get("missed_numbers") is not None
    )


def verification_recovery(dry_run: bool = True) -> dict:
    rows = _prediction_rows()
    verified_at = _now()
    summary = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "scanned": len(rows),
        "would_verify": 0,
        "updated": 0,
        "already_complete": 0,
        "missing_target": 0,
        "missing_official_draw": 0,
        "data_format_error": 0,
        "skipped": {},
        "examples": [],
    }
    for row in rows:
        target = _valid_issue(row.get("target_issue"))
        if not target:
            summary["missing_target"] += 1
            summary["skipped"]["null_or_invalid_target"] = summary["skipped"].get("null_or_invalid_target", 0) + 1
            continue
        if not row.get("official_numbers") and not row.get("winning_numbers"):
            summary["missing_official_draw"] += 1
            summary["skipped"]["missing_official_draw"] = summary["skipped"].get("missing_official_draw", 0) + 1
            continue
        payload = _verification_payload(row, verified_at)
        if not payload:
            summary["data_format_error"] += 1
            summary["skipped"]["data_format_error"] = summary["skipped"].get("data_format_error", 0) + 1
            continue
        if _is_complete_verified({**row, **payload}):
            summary["already_complete"] += 1
            continue
        summary["would_verify"] += 1
        if len(summary["examples"]) < 10:
            summary["examples"].append(
                {
                    "target_issue": target,
                    "hit_count": payload["hit_count"],
                    "prediction_count": payload["prediction_count"],
                }
            )
        if not dry_run:
            updated = _update_verification(row["id"], payload)
            summary["updated"] += updated
    return summary


def _update_verification(row_id: int, payload: dict) -> int:
    params = (
        _json_dumps(payload["winning_numbers"]),
        payload["hit_count"],
        payload["hit_rate"],
        "verified",
        payload["verified_issue"],
        payload["verified_at"],
        _json_dumps(payload["matched_numbers"]),
        _json_dumps(payload["missed_numbers"]),
        payload["prediction_count"],
        payload["hit_rate"],
        payload["super_number_hit"],
        payload["super_number_hit"],
        "prediction_recovery_v1",
        payload["verified_at"],
        row_id,
    )
    return _execute(
        """
        update prediction_history
        set winning_numbers = %s::jsonb,
            hit_count = %s,
            accuracy = %s,
            prediction_status = %s,
            verified_issue = %s,
            verified_at = %s,
            matched_numbers = %s::jsonb,
            missed_numbers = %s::jsonb,
            prediction_count = %s,
            hit_rate = %s,
            super_number_hit = %s,
            super_hit = %s,
            verification_version = %s,
            updated_at = %s
        where id = %s
        """,
        params,
        sqlite_sql="""
        update prediction_history
        set winning_numbers = ?,
            hit_count = ?,
            accuracy = ?,
            prediction_status = ?,
            verified_issue = ?,
            verified_at = ?,
            matched_numbers = ?,
            missed_numbers = ?,
            prediction_count = ?,
            hit_rate = ?,
            super_number_hit = ?,
            super_hit = ?,
            verification_version = ?,
            updated_at = ?
        where id = ?
        """,
    )


def _update_verification_with_cursor(cur, row_id: int, payload: dict, cloud: bool) -> int:
    params = (
        _json_dumps(payload["winning_numbers"]),
        payload["hit_count"],
        payload["hit_rate"],
        "verified",
        payload["verified_issue"],
        payload["verified_at"],
        _json_dumps(payload["matched_numbers"]),
        _json_dumps(payload["missed_numbers"]),
        payload["prediction_count"],
        payload["hit_rate"],
        payload["super_number_hit"],
        payload["super_number_hit"],
        "prediction_recovery_v1",
        payload["verified_at"],
        row_id,
    )
    if cloud:
        cur.execute(
            """
            update prediction_history
            set winning_numbers = %s::jsonb,
                hit_count = %s,
                accuracy = %s,
                prediction_status = %s,
                verified_issue = %s,
                verified_at = %s,
                matched_numbers = %s::jsonb,
                missed_numbers = %s::jsonb,
                prediction_count = %s,
                hit_rate = %s,
                super_number_hit = %s,
                super_hit = %s,
                verification_version = %s,
                updated_at = %s
            where id = %s
            """,
            params,
            prepare=False,
        )
    else:
        cur.execute(
            """
            update prediction_history
            set winning_numbers = ?,
                hit_count = ?,
                accuracy = ?,
                prediction_status = ?,
                verified_issue = ?,
                verified_at = ?,
                matched_numbers = ?,
                missed_numbers = ?,
                prediction_count = ?,
                hit_rate = ?,
                super_number_hit = ?,
                super_hit = ?,
                verification_version = ?,
                updated_at = ?
            where id = ?
            """,
            params,
        )
    return cur.rowcount or 0


def _record_repair_event(summary: dict) -> None:
    try:
        from services.operations_center import record_operation_event

        record_operation_event(
            component="prediction",
            event_type="prediction_lifecycle_recovery_apply",
            status=summary.get("transaction", "unknown"),
            issue=None,
            message=_json_dumps(summary),
            duration_ms=None,
            error_type=summary.get("error_type"),
            error_message=summary.get("error"),
        )
    except Exception:
        logger.exception("prediction lifecycle recovery apply event failed")


def _verification_plan(rows: list[dict], verified_at: str) -> tuple[list[dict], list[dict]]:
    candidates = []
    skipped = []
    for row in rows:
        target = _valid_issue(row.get("target_issue"))
        if not target:
            skipped.append({"target_issue": row.get("target_issue"), "reason": "null_or_invalid_target"})
            continue
        if not row.get("official_numbers") and not row.get("winning_numbers"):
            skipped.append({"target_issue": target, "reason": "missing_official_draw"})
            continue
        payload = _verification_payload(row, verified_at)
        if not payload:
            skipped.append({"target_issue": target, "reason": "data_format_error"})
            continue
        if _is_complete_verified(row):
            skipped.append({"target_issue": target, "reason": "already_complete"})
            continue
        candidates.append({"row": row, "payload": payload, "target_issue": target})
    return candidates, skipped


def _learning_plan(targets: set[str], rows: list[Any]) -> tuple[list[dict], list[dict]]:
    by_target = {str(row[1]): {"id": row[0], "learning_used": bool(row[2])} for row in rows if row[1]}
    candidates = []
    skipped = []
    for target in sorted(targets):
        valid_target = _valid_issue(target)
        if not valid_target:
            skipped.append({"target_issue": target, "reason": "invalid_target"})
            continue
        row = by_target.get(valid_target)
        if not row:
            skipped.append({"target_issue": valid_target, "reason": "unmatched_learning_target"})
            continue
        if row["learning_used"]:
            skipped.append({"target_issue": valid_target, "reason": "already_learning_used"})
            continue
        candidates.append({"id": row["id"], "target_issue": valid_target})
    return candidates, skipped


def apply_recovery(expected_verification_count: int = 83, expected_learning_count: int = 15) -> dict:
    started_at = _now()
    pre_dry_run = dry_run_all()
    verification_rows = _prediction_rows()
    verified_at = started_at
    verification_candidates, verification_skipped = _verification_plan(verification_rows, verified_at)
    learned_targets = _learned_targets()
    learning_rows = _query(
        """
        select id, prediction_issue, learning_used
        from prediction_history
        where prediction_issue is not null
        """
    )
    learning_candidates, learning_skipped = _learning_plan(learned_targets, learning_rows)
    summary = {
        "status": "pending",
        "transaction": "not_started",
        "started_at": started_at,
        "completed_at": None,
        "pre_dry_run": pre_dry_run,
        "verification": {
            "scanned": len(verification_rows),
            "planned_update_count": len(verification_candidates),
            "updated_count": 0,
            "updated_targets": [],
            "skipped": verification_skipped,
            "failed": [],
        },
        "learning_sync": {
            "planned_update_count": len(learning_candidates),
            "updated_count": 0,
            "updated_targets": [],
            "skipped": learning_skipped,
            "failed": [],
        },
    }
    if len(verification_candidates) != expected_verification_count or len(learning_candidates) != expected_learning_count:
        summary["status"] = "aborted"
        summary["transaction"] = "not_started"
        summary["error_type"] = "unexpected_dry_run_count"
        summary["error"] = (
            f"expected verification={expected_verification_count}, learning={expected_learning_count}; "
            f"actual verification={len(verification_candidates)}, learning={len(learning_candidates)}"
        )
        summary["completed_at"] = _now()
        _record_repair_event(summary)
        return summary

    cloud = _cloud_enabled()
    try:
        if cloud:
            with _cloud_connection() as conn:
                try:
                    with conn.cursor() as cur:
                        for item in verification_candidates:
                            updated = _update_verification_with_cursor(cur, item["row"]["id"], item["payload"], True)
                            summary["verification"]["updated_count"] += updated
                            if updated:
                                summary["verification"]["updated_targets"].append(item["target_issue"])
                        for item in learning_candidates:
                            cur.execute(
                                """
                                update prediction_history
                                set learning_used = true,
                                    updated_at = now()
                                where id = %s
                                """,
                                (item["id"],),
                                prepare=False,
                            )
                            updated = cur.rowcount or 0
                            summary["learning_sync"]["updated_count"] += updated
                            if updated:
                                summary["learning_sync"]["updated_targets"].append(item["target_issue"])
                    conn.commit()
                    summary["transaction"] = "commit"
                except Exception:
                    conn.rollback()
                    summary["transaction"] = "rollback"
                    raise
        else:
            with _sqlite_connection() as conn:
                try:
                    cur = conn.cursor()
                    for item in verification_candidates:
                        updated = _update_verification_with_cursor(cur, item["row"]["id"], item["payload"], False)
                        summary["verification"]["updated_count"] += updated
                        if updated:
                            summary["verification"]["updated_targets"].append(item["target_issue"])
                    for item in learning_candidates:
                        cur.execute(
                            """
                            update prediction_history
                            set learning_used = 1,
                                updated_at = ?
                            where id = ?
                            """,
                            (_now(), item["id"]),
                        )
                        updated = cur.rowcount or 0
                        summary["learning_sync"]["updated_count"] += updated
                        if updated:
                            summary["learning_sync"]["updated_targets"].append(item["target_issue"])
                    conn.commit()
                    summary["transaction"] = "commit"
                except Exception:
                    conn.rollback()
                    summary["transaction"] = "rollback"
                    raise
        summary["status"] = "ok"
    except Exception as exc:
        logger.exception("prediction lifecycle recovery apply failed")
        summary["status"] = "error"
        summary["error_type"] = type(exc).__name__
        summary["error"] = str(exc)
    summary["completed_at"] = _now()
    _record_repair_event(summary)
    return summary


def _learned_targets() -> set[str]:
    rows = _query(
        """
        select distinct coalesce(target_issue, issue)
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%%'
        """,
        sqlite_sql="""
        select distinct coalesce(target_issue, issue)
        from learning_history
        where prediction_type = 'live_prediction'
          and learned_status = 'learned'
          and coalesce(target_issue, issue) is not null
          and coalesce(target_issue, issue) not like 'pending:%'
        """,
    )
    return {str(row[0]) for row in rows if row and _valid_issue(row[0])}


def learning_sync(dry_run: bool = True) -> dict:
    targets = _learned_targets()
    rows = _query(
        """
        select id, prediction_issue, learning_used
        from prediction_history
        where prediction_issue is not null
        """
    )
    by_target = {str(row[1]): {"id": row[0], "learning_used": bool(row[2])} for row in rows if row[1]}
    summary = {
        "status": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "learned_distinct_target_count": len(targets),
        "already_learning_used": 0,
        "would_sync": 0,
        "updated": 0,
        "unmatched_learning_target": 0,
        "invalid_target": 0,
        "examples": [],
    }
    for target in sorted(targets):
        if not _valid_issue(target):
            summary["invalid_target"] += 1
            continue
        row = by_target.get(target)
        if not row:
            summary["unmatched_learning_target"] += 1
            continue
        if row["learning_used"]:
            summary["already_learning_used"] += 1
            continue
        summary["would_sync"] += 1
        if len(summary["examples"]) < 10:
            summary["examples"].append(target)
        if not dry_run:
            if _cloud_enabled():
                summary["updated"] += _execute(
                    """
                    update prediction_history
                    set learning_used = true,
                        updated_at = now()
                    where id = %s
                    """,
                    (row["id"],),
                )
            else:
                summary["updated"] += _execute(
                    """
                    update prediction_history
                    set learning_used = 1,
                        updated_at = ?
                    where id = ?
                    """,
                    (_now(), row["id"]),
                )
    return summary


def official_draw_time_investigation() -> dict:
    rows = _query(
        """
        select
            count(*),
            sum(case when draw_time is null then 1 else 0 end),
            sum(case when draw_time is not null then 1 else 0 end),
            min(issue),
            max(issue)
        from official_draw_history
        """
    )
    raw_rows = _query(
        """
        select issue, raw_json
        from official_draw_history
        order by issue desc
        limit 20
        """
    )
    raw_time_keys = set()
    for _, raw in raw_rows:
        payload = _json_loads(raw) or {}
        if isinstance(payload, dict):
            for key in payload:
                lowered = str(key).lower()
                if "time" in lowered or "date" in lowered:
                    raw_time_keys.add(str(key))
    row = rows[0] if rows else [0, 0, 0, None, None]
    return {
        "official_draw_count": int(row[0] or 0),
        "missing_draw_time_count": int(row[1] or 0),
        "has_draw_time_count": int(row[2] or 0),
        "min_issue": row[3],
        "max_issue": row[4],
        "raw_time_like_keys": sorted(raw_time_keys),
        "finding": "official raw payload exposes date-like keys, but stored draw_time is missing for current official records",
    }


def dry_run_all() -> dict:
    return {
        "verification": verification_recovery(dry_run=True),
        "learning_sync": learning_sync(dry_run=True),
        "official_draw_time": official_draw_time_investigation(),
    }
