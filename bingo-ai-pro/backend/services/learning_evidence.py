from __future__ import annotations

from database.adaptive_weight_store import get_adaptive_weight_history
from database.learning_store import get_learning_records
from database.prediction_history_store import get_prediction_history_records

WEIGHT_FIELDS = {
    "laowanjia": "laowanjia_weight",
    "hotcold": "hot_cold_weight",
    "missing": "missing_weight",
    "pattern": "pattern_weight",
    "balance": "balance_weight",
}
EXPECTED_PAIRS = {
    (model, top_n)
    for model in ("laowanjia", "hotcold", "missing", "pattern", "balance", "ensemble")
    for top_n in (5, 10, 20)
}


def _weights(row: dict | None) -> dict[str, float | None]:
    row = row or {}
    return {
        name: (round(float(row[field]), 6) if row.get(field) is not None else None)
        for name, field in WEIGHT_FIELDS.items()
    }


def _delta(before: dict, after: dict) -> dict:
    output = {}
    for key in WEIGHT_FIELDS:
        old = before.get(key)
        new = after.get(key)
        output[key] = round(new - old, 6) if old is not None and new is not None else None
    return output


def get_learning_evidence(limit: int = 20) -> dict:
    """Return durable evidence that learning changed prediction weights.

    This endpoint never infers learning from UI text. A learning event is proven
    only when its persisted weight row, complete 18/18 verified learning ledger,
    and verified prediction learning marker all agree.
    """
    history = [
        row for row in get_adaptive_weight_history(max(2, min(int(limit or 20) + 1, 101)))
        if row.get("strategy") == "v7_models"
    ]
    events = []
    for index, current in enumerate(history[: max(1, min(int(limit or 20), 100))]):
        issue = str(current.get("source_evaluation_id") or "")
        previous = history[index + 1] if index + 1 < len(history) else None
        before = _weights(previous)
        after = _weights(current)
        ledger = get_learning_records(
            limit=100,
            issue=issue,
            prediction_type="live_prediction",
            verification_status="verified",
            learned_status="learned",
        )
        pairs = {
            (str(row.get("model_name") or ""), int(row.get("top_n") or 0))
            for row in ledger
            if row.get("weight_changed")
        }
        complete_ledger = len(ledger) == 18 and pairs == EXPECTED_PAIRS
        predictions = [
            row for row in get_prediction_history_records(200)
            if str(row.get("prediction_issue") or "") == issue
            and row.get("prediction_status") == "verified"
        ]
        learning_used = any(bool(row.get("learning_used")) for row in predictions)
        changed = previous is not None and any(
            value not in (None, 0.0) for value in _delta(before, after).values()
        )
        proven = bool(complete_ledger and learning_used and previous is not None)
        events.append(
            {
                "issue": issue,
                "weight_id": current.get("id"),
                "version": current.get("version"),
                "created_at": current.get("created_at"),
                "window": current.get("window"),
                "average_hits": current.get("average_hits"),
                "before_weights": before,
                "after_weights": after,
                "weight_delta": _delta(before, after),
                "weights_actually_changed": changed,
                "learning_ledger_rows": len(ledger),
                "learning_ledger_complete": complete_ledger,
                "prediction_verified": bool(predictions),
                "learning_used": learning_used,
                "proven": proven,
                "proof_rule": "persisted weights + 18/18 verified learned rows with weight_changed + verified prediction learning_used",
            }
        )
    latest = events[0] if events else None
    return {
        "status": "ok",
        "evidence_version": "1.0",
        "latest_proven": bool(latest and latest.get("proven")),
        "latest_issue": latest.get("issue") if latest else None,
        "events": events,
    }
