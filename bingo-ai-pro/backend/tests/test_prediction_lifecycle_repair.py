from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import prediction_lifecycle_repair as repair
from database import prediction_history_store


def test_verification_payload_uses_twenty_number_denominator():
    payload = repair._verification_payload(
        {
            "target_issue": "114000101",
            "recommend_numbers": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "official_numbers": list(range(1, 21)),
            "super_number": 7,
            "official_super_number": 7,
        },
        "2026-07-16T00:00:00",
    )

    assert payload["hit_count"] == 10
    assert payload["prediction_count"] == 10
    assert payload["hit_rate"] == 1.0
    assert payload["super_number_hit"] is True
    assert len(payload["winning_numbers"]) == 20


def test_verification_recovery_dry_run_does_not_update(monkeypatch):
    rows = [
        {
            "id": 1,
            "target_issue": "114000101",
            "recommend_numbers": [1, 2, 3],
            "official_numbers": list(range(1, 21)),
            "super_number": 1,
            "official_super_number": 2,
        },
        {
            "id": 2,
            "target_issue": "114000102",
            "recommend_numbers": [1, 2, 3],
            "winning_numbers": list(range(1, 21)),
            "matched_numbers": [1, 2, 3],
            "missed_numbers": [],
            "prediction_status": "verified",
            "verified_at": "2026-07-16T00:00:00",
        },
        {"id": 3, "target_issue": None, "recommend_numbers": [1], "official_numbers": [1]},
        {"id": 4, "target_issue": "114000104", "recommend_numbers": [1]},
        {"id": 5, "target_issue": "114000105", "recommend_numbers": [1], "official_numbers": [1, 2]},
    ]
    monkeypatch.setattr(repair, "_prediction_rows", lambda: rows)

    def fail_update(*_args, **_kwargs):
        raise AssertionError("dry-run must not update prediction_history")

    monkeypatch.setattr(repair, "_update_verification", fail_update)

    summary = repair.verification_recovery(dry_run=True)

    assert summary["status"] == "dry_run"
    assert summary["scanned"] == 5
    assert summary["would_verify"] == 1
    assert summary["updated"] == 0
    assert summary["already_complete"] == 1
    assert summary["missing_target"] == 1
    assert summary["missing_official_draw"] == 1
    assert summary["data_format_error"] == 1


def test_learning_sync_dry_run_matches_learned_targets(monkeypatch):
    monkeypatch.setattr(repair, "_learned_targets", lambda: {"114000101", "114000102", "114000999"})
    monkeypatch.setattr(
        repair,
        "_query",
        lambda *_args, **_kwargs: [
            (1, "114000101", False),
            (2, "114000102", True),
        ],
    )

    def fail_execute(*_args, **_kwargs):
        raise AssertionError("dry-run must not update learning flags")

    monkeypatch.setattr(repair, "_execute", fail_execute)

    summary = repair.learning_sync(dry_run=True)

    assert summary["learned_distinct_target_count"] == 3
    assert summary["would_sync"] == 1
    assert summary["already_learning_used"] == 1
    assert summary["unmatched_learning_target"] == 1
    assert summary["updated"] == 0


def test_save_prediction_history_skips_unconfirmed_target(monkeypatch):
    events = []
    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_record_prediction_event", lambda **kwargs: events.append(kwargs))

    result = prediction_history_store.save_prediction_history(
        {
            "issue": "114000100",
            "prediction_issue": None,
            "recommend_numbers": [1, 2, 3],
            "strategy": "unit-test",
        },
        caller_context="prediction_service",
    )

    assert result == {
        "status": "skipped",
        "message": "prediction target is not confirmed",
        "skip_reason": "target_unconfirmed",
    }
    assert events[0]["event_type"] == "prediction_skipped"
    assert events[0]["prediction_skipped"] is True
