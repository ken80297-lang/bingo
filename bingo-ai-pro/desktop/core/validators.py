from __future__ import annotations

import json
from typing import Any


NON_PRODUCTION_MARKERS = ("test", "fixture", "synthetic", "preview", "simulation")


def normalize_numbers(values: Any, *, limit: int | None = None) -> list[int]:
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            values = parsed if isinstance(parsed, list) else [values]
        except Exception:
            values = values.replace("[", "").replace("]", "").split(",")
    numbers: list[int] = []
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 80 and number not in numbers:
            numbers.append(number)
    numbers.sort()
    return numbers[:limit] if limit else numbers


def is_production_issue(issue: Any) -> bool:
    text = str(issue or "").strip()
    if not text.isdigit() or len(text) < 6:
        return False
    if text.startswith("99") or text.upper().startswith("TEST"):
        return False
    return True


def validate_draw(draw: dict | None) -> tuple[bool, str | None]:
    if not isinstance(draw, dict):
        return False, "missing_draw"
    if not is_production_issue(draw.get("issue")):
        return False, "invalid_issue"
    numbers = normalize_numbers(draw.get("numbers"))
    if len(numbers) != 20:
        return False, "numbers_must_have_20_unique_values"
    super_number = draw.get("super_number")
    if super_number not in (None, ""):
        try:
            if not 1 <= int(super_number) <= 80:
                return False, "invalid_super_number"
            if int(super_number) not in numbers:
                return False, "super_number_must_be_in_numbers"
        except (TypeError, ValueError):
            return False, "invalid_super_number"
    marker_text = " ".join(str(draw.get(key) or "") for key in ("source", "verification_status")).lower()
    if any(marker in marker_text for marker in NON_PRODUCTION_MARKERS):
        return False, "non_production_source"
    return True, None


def is_verified_official_draw(draw: dict | None) -> bool:
    valid, _ = validate_draw(draw)
    if not valid:
        return False
    status = str(draw.get("verification_status") or "").lower()
    return bool(draw.get("verified")) or status in {"validated", "verified", "official"}


def validate_prediction(prediction: dict | None) -> tuple[bool, str | None]:
    if not isinstance(prediction, dict):
        return False, "missing_prediction"
    source = prediction.get("issue") or prediction.get("source_issue") or prediction.get("based_on_issue")
    target = prediction.get("prediction_issue") or prediction.get("target_issue")
    if not is_production_issue(source):
        return False, "invalid_source_issue"
    if not is_production_issue(target):
        return False, "invalid_target_issue"
    try:
        if int(target) != int(source) + 1:
            return False, "target_must_follow_source"
    except (TypeError, ValueError):
        return False, "target_must_follow_source"
    if len(normalize_numbers(prediction.get("recommend_numbers") or prediction.get("main_numbers"))) != 20:
        return False, "recommendation_must_have_20_unique_values"
    marker_text = " ".join(str(prediction.get(key) or "") for key in ("strategy", "source", "trigger")).lower()
    if any(marker in marker_text for marker in NON_PRODUCTION_MARKERS):
        return False, "non_production_prediction"
    return True, None


def analysis_status(analysis: dict | None) -> str:
    if not isinstance(analysis, dict):
        return "missing"
    if analysis.get("cluster_level") is not None and analysis.get("updated_at"):
        return "finalized"
    return "provisional"
