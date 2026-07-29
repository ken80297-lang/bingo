from __future__ import annotations

from tkinter import filedialog, messagebox, ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import load_user_settings, prospective_status, run_prospective_operation
from desktop.ui.simulator_widgets import ScrollPage, kv_table, number_row, title


class ProspectivePage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        self.csv_path = load_user_settings()["default_csv_path"]
        self.refresh()

    def refresh(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        title(self.body, "前瞻驗證", 0)
        status = prospective_status()
        current = status["current"]
        pending = status["pending"] or {}
        kv_table(
            self.body,
            [
                ("Archive hash", current.get("archive_manifest_hash")),
                ("Registry hash", current.get("registry_hash")),
                ("Trigger definition hash", current.get("trigger_definition_hash")),
                ("Hash 驗證狀態", current.get("integrity_status")),
            ],
            1,
        )
        frame = ttk.LabelFrame(self.body, text="Pending Snapshot", padding=8)
        frame.grid(row=2, column=0, sticky="ew", pady=8)
        if pending:
            kv_table(
                frame,
                [
                    ("目標期號", pending.get("target_issue")),
                    ("產生時間", pending.get("generated_at")),
                    ("maximum_feature_issue", pending.get("maximum_feature_issue")),
                    ("Snapshot hash", _short(pending.get("snapshot_hash"))),
                    ("Chain hash", _short((status["manifest"].get("snapshot_hashes") or [{}])[-1].get("chain_hash"))),
                    ("狀態", "等待開獎結果"),
                ],
                0,
            )
            number_row(frame, pending.get("missing_top3", []), 1, {int(n): "recommended" for n in pending.get("missing_top3", [])})
        else:
            ttk.Label(frame, text="尚無 Pending Snapshot").grid(row=0, column=0, sticky="w")
        self._buttons(3)
        self._cumulative(4, current)

    def _buttons(self, row: int) -> None:
        frame = ttk.LabelFrame(self.body, text="操作", padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=8)
        for label, command in [
            ("選擇更新後 CSV", self.choose_csv),
            ("匯入最新資料", self.run_flow),
            ("驗證 Pending Snapshot", self.run_flow),
            ("建立下一期 Snapshot", self.run_flow),
            ("執行本期前瞻流程", self.run_flow),
            ("查看 Snapshot", self.show_snapshot),
            ("查看驗證紀錄", lambda: messagebox.showinfo("驗證紀錄", "請至預測時間軸或輸出頁查看")),
            ("執行完整性檢查", self.integrity),
        ]:
            ttk.Button(frame, text=label, command=command).pack(side="left", padx=3, pady=2)

    def _cumulative(self, row: int, current: dict) -> None:
        kv_table(
            self.body,
            [
                ("Validation count", current.get("validation_count")),
                ("Eligible primary count", current.get("eligible_primary_count")),
                ("Excluded count", current.get("excluded_count")),
                ("Retrospective count", current.get("retrospective_count")),
                ("Top 1 cumulative precision", current.get("top1_cumulative_precision") or "尚未驗證"),
                ("Top 2 cumulative precision", current.get("top2_cumulative_precision") or "尚未驗證"),
                ("Top 3 cumulative precision", current.get("top3_cumulative_precision") or "尚未驗證"),
                ("Top 1 lift", current.get("top1_normalized_lift") or "尚未驗證"),
                ("Top 2 lift", current.get("top2_normalized_lift") or "尚未驗證"),
                ("Top 3 lift", current.get("top3_normalized_lift") or "尚未驗證"),
                ("下一 checkpoint", current.get("next_checkpoint")),
            ],
            row,
        )

    def choose_csv(self) -> None:
        path = filedialog.askopenfilename(title="選擇更新後 CSV", filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if path:
            self.csv_path = path

    def run_flow(self) -> None:
        result = run_prospective_operation(self.csv_path)
        status = result["current_status"]
        if status.get("skip_reason") == "snapshot_already_exists":
            messagebox.showinfo("前瞻流程", "目前沒有 115041413 開獎資料，原 Snapshot 繼續等待結果。")
        else:
            messagebox.showinfo("前瞻流程", "前瞻流程已完成")
        self.refresh()

    def show_snapshot(self) -> None:
        pending = prospective_status()["pending"]
        messagebox.showinfo("Snapshot", str(pending or "尚無"))

    def integrity(self) -> None:
        status = prospective_status()
        messagebox.showinfo("完整性", f"Archive: {status['current'].get('archive_hash_verified')}\nRegistry: {status['invariants'].get('registry_hash_verified')}")


def _short(value) -> str:
    text = str(value or "")
    return f"{text[:12]}...{text[-8:]}" if len(text) > 24 else text
