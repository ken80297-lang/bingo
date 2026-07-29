from __future__ import annotations

from dataclasses import dataclass, field

from desktop.core.data_repository import DataRepository
from desktop.models import DrawRecord, PredictionRecord, ReplayRecord


@dataclass
class ReplayEngine:
    repository: DataRepository = field(default_factory=DataRepository)
    records: list[ReplayRecord] = field(default_factory=list)
    index: int = 0

    def load(self, limit: int = 100) -> list[ReplayRecord]:
        predictions = self.repository.get_prediction_history(limit)
        loaded: list[ReplayRecord] = []
        for prediction_data in predictions:
            source_issue = str(prediction_data.get("issue") or "")
            target_issue = str(prediction_data.get("prediction_issue") or "")
            source_draw = self.repository.get_draw_by_issue(source_issue)
            target_draw = self.repository.get_draw_by_issue(target_issue)
            snapshot = None
            snapshots = self.repository.get_rule_snapshots_for_issue(source_issue)
            if snapshots:
                snapshot = snapshots[0]
            loaded.append(
                ReplayRecord(
                    source_draw=DrawRecord.from_dict(source_draw),
                    prediction=PredictionRecord.from_dict(prediction_data),
                    target_draw=DrawRecord.from_dict(target_draw),
                    rule_snapshot=snapshot,
                    index=len(loaded) + 1,
                    total=0,
                )
            )
        total = len(loaded)
        self.records = [
            ReplayRecord(item.source_draw, item.prediction, item.target_draw, item.rule_snapshot, item.index, total)
            for item in loaded
        ]
        self.index = 0
        return self.records

    def current(self) -> ReplayRecord | None:
        if not self.records:
            return None
        self.index = max(0, min(self.index, len(self.records) - 1))
        return self.records[self.index]

    def next(self) -> ReplayRecord | None:
        if self.records:
            self.index = min(len(self.records) - 1, self.index + 1)
        return self.current()

    def previous(self) -> ReplayRecord | None:
        if self.records:
            self.index = max(0, self.index - 1)
        return self.current()

    def first(self) -> ReplayRecord | None:
        self.index = 0
        return self.current()

    def last(self) -> ReplayRecord | None:
        if self.records:
            self.index = len(self.records) - 1
        return self.current()

    def seek(self, index: int) -> ReplayRecord | None:
        self.index = max(0, min(index, len(self.records) - 1))
        return self.current()

