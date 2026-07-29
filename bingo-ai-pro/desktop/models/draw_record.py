from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from desktop.core.validators import normalize_numbers


@dataclass(frozen=True)
class DrawRecord:
    issue: str
    numbers: list[int] = field(default_factory=list)
    super_number: int | None = None
    draw_time: str | None = None
    verified: bool = False
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DrawRecord | None":
        if not data:
            return None
        super_number = data.get("super_number")
        return cls(
            issue=str(data.get("issue") or ""),
            numbers=normalize_numbers(data.get("numbers")),
            super_number=int(super_number) if super_number not in (None, "") else None,
            draw_time=data.get("draw_time") or data.get("draw_date"),
            verified=bool(data.get("verified")) or str(data.get("verification_status") or "").lower() in {"validated", "verified"},
            source=data.get("source"),
        )

