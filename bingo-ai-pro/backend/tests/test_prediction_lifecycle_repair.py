from __future__ import annotations

import pathlib
import sqlite3
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


def test_live_verification_updates_by_target_issue_without_history_selector(monkeypatch, tmp_path):
    db_path = tmp_path / "live_verify.db"

    def connect():
        return sqlite3.connect(db_path)

    with connect() as conn:
        conn.executescript(
            """
            create table prediction_history (
                id integer primary key,
                issue text,
                prediction_issue text,
                predict_time text,
                strategy text,
                confidence real,
                recommend_numbers text,
                super_number integer,
                three_star text,
                four_star text,
                twins text,
                consecutive text,
                patch_numbers text,
                tails text,
                big_small text,
                odd_even text,
                reasons text,
                winning_numbers text,
                hit_count integer,
                super_hit integer,
                three_star_hit integer,
                four_star_hit integer,
                accuracy real,
                created_at text,
                updated_at text,
                model_scores text,
                winning_model text,
                prediction_status text,
                verified_issue text,
                verified_at text,
                matched_numbers text,
                missed_numbers text,
                prediction_count integer,
                hit_rate real,
                super_number_hit integer,
                verification_version text,
                learning_used integer,
                model_score real
            );
            """
        )
        conn.execute(
            """
            insert into prediction_history (
                id, issue, prediction_issue, predict_time, strategy, confidence,
                recommend_numbers, super_number, three_star, four_star, twins,
                consecutive, patch_numbers, tails, big_small, odd_even, reasons,
                winning_numbers, hit_count, super_hit, three_star_hit, four_star_hit,
                accuracy, created_at, updated_at, model_scores, winning_model,
                prediction_status, verified_issue, verified_at, matched_numbers,
                missed_numbers, prediction_count, hit_rate, super_number_hit,
                verification_version, learning_used, model_score
            ) values (
                1, '115000100', '115000101', '2026-07-17T00:00:00', 'V7', 88,
                '[1,2,3,4,5]', 7, '[1,2,3]', '[1,2,3,4]', '[]',
                '[]', '[]', '[]', 'balanced', 'balanced', '[]',
                '[]', 0, 0, 0, 0,
                0, '2026-07-17T00:00:00', '2026-07-17T00:00:00', '{}', null,
                'waiting_draw', null, null, '[]',
                '[]', 5, 0, 0,
                null, 0, 0
            )
            """
        )

    monkeypatch.setattr(prediction_history_store, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(prediction_history_store, "_cloud_enabled", lambda: False)
    monkeypatch.setattr(prediction_history_store, "_sqlite_connection", connect)
    monkeypatch.setattr(prediction_history_store, "get_prediction_history_records", lambda limit: [])

    result = prediction_history_store.update_prediction_history_result(
        {"issue": "115000101", "numbers": list(range(1, 21)), "super_number": 7}
    )

    assert result["updated"] == 1
    assert result["prediction_status"] == "verified"
    with connect() as conn:
        row = conn.execute(
            "select prediction_status, verified_issue, winning_numbers, matched_numbers, missed_numbers, hit_count from prediction_history where id = 1"
        ).fetchone()
    assert row[0] == "verified"
    assert row[1] == "115000101"
    assert row[5] == 5
    assert row[2] != "[]"
    assert row[3] != "[]"
