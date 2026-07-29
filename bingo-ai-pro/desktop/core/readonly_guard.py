from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


READ_ONLY_REASON = "desktop_read_only_mode"
BLOCKED_RESPONSE = {
    "status": "blocked",
    "reason": READ_ONLY_REASON,
    "message": "Desktop read-only mode blocks writes to backend data.",
}


@dataclass(frozen=True)
class ReadonlyGuard:
    read_only: bool = True
    allow_database_write: bool = False
    allow_live_collector: bool = False
    allow_learning_write: bool = False
    allow_prediction_write: bool = False

    @classmethod
    def from_env(cls) -> "ReadonlyGuard":
        return cls(
            read_only=_truthy(os.getenv("DESKTOP_READ_ONLY", "true")),
            allow_database_write=_truthy(os.getenv("DESKTOP_ALLOW_DATABASE_WRITE", "false")),
            allow_live_collector=_truthy(os.getenv("DESKTOP_ALLOW_LIVE_COLLECTOR", "false")),
            allow_learning_write=_truthy(os.getenv("DESKTOP_ALLOW_LEARNING_WRITE", "false")),
            allow_prediction_write=_truthy(os.getenv("DESKTOP_ALLOW_PREDICTION_WRITE", "false")),
        )

    def install(self) -> None:
        os.environ["DESKTOP_READ_ONLY"] = "true"
        os.environ["DESKTOP_ALLOW_DATABASE_WRITE"] = "false"
        os.environ["DESKTOP_ALLOW_LIVE_COLLECTOR"] = "false"
        os.environ["DESKTOP_ALLOW_LEARNING_WRITE"] = "false"
        os.environ["DESKTOP_ALLOW_PREDICTION_WRITE"] = "false"

    def block_write(self, operation: str, payload: Any | None = None) -> dict:
        response = dict(BLOCKED_RESPONSE)
        response["operation"] = operation
        if payload is not None:
            response["payload"] = payload
        return response

    def database_write_allowed(self) -> bool:
        return (not self.read_only) and self.allow_database_write

    def collector_allowed(self) -> bool:
        return (not self.read_only) and self.allow_live_collector

    def learning_write_allowed(self) -> bool:
        return (not self.read_only) and self.allow_learning_write

    def prediction_write_allowed(self) -> bool:
        return (not self.read_only) and self.allow_prediction_write


def install_readonly_guard() -> ReadonlyGuard:
    guard = ReadonlyGuard.from_env()
    guard.install()
    return ReadonlyGuard.from_env()


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

