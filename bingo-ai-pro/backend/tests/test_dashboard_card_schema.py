from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.dashboard_card_schema import (
    high_probability_numbers,
    odd_even_prediction,
    size_prediction,
    validation_diagnostics,
)
from services import next_prediction_center
from services import player_dashboard


def test_dashboard_card_schema_builds_high_probability_subset():
    record = {
        "confidence": 92,
        "recommend_numbers": list(range(1, 21)),
        "model_scores": {
            "voting": {"confidence": 95, "candidate_numbers": [18, 3, 7, 11, 12]},
            "missing": {"confidence": 80, "candidate_numbers": [20, 18, 14, 9, 5]},
        },
    }
    result = high_probability_numbers(record, record["recommend_numbers"])

    assert len(result["numbers"]) == 5
    assert set(result["numbers"]).issubset(set(record["recommend_numbers"]))
    assert result["details"][0]["number"] in result["numbers"]
    assert result["fallback_used"] is True


def test_dashboard_card_schema_validates_distribution_counts():
    numbers = list(range(1, 21))
    size = size_prediction(numbers, {"confidence": 0.72})
    odd_even = odd_even_prediction(numbers, {"confidence": 68})
    diagnostics = validation_diagnostics(
        {
            "current_draw": {"numbers": list(range(21, 41))},
            "recommend_numbers": numbers,
            "high_probability_numbers": [1, 2, 3, 4, 5],
            "super_candidates": [1, 2, 3],
            "size_prediction": size,
            "odd_even_prediction": odd_even,
            "confidence": 0.92,
        }
    )

    assert size["small_count"] + size["large_count"] == 20
    assert odd_even["odd_count"] + odd_even["even_count"] == 20
    assert size["confidence"] == 0.72
    assert odd_even["confidence_percent"] == 68
    assert diagnostics["valid"] is True


def test_player_dashboard_enriches_card_v1_fields():
    next_prediction = {
        "prediction_issue": "115040902",
        "based_on_issue": "115040901",
        "recommend_numbers": list(range(1, 21)),
        "confidence": 88,
        "super_number": 7,
        "model_scores": {
            "voting": {"confidence": 90, "candidate_numbers": [7, 8, 9, 10, 11]},
        },
    }

    enriched = player_dashboard._enrich_dashboard_card_v1(
        next_prediction,
        {"issue": "115040901", "numbers": list(range(21, 41))},
    )

    assert enriched["confidence"] == 0.88
    assert enriched["confidence_percent"] == 88
    assert len(enriched["high_probability_numbers"]) == 5
    assert enriched["numbers"] == list(range(1, 21))
    assert enriched["top_five"] == enriched["high_probability_numbers"]
    assert enriched["super_candidate"] == 7
    assert set(enriched["high_probability_numbers"]).issubset(set(enriched["recommend_numbers"]))
    assert enriched["size_prediction"]["small_count"] + enriched["size_prediction"]["large_count"] == 20
    assert enriched["odd_even_prediction"]["odd_count"] + enriched["odd_even_prediction"]["even_count"] == 20
    assert enriched["diagnostics"]["dashboard_card_v1"]["valid"] is True


def test_player_dashboard_does_not_promote_super_candidate_outside_prediction_numbers():
    enriched = player_dashboard._enrich_dashboard_card_v1(
        {
            "prediction_issue": "115040902",
            "based_on_issue": "115040901",
            "recommend_numbers": list(range(1, 21)),
            "confidence": 88,
            "super_number": 80,
        },
        {"issue": "115040901", "numbers": list(range(21, 41))},
    )

    assert enriched["super_candidates"] == []
    assert enriched["super_candidate"] is None


def test_dashboard_card1_schema_sorts_numbers_and_keeps_official_super_inside_20():
    next_prediction = player_dashboard._enrich_dashboard_card_v1(
        {
            "prediction_issue": "115040902",
            "based_on_issue": "115040901",
            "recommend_numbers": [20, 3, 9, 3, 1, 80, 42, 7, 6, 5, 4, 2, 8, 10, 11, 12, 13, 14, 15, 16, 17],
            "confidence": 67,
            "super_number": 80,
            "high_probability_numbers": [42, 20, 9, 7, 3],
        },
        {"issue": "115040901", "numbers": list(range(1, 21))},
    )
    card = player_dashboard._card_one_payload(
        current_draw=None,
        latest_official_draw={
            "issue": "115040901",
            "draw_time": "2026-07-30T12:00:00+08:00",
            "numbers": [20, 5, 1, 7, 3, 2, 4, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
            "super_number": 7,
            "verification_status": "verified",
        },
        next_prediction=next_prediction,
        previous_verification=None,
        rule_library={"rules": []},
    )

    assert card["title"] == "🎯 最新開獎與 AI 推薦"
    assert card["official_numbers"] == list(range(1, 21))
    assert card["official_super_number"] == 7
    assert card["official_super_number"] in card["official_numbers"]
    assert card["prediction_numbers"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 20, 42, 80]
    assert len(card["prediction_numbers"]) == 20
    assert len(set(card["prediction_numbers"])) == 20
    assert all(1 <= number <= 80 for number in card["prediction_numbers"])
    assert card["super_candidates"] == []


def test_next_prediction_card_fields_accept_confidence_percent():
    prediction = {
        "recommend_numbers": list(range(1, 21)),
        "confidence_percent": 77,
        "super_number_candidates": [3, 9, 18],
    }

    fields = next_prediction_center._dashboard_card_fields(prediction, prediction["recommend_numbers"])

    assert fields["confidence"] == 0.77
    assert fields["confidence_percent"] == 77
    assert fields["super_candidates"] == [3, 9, 18]
    assert fields["diagnostics"]["dashboard_card_v1"]["valid"] is True
