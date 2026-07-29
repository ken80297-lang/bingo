from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import dataset_status, load_user_settings, single_issue_payload
from desktop.ui.simulator_widgets import ScrollPage, kv_table, number_row, title


class SingleIssuePage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        settings = load_user_settings()
        data = dataset_status(settings["default_csv_path"])
        self.csv_path = settings["default_csv_path"]
        next_issue = str(int(data["last_issue"]) + 1) if data["last_issue"] else ""
        self.issue_var = tk.StringVar(value=next_issue)
        title(self.body, "單期模擬", 0)
        controls = ttk.Frame(self.body)
        controls.grid(row=1, column=0, sticky="ew", pady=6)
        ttk.Label(controls, text="Target issue").pack(side="left")
        ttk.Entry(controls, textvariable=self.issue_var, width=18).pack(side="left", padx=4)
        ttk.Button(controls, text="上一期", command=lambda: self._move(-1)).pack(side="left", padx=2)
        ttk.Button(controls, text="下一期", command=lambda: self._move(1)).pack(side="left", padx=2)
        ttk.Button(controls, text="執行單期模擬", command=self.run).pack(side="left", padx=6)
        self.result = ttk.Frame(self.body)
        self.result.grid(row=2, column=0, sticky="ew")
        self.run()

    def _move(self, delta: int) -> None:
        try:
            self.issue_var.set(str(int(self.issue_var.get()) + delta))
        except ValueError:
            return
        self.run()

    def run(self) -> None:
        for child in self.result.winfo_children():
            child.destroy()
        payload = single_issue_payload(self.csv_path, self.issue_var.get())
        kv_table(
            self.result,
            [
                ("Target issue", payload.get("target_issue")),
                ("Maximum feature issue", payload.get("maximum_feature_issue")),
                ("使用歷史期數", payload.get("history_count")),
                ("超級獎候選", payload.get("super_candidate")),
                ("大小判斷", payload.get("big_small")),
                ("單雙判斷", payload.get("odd_even")),
                ("實際超級獎", payload.get("actual_super") or "尚無資料"),
                ("命中數", payload.get("hit_count") if payload.get("hit_count") is not None else "尚未驗證"),
            ],
            0,
        )
        ttk.Label(self.result, text="推薦 20 碼").grid(row=1, column=0, sticky="w", pady=(8, 0))
        number_row(self.result, payload.get("recommend_numbers") or [], 2, {n: "recommended" for n in payload.get("high_probability_numbers") or []})
        ttk.Label(self.result, text="AI 優先 5 碼").grid(row=3, column=0, sticky="w", pady=(8, 0))
        number_row(self.result, payload.get("high_probability_numbers") or [], 4, {n: "high_probability" for n in payload.get("high_probability_numbers") or []})
        ttk.Label(self.result, text="實際開獎 20 碼").grid(row=5, column=0, sticky="w", pady=(8, 0))
        if payload.get("actual_numbers"):
            states = {n: "hit" for n in payload.get("hit_numbers") or []}
            number_row(self.result, payload["actual_numbers"], 6, states)
        else:
            ttk.Label(self.result, text="尚無資料，precision 與 lift 尚未驗證").grid(row=6, column=0, sticky="w")
