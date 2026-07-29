from __future__ import annotations

from desktop.core.data_repository import DataRepository


class RuleAdapter:
    def __init__(self, repository: DataRepository | None = None) -> None:
        self.repository = repository or DataRepository()

    def registry(self) -> list[dict]:
        return self.repository.get_rule_registry()

    def snapshots_for_issue(self, issue: str) -> list[dict]:
        return self.repository.get_rule_snapshots_for_issue(issue)

