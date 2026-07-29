from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import catch_up_service


def _draw(issue: str, numbers=None):
    return {
        "issue": issue,
        "draw_date": "2026-07-29",
        "draw_time": "2026-07-29T05:00:00+00:00",
        "numbers": numbers or list(range(1, 21)),
        "open_order_numbers": numbers or list(range(1, 21)),
        "super_number": 8,
        "source": "taiwan_lottery",
    }


def test_sequential_missing_draws_starts_at_first_gap_504():
    source_draws = [_draw(str(issue)) for issue in range(115042504, 115042516)]

    missing, pending, reason = catch_up_service._sequential_missing_draws(
        source_draws,
        database_number=115042503,
        source_number=115042515,
        known_database_issues={115042503},
        max_batch_size=3,
    )

    assert [draw["issue"] for draw in missing] == ["115042504", "115042505", "115042506"]
    assert pending == []
    assert reason == "batch_limit"


def test_sequential_missing_draws_does_not_require_target_equal_source_latest():
    source_draws = [_draw("115042504"), _draw("115042515")]

    missing, pending, reason = catch_up_service._sequential_missing_draws(
        source_draws,
        database_number=115042503,
        source_number=115042515,
        known_database_issues={115042503},
        max_batch_size=1,
    )

    assert [draw["issue"] for draw in missing] == ["115042504"]
    assert pending == []
    assert reason == "batch_limit"


def test_sequential_missing_draws_waits_when_target_is_newer_than_source():
    missing, pending, reason = catch_up_service._sequential_missing_draws(
        [_draw("115042503")],
        database_number=115042503,
        source_number=115042503,
        known_database_issues={115042503},
        max_batch_size=3,
    )

    assert missing == []
    assert pending == []
    assert reason == "no_gap"


def test_sequential_missing_draws_stops_at_invalid_first_gap():
    invalid = _draw("115042504", numbers=list(range(1, 20)))

    missing, pending, reason = catch_up_service._sequential_missing_draws(
        [invalid, _draw("115042505")],
        database_number=115042503,
        source_number=115042505,
        known_database_issues={115042503},
        max_batch_size=3,
    )

    assert missing == []
    assert pending == ["115042504"]
    assert reason == "invalid_or_incomplete_official_draw"


def test_sequential_missing_draws_simulates_three_local_rounds():
    known = {115042503}
    saved = []
    source_draws = [_draw(str(issue)) for issue in range(115042504, 115042507)]

    for latest in [115042503, 115042504, 115042505]:
        missing, pending, reason = catch_up_service._sequential_missing_draws(
            source_draws,
            database_number=latest,
            source_number=115042506,
            known_database_issues=known,
            max_batch_size=1,
        )
        assert pending == []
        assert reason in {"batch_limit", "completed"}
        saved.extend(draw["issue"] for draw in missing)
        known.update(int(draw["issue"]) for draw in missing)

    assert saved == ["115042504", "115042505", "115042506"]
