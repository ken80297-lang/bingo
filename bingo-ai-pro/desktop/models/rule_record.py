from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuleRecord:
    name: str
    rule_key: str
    score: float = 0
    confidence: float = 0
    status: str | None = None
    summary: str | None = None
    generated_at: str | None = None
    source_version: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuleRecord | None":
        if not data:
            return None
        return cls(
            name=str(data.get("name") or data.get("title") or data.get("rule_key") or ""),
            rule_key=str(data.get("rule_key") or data.get("key") or ""),
            score=float(data.get("score") or 0),
            confidence=float(data.get("confidence") or 0),
            status=data.get("status"),
            summary=data.get("summary") or data.get("description"),
            generated_at=data.get("generated_at"),
            source_version=data.get("source_version") or data.get("rule_library_version"),
        )

