from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from desktop.core.backend_path import ensure_backend_path
from desktop.core.readonly_guard import ReadonlyGuard, install_readonly_guard
from desktop.core.validators import is_production_issue, validate_draw, validate_prediction

logger = logging.getLogger(__name__)


@dataclass
class DataRepository:
    guard: ReadonlyGuard = field(default_factory=install_readonly_guard)

    def __post_init__(self) -> None:
        ensure_backend_path()

    def get_latest_draw(self) -> dict | None:
        from database.official_draw_store import get_latest_official_draw

        latest = self._valid_draw_or_none(self._safe_call(get_latest_official_draw))
        if latest:
            return latest
        history = self.get_draw_history(limit=200)
        return history[0] if history else None

    def get_draw_history(self, limit: int = 200, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
        from database.official_draw_store import get_official_draw_history

        rows = self._safe_call(get_official_draw_history, limit) or []
        output = [item for item in rows if self._valid_draw_or_none(item)]
        if start_date:
            output = [item for item in output if str(item.get("draw_date") or item.get("draw_time") or "") >= start_date]
        if end_date:
            output = [item for item in output if str(item.get("draw_date") or item.get("draw_time") or "") <= end_date]
        return output

    def get_draw_by_issue(self, issue: str) -> dict | None:
        if not is_production_issue(issue):
            return None
        from database.official_draw_store import get_official_draw_by_issue

        return self._valid_draw_or_none(self._safe_call(get_official_draw_by_issue, str(issue)))

    def get_latest_prediction(self) -> dict | None:
        from database.prediction_history_store import get_latest_prediction_history

        latest = self._valid_prediction_or_none(self._safe_call(get_latest_prediction_history))
        if latest:
            return latest
        history = self.get_prediction_history(limit=200)
        return history[0] if history else None

    def get_prediction_for_issue(self, issue: str) -> dict | None:
        if not is_production_issue(issue):
            return None
        source = str(int(str(issue)) - 1)
        from database.prediction_history_store import get_prediction_for_source_target

        return self._valid_prediction_or_none(self._safe_call(get_prediction_for_source_target, source, str(issue)))

    def get_prediction_for_source_target(self, source_issue: str, target_issue: str) -> dict | None:
        if not is_production_issue(source_issue) or not is_production_issue(target_issue):
            return None
        from database.prediction_history_store import get_prediction_for_source_target

        return self._valid_prediction_or_none(
            self._safe_call(get_prediction_for_source_target, str(source_issue), str(target_issue))
        )

    def get_prediction_history(self, limit: int = 100) -> list[dict]:
        from database.prediction_history_store import get_prediction_history_records

        return [item for item in self._safe_call(get_prediction_history_records, limit) or [] if self._valid_prediction_or_none(item)]

    def get_latest_analysis_history(self) -> dict | None:
        from database.analysis_store import get_latest_analysis_history

        return self._safe_call(get_latest_analysis_history)

    def get_analysis_for_issue(self, issue: str) -> dict | None:
        if not is_production_issue(issue):
            return None
        from database.analysis_store import get_analysis_history_by_issue

        return self._safe_call(get_analysis_history_by_issue, str(issue))

    def get_rule_snapshots_for_issue(self, issue: str) -> list[dict]:
        if not is_production_issue(issue):
            return []
        target_issue = str(int(str(issue)) + 1)
        try:
            from database.rule_snapshot_store import get_rule_snapshot, get_rule_snapshots
        except Exception:
            logger.warning("rule snapshot store unavailable; returning empty desktop snapshot list")
            return []

        exact = self._safe_call(get_rule_snapshot, source_issue=str(issue), target_issue=target_issue)
        if exact:
            return [exact]
        rows = self._safe_call(get_rule_snapshots, 100) or []
        return [item for item in rows if str(item.get("source_issue")) == str(issue)]

    def get_latest_rule_snapshot(self) -> dict | None:
        try:
            from database.rule_snapshot_store import get_latest_rule_snapshot
        except Exception:
            logger.warning("rule snapshot store unavailable; returning no desktop snapshot")
            return None

        return self._safe_call(get_latest_rule_snapshot)

    def get_rule_registry(self) -> list[dict]:
        try:
            from services.rule_snapshot import get_rule_registry

            return get_rule_registry()
        except Exception:
            logger.exception("rule registry read failed")
            return []

    def get_learning_summary(self, limit: int = 100) -> dict:
        from database.learning_store import get_learning_model_performance, get_learning_records, get_learning_status_counts

        return {
            "counts": self._safe_call(get_learning_status_counts) or {},
            "performance": self._safe_call(get_learning_model_performance, window=limit) or [],
            "records": self._safe_call(get_learning_records, limit=limit) or [],
        }

    def get_prediction_statistics(self, limit: int = 100) -> dict:
        from database.prediction_history_store import get_prediction_history_statistics

        return self._safe_call(get_prediction_history_statistics, limit) or {}

    def get_recommendation_history(self, limit: int = 20) -> list[dict]:
        try:
            from database.recommendation_center_store import get_recommendation_history

            return self._safe_call(get_recommendation_history, limit) or []
        except Exception:
            logger.exception("recommendation history read failed")
            return []

    def health_check(self) -> dict:
        latest_draw = self.get_latest_draw()
        latest_prediction = self.get_latest_prediction()
        latest_analysis = self.get_latest_analysis_history()
        latest_rule = self.get_latest_rule_snapshot()
        learning = self.get_learning_summary(limit=20)
        return {
            "status": "ok",
            "read_only": self.guard.read_only,
            "backend_path": str(ensure_backend_path()),
            "latest_draw_issue": latest_draw.get("issue") if latest_draw else None,
            "latest_prediction_target": latest_prediction.get("prediction_issue") if latest_prediction else None,
            "analysis_available": bool(latest_analysis),
            "rule_snapshot_available": bool(latest_rule),
            "learning_records": (learning.get("counts") or {}).get("total_records", 0),
            "writes": {
                "database": self.guard.database_write_allowed(),
                "collector": self.guard.collector_allowed(),
                "learning": self.guard.learning_write_allowed(),
                "prediction": self.guard.prediction_write_allowed(),
            },
        }

    def block_database_write(self, operation: str, payload: Any | None = None) -> dict:
        return self.guard.block_write(operation, payload)

    def _safe_call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("desktop repository read failed: %s", getattr(func, "__name__", func))
            return None

    @staticmethod
    def _valid_draw_or_none(item: dict | None) -> dict | None:
        valid, _ = validate_draw(item)
        return item if valid else None

    @staticmethod
    def _valid_prediction_or_none(item: dict | None) -> dict | None:
        valid, _ = validate_prediction(item)
        return item if valid else None
