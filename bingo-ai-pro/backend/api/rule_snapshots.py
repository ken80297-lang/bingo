from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services.rule_snapshot import build_rule_snapshot_health, generate_rule_snapshot_for_issue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rule-snapshots", tags=["Rule Snapshots"])


class GenerateRuleSnapshotRequest(BaseModel):
    source_issue: str | None = None
    target_issue: str | None = None
    persist: bool = False


def _safe_limit(limit: int = 100) -> int:
    try:
        value = int(limit)
    except Exception:
        value = 100
    return max(1, min(value, 500))


def _safe_audit_limit(limit: int = 20) -> int:
    try:
        value = int(limit)
    except Exception:
        value = 20
    return max(1, min(value, 100))


def audit_rule_snapshot_fast_path(limit: int = 20):
    from services.recommendation_center import audit_rule_snapshot_fast_path as audit

    return audit(limit=limit)


def compare_rule_snapshot_fast_path(source_issue: str, target_issue: str | None = None):
    from services.recommendation_center import compare_rule_snapshot_fast_path as compare

    return compare(source_issue, target_issue)


@router.get("/health")
def api_rule_snapshot_health(limit: int = 100):
    safe_limit = _safe_limit(limit)
    try:
        return build_rule_snapshot_health(limit=safe_limit)
    except Exception as exc:
        logger.exception("rule snapshot health API failed")
        return {
            "status": "error",
            "error": str(exc),
            "limit": safe_limit,
            "warnings": ["rule_snapshot_health_failed"],
        }


@router.get("/audit")
def api_rule_snapshot_audit(limit: int = 20):
    safe_limit = _safe_audit_limit(limit)
    try:
        return audit_rule_snapshot_fast_path(limit=safe_limit)
    except Exception as exc:
        logger.exception("rule snapshot audit API failed")
        return {
            "status": "error",
            "reason": "rule_snapshot_audit_failed",
            "limit": safe_limit,
            "total_compared": 0,
            "snapshot_used_count": 0,
            "fallback_used_count": 0,
            "snapshot_missing_count": 0,
            "incomplete_snapshot_count": 0,
            "average_overlap_count": 0,
            "average_overlap_rate": 0,
            "max_difference_issues": [],
            "items": [],
            "warnings": ["rule_snapshot_audit_failed"],
            "error": str(exc),
        }


@router.get("/compare")
def api_rule_snapshot_compare(source_issue: str | None = None, target_issue: str | None = None):
    resolved_source_issue = str(source_issue or "").strip()
    resolved_target_issue = str(target_issue).strip() if target_issue not in (None, "") else None
    if not resolved_source_issue:
        return {
            "status": "error",
            "reason": "missing_source_issue",
            "source_issue": None,
            "target_issue": resolved_target_issue,
            "snapshot_used": False,
            "fallback_used": False,
        }
    try:
        return compare_rule_snapshot_fast_path(resolved_source_issue, resolved_target_issue)
    except Exception as exc:
        logger.exception("rule snapshot compare API failed")
        return {
            "status": "error",
            "reason": "rule_snapshot_compare_failed",
            "source_issue": resolved_source_issue,
            "target_issue": resolved_target_issue,
            "snapshot_used": False,
            "fallback_used": False,
            "error": str(exc),
        }


@router.post("/generate")
def api_rule_snapshot_generate(payload: GenerateRuleSnapshotRequest):
    source_issue = str(payload.source_issue or "").strip()
    target_issue = str(payload.target_issue).strip() if payload.target_issue not in (None, "") else None
    if not source_issue:
        return {
            "status": "error",
            "reason": "missing_source_issue",
            "source_issue": None,
            "target_issue": target_issue,
            "persisted": False,
        }
    try:
        return generate_rule_snapshot_for_issue(
            source_issue,
            target_issue=target_issue,
            persist=payload.persist,
        )
    except Exception as exc:
        logger.exception("rule snapshot generate API failed")
        return {
            "status": "error",
            "reason": "rule_snapshot_generate_failed",
            "source_issue": source_issue,
            "target_issue": target_issue,
            "persist": payload.persist,
            "persisted": False,
            "error": str(exc),
        }
