from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import official_verification


def _draw(issue: str) -> dict:
    return {
        "issue": issue,
        "numbers": list(range(1, 21)),
        "super_number": 7,
    }


def test_verification_candidates_include_pending_prediction_targets(monkeypatch):
    recent = [_draw("115000110")]
    pending_target = _draw("115000101")

    monkeypatch.setattr(official_verification, "get_official_draw_history", lambda limit: recent)
    monkeypatch.setattr(
        official_verification,
        "_pending_prediction_official_draws",
        lambda limit=50: [pending_target],
    )

    candidates = official_verification._verification_candidates(10)

    assert [item["issue"] for item in candidates] == ["115000110", "115000101"]


def test_pending_prediction_official_draws_skip_verified_and_missing_official(monkeypatch):
    predictions = [
        {"prediction_issue": "115000101", "prediction_status": "waiting_draw", "verified_at": None},
        {"prediction_issue": "115000102", "prediction_status": "verified", "verified_at": "2026-07-17T00:00:00"},
        {"prediction_issue": "115000103", "prediction_status": "waiting_draw", "verified_at": None},
    ]

    def fake_get_prediction_history_records(limit):
        assert limit == 500
        return predictions

    def fake_get_official_draw_by_issue(issue):
        if issue == "115000101":
            return _draw(issue)
        return None

    monkeypatch.setattr(
        "database.prediction_history_store.get_prediction_history_records",
        fake_get_prediction_history_records,
    )
    monkeypatch.setattr(official_verification, "get_official_draw_by_issue", fake_get_official_draw_by_issue)

    draws = official_verification._pending_prediction_official_draws()

    assert [item["issue"] for item in draws] == ["115000101"]


def test_pending_prediction_official_draws_includes_incomplete_verified(monkeypatch):
    predictions = [
        {
            "prediction_issue": "115000104",
            "prediction_status": "verified",
            "verified_at": "2026-07-17T00:00:00",
            "winning_numbers": [],
            "matched_numbers": [],
            "missed_numbers": [],
        },
    ]

    monkeypatch.setattr(
        "database.prediction_history_store.get_prediction_history_records",
        lambda limit: predictions,
    )
    monkeypatch.setattr(official_verification, "get_official_draw_by_issue", lambda issue: _draw(issue))

    draws = official_verification._pending_prediction_official_draws()

    assert [item["issue"] for item in draws] == ["115000104"]
