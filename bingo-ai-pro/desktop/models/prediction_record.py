from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from desktop.core.validators import normalize_numbers


@dataclass(frozen=True)
class PredictionRecord:
    source_issue: str
    target_issue: str
    numbers: list[int] = field(default_factory=list)
    super_number: int | None = None
    confidence: float = 0
    strategy: str | None = None
    hit_count: int = 0
    matched_numbers: list[int] = field(default_factory=list)
    status: str | None = None
    created_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PredictionRecord | None":
        if not data:
            return None
        super_number = data.get("super_number")
        return cls(
            source_issue=str(data.get("issue") or data.get("source_issue") or data.get("based_on_issue") or ""),
            target_issue=str(data.get("prediction_issue") or data.get("target_issue") or ""),
            numbers=normalize_numbers(data.get("recommend_numbers") or data.get("main_numbers")),
            super_number=int(super_number) if super_number not in (None, "") else None,
            confidence=float(data.get("confidence") or 0),
            strategy=data.get("strategy"),
            hit_count=int(data.get("hit_count") or 0),
            matched_numbers=normalize_numbers(data.get("matched_numbers")),
            status=data.get("prediction_status") or data.get("status"),
            created_at=data.get("created_at") or data.get("predict_time"),
        )

