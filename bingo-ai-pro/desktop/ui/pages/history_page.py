from __future__ import annotations

import csv
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from desktop.core.data_repository import DataRepository
from desktop.core.replay_worker import ReplayWorker, WorkerEvent
from desktop.core.simulator_services import load_user_settings
from desktop.ui.simulator_widgets import ScrollPage, data_tree, kv_table, title


class HistoryPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        settings = load_user_settings()
        self.worker: ReplayWorker | None = None
        self.last_report: dict | None = None
        title(self.body, "歷史模擬", 0)
        self.csv_var = tk.StringVar(value=settings["default_csv_path"])
        self.warmup_var = tk.StringVar(value=str(settings["warmup"]))
        self.recent_var = tk.StringVar(value="")
        self.stage_var = tk.StringVar(value="待命")
        self.progress_var = tk.DoubleVar(value=0)
        self.chart_var = tk.StringVar(value="每期 20 碼命中")
        self._controls(1)
        self._progress(2)
        self.result_frame = ttk.LabelFrame(self.body, text="歷史模擬結果", padding=8)
        self.result_frame.grid(row=3, column=0, sticky="ew", pady=8)
        self.chart_frame = ttk.LabelFrame(self.body, text="歷史走勢圖", padding=8)
        self.chart_frame.grid(row=4, column=0, sticky="nsew", pady=8)
        self.chart = tk.Canvas(self.chart_frame, height=240, bg="white", highlightthickness=1)
        self.chart.grid(row=1, column=0, sticky="ew")
        ttk.OptionMenu(
            self.chart_frame,
            self.chart_var,
            self.chart_var.get(),
            "每期 20 碼命中",
            "100 期移動平均",
            "AI 優先 5 碼命中",
            "累積平均命中",
            "與隨機基準差異",
            "每日平均命中",
            command=lambda _: self._draw_chart(),
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(self.chart_frame, text="重設", command=self._draw_chart).grid(row=0, column=1, padx=4)
        ttk.Button(self.chart_frame, text="匯出 PNG", command=lambda: messagebox.showinfo("匯出 PNG", "目前 Tkinter 畫布可另存 PostScript；PNG 匯出保留給打包版。")).grid(row=0, column=2, padx=4)

    def _controls(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="模擬條件", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        for col in range(6):
            frame.grid_columnconfigure(col, weight=1)
        ttk.Label(frame, text="CSV 路徑").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.csv_var, width=70).grid(row=0, column=1, columnspan=4, sticky="ew", padx=4)
        ttk.Button(frame, text="選擇 CSV", command=self.choose_csv).grid(row=0, column=5)
        ttk.Label(frame, text="Warm-up 期數").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.warmup_var, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(frame, text="最近多少期").grid(row=1, column=2, sticky="w")
        ttk.Entry(frame, textvariable=self.recent_var, width=10).grid(row=1, column=3, sticky="w")
        self.rule_var = tk.BooleanVar(value=True)
        self.walk_var = tk.BooleanVar(value=True)
        self.stat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Rule 分析", variable=self.rule_var).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(frame, text="Walk-forward", variable=self.walk_var).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(frame, text="統計驗證", variable=self.stat_var).grid(row=2, column=2, sticky="w")
        ttk.Button(frame, text="開始模擬", command=self.start).grid(row=3, column=0, pady=6)
        ttk.Button(frame, text="停止模擬", command=self.cancel).grid(row=3, column=1)
        ttk.Button(frame, text="重設條件", command=self.reset).grid(row=3, column=2)
        ttk.Button(frame, text="開啟輸出", command=lambda: messagebox.showinfo("輸出", "desktop/output/phase2_30day")).grid(row=3, column=3)
        ttk.Button(frame, text="匯出摘要", command=self.export_summary).grid(row=3, column=4)

    def _progress(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="背景執行", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        ttk.Label(frame, textvariable=self.stage_var).pack(anchor="w")
        ttk.Progressbar(frame, variable=self.progress_var, maximum=100).pack(fill="x", pady=4)

    def choose_csv(self) -> None:
        path = filedialog.askopenfilename(title="選擇 CSV", filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if path:
            self.csv_var.set(path)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("模擬中", "目前已有模擬正在執行")
            return
        recent = int(self.recent_var.get()) if self.recent_var.get().strip() else None
        self.worker = ReplayWorker(self.csv_var.get(), int(self.warmup_var.get()), recent)
        self.worker.on_event(self._handle_event)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.stage_var.set("模擬已停止")

    def reset(self) -> None:
        self.warmup_var.set("100")
        self.recent_var.set("")
        self.stage_var.set("待命")
        self.progress_var.set(0)

    def _handle_event(self, event: WorkerEvent) -> None:
        self.after(0, lambda: self._render_event(event))

    def _render_event(self, event: WorkerEvent) -> None:
        payload = event.payload
        if event.name in {"started", "stage_changed", "finished"}:
            self.stage_var.set(f"{payload.get('stage')}  {payload.get('percent', 0)}%")
            self.progress_var.set(payload.get("percent", 0))
        if event.name == "finished":
            self.last_report = payload["report"]
            self._render_result(self.last_report)
            self._draw_chart()
        if event.name == "cancelled":
            self.stage_var.set("模擬已停止")
        if event.name == "failed":
            self.stage_var.set("模擬失敗")
            messagebox.showerror("模擬失敗", payload.get("message", "未知錯誤"))

    def _render_result(self, report: dict) -> None:
        for child in self.result_frame.winfo_children():
            child.destroy()
        rows = [
            ("有效 Replay 期數", report.get("valid_simulations")),
            ("20 碼平均命中", report.get("average_hits")),
            ("20 碼 95% CI", "請見統計驗證頁"),
            ("20 碼 p-value", "請見統計驗證頁"),
            ("AI 優先 5 碼平均命中", report.get("average_high5_hits")),
            ("AI 優先 5 碼 95% CI", "請見統計驗證頁"),
            ("AI 優先 5 碼 p-value", "請見統計驗證頁"),
            ("超級獎命中率", report.get("super_hit_rate")),
            ("大小準確率", report.get("big_small_hit_rate")),
            ("單雙準確率", report.get("odd_even_hit_rate")),
            ("統計判斷", "無法證明優於隨機"),
        ]
        kv_table(self.result_frame, rows, 0)

    def _draw_chart(self) -> None:
        self.chart.delete("all")
        if not self.last_report:
            self.chart.create_text(320, 120, text="尚未執行模擬", fill="#64748b")
            return
        sims = self.last_report.get("simulations") or []
        values = _chart_values(sims, self.chart_var.get())
        if not values:
            return
        width = max(600, self.chart.winfo_width() or 600)
        height = 220
        max_v = max(max(values), 5)
        min_v = min(min(values), 0)
        points = []
        for i, value in enumerate(values):
            x = 20 + i * (width - 40) / max(1, len(values) - 1)
            y = height - 20 - (value - min_v) * (height - 40) / max(1, max_v - min_v)
            points.extend([x, y])
        self.chart.create_line(20, height - 20, width - 20, height - 20, fill="#cbd5e1")
        self.chart.create_line(20, 20, 20, height - 20, fill="#cbd5e1")
        self.chart.create_line(points, fill="#2563eb", width=2)
        self.chart.create_text(width / 2, 12, text=self.chart_var.get(), fill="#0f172a")

    def export_summary(self) -> None:
        if not self.last_report:
            messagebox.showinfo("匯出摘要", "尚未執行模擬")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["項目", "數值"])
            for key in ["valid_simulations", "average_hits", "average_high5_hits", "super_hit_rate"]:
                writer.writerow([key, self.last_report.get(key)])


def _chart_values(sims: list[dict], mode: str) -> list[float]:
    if mode == "AI 優先 5 碼命中":
        return [item["hits_high5"] for item in sims]
    if mode == "100 期移動平均":
        return [sum(item["hits_20"] for item in sims[max(0, i - 99): i + 1]) / len(sims[max(0, i - 99): i + 1]) for i in range(len(sims))]
    if mode == "累積平均命中":
        total = 0
        out = []
        for i, item in enumerate(sims, start=1):
            total += item["hits_20"]
            out.append(total / i)
        return out
    if mode == "與隨機基準差異":
        return [item["hits_20"] - 5 for item in sims]
    if mode == "每日平均命中":
        buckets: dict[str, list[int]] = {}
        for item in sims:
            buckets.setdefault(item["target_date"], []).append(item["hits_20"])
        return [sum(values) / len(values) for _, values in sorted(buckets.items())]
    return [item["hits_20"] for item in sims]
