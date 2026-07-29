from __future__ import annotations

from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.replay_engine import ReplayEngine
from desktop.ui.widgets.empty_state import EmptyState
from desktop.ui.widgets.number_grid import NumberGrid


class ReplayPage(ttk.Frame):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.engine = ReplayEngine(repository)
        self.grid_columnconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew")
        for label, command in [
            ("\u7b2c\u4e00\u7b46", self.first),
            ("\u4e0a\u4e00\u7b46", self.previous),
            ("\u4e0b\u4e00\u7b46", self.next),
            ("\u6700\u5f8c\u4e00\u7b46", self.last),
            ("\u91cd\u65b0\u8f09\u5165", self.reload),
        ]:
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=3)
        self.status = ttk.Label(toolbar, text="0 / 0")
        self.status.pack(side="right")
        self.content = ttk.Frame(self)
        self.content.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.reload()

    def reload(self) -> None:
        self.engine.load(limit=100)
        self.render()

    def render(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        record = self.engine.current()
        if not record or not record.prediction:
            self.status.configure(text="0 / 0")
            EmptyState(self.content, "\u6c92\u6709\u56de\u653e\u8cc7\u6599", "\u627e\u4e0d\u5230\u53ef\u914d\u5c0d target \u958b\u734e\u7684\u6b63\u5f0f prediction\u3002").pack(fill="x")
            return
        self.status.configure(text=f"{record.index} / {record.total}")
        header = ttk.Frame(self.content)
        header.pack(fill="x")
        ttk.Label(header, text=f"\u4f86\u6e90\u671f {record.prediction.source_issue} -> \u76ee\u6a19\u671f {record.prediction.target_issue}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=f"\u547d\u4e2d {len(record.hit_numbers)}/20  \u547d\u4e2d\u7387 {record.hit_rate:.0%}", style="Muted.TLabel").pack(anchor="w")
        prediction_grid = NumberGrid(self.content)
        prediction_grid.pack(anchor="w", pady=(12, 8))
        prediction_grid.set_numbers(record.prediction.numbers, hits=record.hit_numbers, super_number=record.prediction.super_number)
        if record.target_draw:
            ttk.Label(self.content, text="\u76ee\u6a19\u671f\u5be6\u969b\u958b\u734e", style="Title.TLabel").pack(anchor="w", pady=(8, 4))
            actual_grid = NumberGrid(self.content)
            actual_grid.pack(anchor="w")
            actual_grid.set_numbers(record.target_draw.numbers, hits=record.hit_numbers, super_number=record.target_draw.super_number)
        if record.rule_snapshot:
            snapshot = record.rule_snapshot.get("snapshot_json") or {}
            ttk.Label(self.content, text=f"Rule Snapshot {snapshot.get('rule_library_version') or record.rule_snapshot.get('rule_library_version')}", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    def first(self) -> None:
        self.engine.first()
        self.render()

    def previous(self) -> None:
        self.engine.previous()
        self.render()

    def next(self) -> None:
        self.engine.next()
        self.render()

    def last(self) -> None:
        self.engine.last()
        self.render()

