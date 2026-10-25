"""Pure verification for external-AI shadow predictions.

This module never alters production prediction history or model weights.
"""
from __future__ import annotations


def verify_shadow_prediction(prediction: dict, draw: dict) -> dict:
    """Compare a previously saved prediction against its target draw.

    Raises ValueError on malformed or mismatched records. The caller must
    load a durable prediction saved before the draw and enforce that ordering.
    """
    target = str(prediction.get("prediction_issue", ""))
    actual_issue = str(draw.get("issue", ""))
    if not target.isdigit() or target != actual_issue:
        raise ValueError("prediction_target_mismatch")
    predicted = prediction.get("numbers")
    top5 = prediction.get("top5")
    winning = draw.get("numbers")
    super_guess = prediction.get("super_number")
    actual_super = draw.get("super_number")
    def valid_numbers(values, length):
        return (
            isinstance(values, list) and len(values) == length
            and all(type(x) is int and 1 <= x <= 80 for x in values)
            and len(set(values)) == length
        )
    if not (valid_numbers(predicted, 20) and valid_numbers(top5, 5)
            and valid_numbers(winning, 20) and set(top5).issubset(predicted)
            and type(super_guess) is int and 1 <= super_guess <= 80
            and type(actual_super) is int and actual_super in winning):
        raise ValueError("invalid_prediction_or_draw")
    matches = sorted(set(predicted) & set(winning))
    return {
        "prediction_issue": target,
        "hit_count": len(matches),
        "matched_numbers": matches,
        "top5_hit_count": len(set(top5) & set(winning)),
        "super_hit": super_guess == actual_super,
        "actual_super_number": actual_super,
        "actual_numbers": sorted(winning),
    }
