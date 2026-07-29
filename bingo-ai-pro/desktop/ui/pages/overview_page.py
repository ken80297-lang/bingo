from __future__ import annotations

from tkinter import filedialog, messagebox, ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import (
    dataset_status,
    export_current_summary,
    latest_backtest_summary,
    latest_phase2_1_summary,
    load_user_settings,
    prospective_status,
    run_prospective_operation,
)
from desktop.ui.simulator_widgets import ScrollPage, kv_table, number_row, title


class OverviewPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        self.settings = load_user_settings()
        self.csv_path = self.settings["default_csv_path"]
        self.refresh()

    def refresh(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        title(self.body, "首頁總覽", 0)
        data = dataset_status(self.csv_path)
        kv_table(
            self.body,
            [
                ("CSV 路徑", data["path"]),
                ("總期數", data["total_rows"]),
                ("合法期數", data["valid_rows"]),
                ("錯誤期數", data["error_rows"]),
                ("最早 issue", data["first_issue"]),
                ("最新 issue", data["last_issue"]),
                ("日期範圍", data["date_range"]),
                ("Dataset hash", data["dataset_hash"]),
            ],
            1,
        )
        self._buttons(2)
        self._backtest_summary(3)
        self._prospective(4)
        self._quick_actions(7)

    def _buttons(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="資料狀態操作", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        ttk.Button(frame, text="選擇 CSV", command=self.choose_csv).pack(side="left", padx=3)
        ttk.Button(frame, text="重新載入", command=self.refresh).pack(side="left", padx=3)
        ttk.Button(frame, text="驗證資料", command=self.verify_data).pack(side="left", padx=3)
        ttk.Button(frame, text="開啟資料夾", command=lambda: messagebox.showinfo("資料夾", self.csv_path)).pack(side="left", padx=3)

    def _backtest_summary(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="最近模擬結果", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        summary = latest_backtest_summary()
        stats = latest_phase2_1_summary()
        if not summary:
            ttk.Label(frame, text="尚未執行模擬").pack(anchor="w")
            return
        rows = [
            ("Replay 期數", summary.get("valid_simulations")),
            ("20 碼平均命中", summary.get("average_hits")),
            ("理論隨機基準", 5.0),
            ("AI 優先 5 碼平均命中", summary.get("average_high5_hits")),
            ("AI 優先 5 碼隨機基準", 1.25),
            ("超級獎命中率", summary.get("super_hit_rate")),
            ("大小準確率", summary.get("big_small_hit_rate")),
            ("單雙準確率", summary.get("odd_even_hit_rate")),
            ("Look-ahead 狀態", "無" if summary.get("no_look_ahead") else "需檢查"),
            ("統計結論", _conclusion((stats.get("overall") or {}).get("difference_vs_baseline"))),
        ]
        kv_table(frame, rows, 0)

    def _prospective(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="前瞻驗證狀態", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        status = prospective_status()
        current = status["current"]
        pending = status["pending"] or {}
        kv_table(
            frame,
            [
                ("Experiment ID", current.get("experiment_id")),
                ("Archive 完整性", "通過" if current.get("archive_hash_verified") else "需檢查"),
                ("Registry 完整性", "通過" if status["invariants"].get("registry_hash_verified") else "需檢查"),
                ("Trigger definition 完整性", current.get("trigger_definition_hash")),
                ("最新合法 issue", current.get("latest_valid_issue")),
                ("Pending target", current.get("current_pending_target")),
                ("Pending Snapshot 數", current.get("pending_snapshot_count")),
                ("已驗證數", current.get("validation_count")),
                ("Eligible primary 數", current.get("eligible_primary_count")),
                ("Retrospective 數", current.get("retrospective_count")),
                ("下一 checkpoint", current.get("next_checkpoint")),
                ("狀態", "等待開獎結果" if pending else current.get("status")),
            ],
            0,
        )
        if pending:
            ttk.Label(frame, text="Pending Snapshot 候選").grid(row=1, column=0, sticky="w", pady=(8, 0))
            number_row(frame, pending.get("missing_top3", []), 2, {int(n): "recommended" for n in pending.get("missing_top3", [])})

    def _quick_actions(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="快速操作", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        ttk.Button(frame, text="執行完整歷史模擬", command=lambda: messagebox.showinfo("提示", "請至歷史模擬頁執行")).pack(side="left", padx=3)
        ttk.Button(frame, text="執行最近 100 期", command=lambda: messagebox.showinfo("提示", "請至歷史模擬頁執行")).pack(side="left", padx=3)
        ttk.Button(frame, text="開啟單期模擬", command=lambda: messagebox.showinfo("提示", "請由左側切換至單期模擬")).pack(side="left", padx=3)
        ttk.Button(frame, text="執行本期前瞻流程", command=self.run_prospective).pack(side="left", padx=3)
        ttk.Button(frame, text="查看最新報告", command=lambda: messagebox.showinfo("報告", "\n".join(export_current_summary().values()))).pack(side="left", padx=3)

    def choose_csv(self) -> None:
        path = filedialog.askopenfilename(title="選擇 CSV", filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if path:
            self.csv_path = path
            self.refresh()

    def verify_data(self) -> None:
        data = dataset_status(self.csv_path)
        messagebox.showinfo("資料驗證", f"合法期數：{data['valid_rows']}\n錯誤期數：{data['error_rows']}")

    def run_prospective(self) -> None:
        result = run_prospective_operation(self.csv_path)
        status = result["current_status"]
        if status.get("skip_reason") == "snapshot_already_exists":
            messagebox.showinfo("前瞻流程", "目前沒有 115041413 開獎資料，原 Snapshot 繼續等待結果。")
        self.refresh()


def _conclusion(diff) -> str:
    try:
        value = float(diff)
    except (TypeError, ValueError):
        return "樣本不足"
    if value > 0.05:
        return "顯著性仍需檢查"
    if value < 0:
        return "表現低於基準"
    return "無法證明優於隨機"
