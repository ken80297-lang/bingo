from __future__ import annotations

from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.ui.widgets.metric_card import MetricCard


class LearningPage(ttk.Frame):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        self.grid_columnconfigure(0, weight=1)
        self.refresh()

    def refresh(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        summary = self.repository.get_learning_summary(limit=100)
        counts = summary.get("counts") or {}
        ttk.Label(self, text="AI \u5b78\u7fd2\uff08\u552f\u8b80\uff09", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        cards = ttk.Frame(self)
        cards.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for idx, (title, key) in enumerate(
            [("\u7e3d\u7b46\u6578", "total_records"), ("\u5df2\u5b78\u7fd2", "learned_records"), ("\u5f85\u8655\u7406", "pending_records"), ("\u6a21\u578b\u6578", "model_count")]
        ):
            cards.grid_columnconfigure(idx, weight=1)
            MetricCard(cards, title, str(counts.get(key, 0))).grid(row=0, column=idx, sticky="ew", padx=4)
        columns = ("model", "version", "sample", "avg_hits", "rank")
        tree = ttk.Treeview(self, columns=columns, show="headings", height=16)
        headings = {"model": "\u6a21\u578b", "version": "\u7248\u672c", "sample": "\u6a23\u672c", "avg_hits": "\u5e73\u5747\u547d\u4e2d", "rank": "\u6392\u540d\u5206\u6578"}
        for column in columns:
            tree.heading(column, text=headings[column])
        tree.grid(row=2, column=0, sticky="nsew", pady=(14, 0))
        for item in summary.get("performance") or []:
            tree.insert(
                "",
                "end",
                values=(item.get("model_name"), item.get("model_version"), item.get("sample_size"), item.get("average_hits"), item.get("rank_score")),
            )

