from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from desktop.core.phase2_backtest import run_phase2_backtest


@dataclass
class WorkerEvent:
    name: str
    payload: dict[str, Any]


class ReplayWorker:
    def __init__(self, csv_path: str, min_history: int = 100, recent_limit: int | None = None) -> None:
        self.csv_path = csv_path
        self.min_history = min_history
        self.recent_limit = recent_limit
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._callbacks: list[Callable[[WorkerEvent], None]] = []
        self.result: dict[str, Any] | None = None

    def on_event(self, callback: Callable[[WorkerEvent], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _emit(self, name: str, **payload: Any) -> None:
        event = WorkerEvent(name, payload)
        for callback in list(self._callbacks):
            callback(event)

    def _run(self) -> None:
        started_at = time.monotonic()
        try:
            self._emit("started", stage="準備資料", processed=0, total=0, percent=0, current_issue="", elapsed=0)
            for percent, stage in [(8, "載入 CSV"), (18, "建立歷史視窗"), (30, "執行 Replay")]:
                if self._cancelled.is_set():
                    self._emit("cancelled", message="模擬已停止")
                    return
                self._emit("stage_changed", stage=stage, processed=0, total=0, percent=percent, current_issue="", elapsed=time.monotonic() - started_at)
                time.sleep(0.02)
            if self._cancelled.is_set():
                self._emit("cancelled", message="模擬已停止")
                return
            report = run_phase2_backtest(self.csv_path, min_history=self.min_history)
            if self.recent_limit:
                report = _slice_recent_report(report, self.recent_limit)
            if self._cancelled.is_set():
                self._emit("cancelled", message="模擬已停止")
                return
            self.result = report
            self._emit(
                "finished",
                stage="完成",
                processed=report.get("valid_simulations", 0),
                total=report.get("valid_simulations", 0),
                percent=100,
                current_issue=(report.get("simulations") or [{}])[-1].get("target_issue", ""),
                elapsed=time.monotonic() - started_at,
                report=report,
            )
        except Exception as exc:
            self._emit("failed", message=str(exc), elapsed=time.monotonic() - started_at)


def _slice_recent_report(report: dict[str, Any], recent_limit: int) -> dict[str, Any]:
    simulations = report.get("simulations") or []
    if len(simulations) <= recent_limit:
        return report
    sliced = dict(report)
    recent = simulations[-recent_limit:]
    hits = [item["hits_20"] for item in recent]
    high5 = [item["hits_high5"] for item in recent]
    sliced["simulations"] = recent
    sliced["valid_simulations"] = len(recent)
    sliced["average_hits"] = round(sum(hits) / len(hits), 4) if hits else 0
    sliced["average_high5_hits"] = round(sum(high5) / len(high5), 4) if high5 else 0
    sliced["max_hits"] = max(hits) if hits else 0
    sliced["min_hits"] = min(hits) if hits else 0
    sliced["super_hit_rate"] = round(sum(1 for item in recent if item["super_hit"]) / len(recent), 4) if recent else 0
    sliced["big_small_hit_rate"] = round(sum(1 for item in recent if item["big_small_hit"]) / len(recent), 4) if recent else 0
    sliced["odd_even_hit_rate"] = round(sum(1 for item in recent if item["odd_even_hit"]) / len(recent), 4) if recent else 0
    return sliced
