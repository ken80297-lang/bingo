from __future__ import annotations

from tkinter import ttk


PAGES = [
    ("overview", "首頁總覽"),
    ("history", "歷史模擬"),
    ("single", "單期模擬"),
    ("rules", "規則分析"),
    ("statistics", "統計驗證"),
    ("prospective", "前瞻驗證"),
    ("timeline", "預測時間軸"),
    ("reports", "報告與輸出"),
    ("settings", "系統設定"),
]


class Navigation(ttk.Frame):
    def __init__(self, master, on_select) -> None:
        super().__init__(master, style="Panel.TFrame", padding=(8, 12))
        self.on_select = on_select
        self.buttons: dict[str, ttk.Button] = {}
        for key, label in PAGES:
            button = ttk.Button(self, text=label, command=lambda page=key: self.select(page))
            button.pack(fill="x", pady=3)
            self.buttons[key] = button

    def select(self, page: str) -> None:
        self.on_select(page)
