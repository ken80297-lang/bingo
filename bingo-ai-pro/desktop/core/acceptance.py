from __future__ import annotations

from typing import Any

from desktop.core.data_repository import DataRepository
from desktop.core.rule_order import FIXED_RULE_ORDER, RULE_NAME_ZH
from desktop.core.validators import analysis_status, normalize_numbers, validate_draw, validate_prediction


def collect_live_read_acceptance(repository: DataRepository | None = None) -> dict[str, Any]:
    repo = repository or DataRepository()
    latest_draw = repo.get_latest_draw()
    draw_valid, draw_reason = validate_draw(latest_draw)
    history = repo.get_draw_history(limit=50)
    latest_prediction = repo.get_latest_prediction()
    prediction_valid, prediction_reason = validate_prediction(latest_prediction)
    analysis = repo.get_analysis_for_issue(latest_draw["issue"]) if latest_draw else None
    latest_rule = repo.get_latest_rule_snapshot()
    learning = repo.get_learning_summary(limit=100)
    return {
        "official": {
            "issue": latest_draw.get("issue") if latest_draw else None,
            "draw_time": latest_draw.get("draw_time") if latest_draw else None,
            "numbers": latest_draw.get("numbers") if latest_draw else [],
            "super_number": latest_draw.get("super_number") if latest_draw else None,
            "verification_status": latest_draw.get("verification_status") if latest_draw else None,
            "valid": draw_valid,
            "invalid_reason": draw_reason,
        },
        "history": {
            "count": len(history),
            "descending": _issues_descending(history),
            "production_only": all(_production_history_item(item) for item in history),
        },
        "prediction": _prediction_payload(latest_prediction, prediction_valid, prediction_reason),
        "analysis": {
            "issue": analysis.get("issue") if analysis else None,
            "status": analysis_status(analysis),
            "available": bool(analysis),
            "snapshot_keys": sorted(analysis.keys()) if analysis else [],
        },
        "rule_snapshot": normalize_rule_snapshot(latest_rule),
        "learning": {
            "available": bool(learning),
            "counts": learning.get("counts") or {},
            "performance_count": len(learning.get("performance") or []),
            "records_count": len(learning.get("records") or []),
        },
    }


def normalize_rule_snapshot(record: dict | None) -> dict[str, Any]:
    if not record:
        return {"available": False, "rules": []}
    snapshot = record.get("snapshot_json") or {}
    rules = snapshot.get("rules") or []
    by_key = {str(item.get("rule_key") or item.get("key")): item for item in rules if isinstance(item, dict)}
    ordered = []
    for key, name_zh in FIXED_RULE_ORDER:
        item = by_key.get(key)
        if not item:
            continue
        ordered.append(
            {
                "rule_key": key,
                "rule_name_zh": name_zh,
                "source_issue": snapshot.get("source_issue") or record.get("source_issue"),
                "target_issue": snapshot.get("target_issue") or record.get("target_issue"),
                "candidates": normalize_numbers(item.get("candidates") or item.get("numbers")),
                "matched_numbers": normalize_numbers(item.get("matched_numbers")),
                "score": item.get("score"),
                "confidence": item.get("confidence"),
                "summary": item.get("summary"),
                "evidence": item.get("evidence"),
                "status": item.get("status"),
                "generated_at": snapshot.get("generated_at") or record.get("generated_at"),
                "source_version": snapshot.get("rule_library_version") or record.get("rule_library_version"),
            }
        )
    return {
        "available": True,
        "source_issue": record.get("source_issue"),
        "target_issue": record.get("target_issue"),
        "rules": ordered,
        "fixed_order": [name for _, name in FIXED_RULE_ORDER],
    }


def _prediction_payload(prediction: dict | None, valid: bool, reason: str | None) -> dict[str, Any]:
    if not prediction:
        return {"available": False, "valid": False, "invalid_reason": "missing_prediction"}
    high_probability = _first_five_unique(
        prediction.get("three_star"),
        prediction.get("four_star"),
        prediction.get("recommend_numbers"),
    )
    return {
        "available": True,
        "valid": valid,
        "invalid_reason": reason,
        "source_issue": prediction.get("issue") or prediction.get("source_issue"),
        "target_issue": prediction.get("prediction_issue") or prediction.get("target_issue"),
        "recommend_numbers": normalize_numbers(prediction.get("recommend_numbers")),
        "high_probability_five": high_probability,
        "odd_even": prediction.get("odd_even"),
        "big_small": prediction.get("big_small"),
        "super_candidate": prediction.get("super_number"),
        "prediction_snapshot_time": prediction.get("created_at") or prediction.get("predict_time"),
    }


def _first_five_unique(*groups: Any) -> list[int]:
    output: list[int] = []
    for group in groups:
        for number in normalize_numbers(group):
            if number not in output:
                output.append(number)
            if len(output) == 5:
                return output
    return output


def _issues_descending(history: list[dict]) -> bool:
    issues = [int(item["issue"]) for item in history if str(item.get("issue") or "").isdigit()]
    return issues == sorted(issues, reverse=True)


def _production_history_item(item: dict) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("issue", "source", "verification_status")).lower()
    return not any(marker in text for marker in ("test", "legacy", "pending", "fixture", "synthetic"))
