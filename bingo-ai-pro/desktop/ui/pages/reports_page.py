from __future__ import annotations

import csv
from pathlib import Path
from tkinter import messagebox, ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import export_current_summary, output_directories, timeline_rows
from desktop.ui.simulator_widgets import ScrollPage, data_tree, title


class ReportsPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        title(self.body, "報告與輸出", 0)
        self.tree = data_tree(self.body, [("name", "階段", 160), ("path", "輸出資料夾", 620)], 1, 6)
        for row in output_directories():
            self.tree.insert("", "end", values=(row["name"], row["path"]))
        frame = ttk.LabelFrame(self.body, text="操作", padding=8)
        frame.grid(row=2, column=0, sticky="ew", pady=8)
        ttk.Button(frame, text="開啟 TXT 報告", command=lambda: self._show("TXT 報告請見各輸出資料夾")).pack(side="left", padx=3)
        ttk.Button(frame, text="開啟 CSV", command=lambda: self._show("CSV 已列於各輸出資料夾")).pack(side="left", padx=3)
        ttk.Button(frame, text="開啟輸出資料夾", command=self.open_selected).pack(side="left", padx=3)
        ttk.Button(frame, text="匯出目前摘要 CSV/JSON/中文報告", command=self.export_summary).pack(side="left", padx=3)
        ttk.Button(frame, text="匯出 Timeline CSV", command=self.export_timeline).pack(side="left", padx=3)

    def open_selected(self) -> None:
        item = self.tree.focus()
        if not item:
            return
        path = self.tree.item(item, "values")[1]
        messagebox.showinfo("輸出資料夾", path)

    def export_summary(self) -> None:
        paths = export_current_summary()
        messagebox.showinfo("匯出完成", "\n".join(paths.values()))

    def export_timeline(self) -> None:
        path = Path("desktop") / "output" / "phase2_3_prospective" / "timeline_export.csv"
        rows = timeline_rows()
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(rows[0].keys()) if rows else ["target_issue"])
            writer.writeheader()
            writer.writerows(rows)
        messagebox.showinfo("匯出完成", str(path))

    def _show(self, text: str) -> None:
        messagebox.showinfo("報告與輸出", text)
