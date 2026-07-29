from __future__ import annotations

from desktop.core.data_repository import DataRepository


class AnalysisAdapter:
    def __init__(self, repository: DataRepository | None = None) -> None:
        self.repository = repository or DataRepository()

    def latest(self) -> dict | None:
        return self.repository.get_latest_analysis_history()

    def for_issue(self, issue: str) -> dict | None:
        return self.repository.get_analysis_for_issue(issue)

