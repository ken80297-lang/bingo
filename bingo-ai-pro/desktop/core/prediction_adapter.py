from __future__ import annotations

from desktop.core.data_repository import DataRepository
from desktop.models import PredictionRecord


class PredictionAdapter:
    def __init__(self, repository: DataRepository | None = None) -> None:
        self.repository = repository or DataRepository()

    def latest(self) -> PredictionRecord | None:
        return PredictionRecord.from_dict(self.repository.get_latest_prediction())

    def for_target_issue(self, issue: str) -> PredictionRecord | None:
        return PredictionRecord.from_dict(self.repository.get_prediction_for_issue(issue))

