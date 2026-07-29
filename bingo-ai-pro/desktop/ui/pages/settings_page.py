from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import load_user_settings, save_user_settings
from desktop.ui.simulator_widgets import ScrollPage, title


class SettingsPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        settings = load_user_settings()
        title(self.body, "系統設定", 0)
        self.vars = {
            "default_csv_path": tk.StringVar(value=settings["default_csv_path"]),
            "default_output_path": tk.StringVar(value=settings["default_output_path"]),
            "warmup": tk.StringVar(value=str(settings["warmup"])),
            "recent_limit": tk.StringVar(value=str(settings["recent_limit"])),
            "auto_load_on_start": tk.BooleanVar(value=settings["auto_load_on_start"]),
            "verify_hash_on_start": tk.BooleanVar(value=settings["verify_hash_on_start"]),
            "open_report_on_finish": tk.BooleanVar(value=settings["open_report_on_finish"]),
            "show_advanced_statistics": tk.BooleanVar(value=settings["show_advanced_statistics"]),
            "auto_mode": tk.BooleanVar(value=settings["auto_mode"]),
        }
        self._build()

    def _build(self) -> None:
        frame = ttk.LabelFrame(self.body, text="本機設定（唯讀桌面模式）", padding=8)
        frame.grid(row=1, column=0, sticky="ew")
        labels = [
            ("預設 CSV 路徑", "default_csv_path"),
            ("預設輸出路徑", "default_output_path"),
            ("Warm-up", "warmup"),
            ("最近模擬期數", "recent_limit"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(frame, textvariable=self.vars[key], width=80).grid(row=row, column=1, sticky="ew", padx=4)
            if key.endswith("path"):
                ttk.Button(frame, text="選擇", command=lambda k=key: self.choose(k)).grid(row=row, column=2)
        checks = [
            ("啟動時自動載入", "auto_load_on_start"),
            ("啟動時驗證 Hash", "verify_hash_on_start"),
            ("完成後自動開啟報告", "open_report_on_finish"),
            ("顯示進階統計", "show_advanced_statistics"),
            ("自動模式", "auto_mode"),
        ]
        for index, (label, key) in enumerate(checks, start=len(labels)):
            ttk.Checkbutton(frame, text=label, variable=self.vars[key]).grid(row=index, column=0, sticky="w", pady=2)
        ttk.Button(frame, text="儲存設定", command=self.save).grid(row=len(labels) + len(checks), column=0, sticky="w", pady=8)
        ttk.Label(self.body, text="Read-only Desktop Mode：不修改 backend、正式資料庫、Render、Supabase 或 Collector 設定。").grid(row=2, column=0, sticky="w", pady=8)

    def choose(self, key: str) -> None:
        if key == "default_csv_path":
            path = filedialog.askopenfilename(title="選擇 CSV", filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        else:
            path = filedialog.askdirectory(title="選擇輸出資料夾")
        if path:
            self.vars[key].set(path)

    def save(self) -> None:
        settings = {}
        for key, var in self.vars.items():
            value = var.get()
            if key in {"warmup", "recent_limit"}:
                value = int(value)
            settings[key] = value
        save_user_settings(settings)
        messagebox.showinfo("設定", "已保存 desktop 本機設定")
