from __future__ import annotations

from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import timeline_rows
from desktop.ui.simulator_widgets import ScrollPage, data_tree, title


class TimelinePage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        title(self.body, "預測時間軸", 0)
        self.filter_var = ttk.Combobox(self.body, values=["全部", "等待結果", "已驗證", "已排除", "事後重建"], state="readonly")
        self.filter_var.set("全部")
        self.filter_var.grid(row=1, column=0, sticky="w", pady=4)
        self.filter_var.bind("<<ComboboxSelected>>", lambda _: self.load())
        self.tree = data_tree(
            self.body,
            [
                ("target", "目標期號", 90),
                ("created", "Snapshot 建立時間", 155),
                ("top1", "Top 1", 60),
                ("top2", "Top 2", 80),
                ("top3", "Top 3", 100),
                ("snapshot", "Snapshot hash", 140),
                ("result", "結果時間", 120),
                ("h1", "Top 1 命中", 90),
                ("h2", "Top 2 命中數", 95),
                ("h3", "Top 3 命中數", 95),
                ("timing", "Timing", 80),
                ("eligible", "主要分析", 90),
                ("validation", "Validation hash", 140),
                ("status", "狀態", 80),
            ],
            2,
            16,
        )
        self.load()

    def load(self) -> None:
        self.tree.delete(*self.tree.get_children())
        selected = self.filter_var.get()
        for row in timeline_rows():
            if selected != "全部" and row["status"] != selected:
                continue
            self.tree.insert(
                "",
                "end",
                values=(
                    row["target_issue"],
                    row["generated_at"],
                    row["top1"],
                    row["top2"],
                    row["top3"],
                    row["snapshot_hash"][:16],
                    row["result_time"],
                    row["top1_hit"],
                    row["top2_hit"],
                    row["top3_hit"],
                    row["timing_valid"],
                    row["eligible"],
                    row["validation_hash"][:16],
                    row["status"],
                ),
            )
