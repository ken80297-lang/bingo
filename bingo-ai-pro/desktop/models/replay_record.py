from __future__ import annotations

from dataclasses import dataclass

from .draw_record import DrawRecord
from .prediction_record import PredictionRecord


@dataclass(frozen=True)
class ReplayRecord:
    source_draw: DrawRecord | None
    prediction: PredictionRecord | None
    target_draw: DrawRecord | None
    rule_snapshot: dict | None
    index: int
    total: int

    @property
    def hit_numbers(self) -> list[int]:
        if not self.prediction or not self.target_draw:
            return []
        actual = set(self.target_draw.numbers)
        return [number for number in self.prediction.numbers if number in actual]

    @property
    def miss_numbers(self) -> list[int]:
        if not self.prediction or not self.target_draw:
            return []
        actual = set(self.target_draw.numbers)
        return [number for number in self.prediction.numbers if number not in actual]

    @property
    def hit_rate(self) -> float:
        if not self.prediction:
            return 0
        return round(len(self.hit_numbers) / max(1, len(self.prediction.numbers)), 4)

