from __future__ import annotations

import threading
from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.phase2_backtest import run_phase2_backtest
from desktop.core.phase2_2_sparse_triggers import run_phase2_2_sparse_triggers
from desktop.core.phase2_3_prospective import run_phase2_3_prospective
from desktop.core.phase2_4_operations import run_phase2_4_operation_cycle
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH


class BacktestPage(ttk.Frame):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        ttk.Label(self, text="30 \u5929 Replay / AI Backtest\uff08\u552f\u8b80\uff09", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        toolbar = ttk.Frame(self)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(toolbar, text="CSV").pack(side="left")
        self.path_var = ttk.Entry(toolbar, width=72)
        self.path_var.insert(0, str(DEFAULT_MASTER_DRAWS_PATH))
        self.path_var.pack(side="left", padx=4)
        ttk.Button(toolbar, text="\u57f7\u884c", command=self.run_backtest).pack(side="left")
        ttk.Button(toolbar, text="\u7a00\u758f Trigger", command=self.run_sparse_triggers).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="\u524d\u77bb\u9a57\u8b49", command=self.run_prospective).pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="\u5efa\u7acb\u5feb\u7167", command=self.run_snapshot_cycle).pack(side="left", padx=(4, 0))
        self.status = ttk.Label(toolbar, text="\u5f85\u547d")
        self.status.pack(side="left", padx=10)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.trees = {
            "replay": self._add_tree("Replay Summary"),
            "backtest": self._add_tree("Backtest Summary"),
            "confidence": self._add_tree("High Confidence Report"),
            "rules": self._add_tree("Rule Performance"),
            "sparse": self._add_tree("\u7a00\u758f Trigger \u5c01\u5b58"),
            "prospective": self._add_tree("\u524d\u77bb Trigger \u9a57\u8b49"),
            "snapshot_ops": self._add_tree("\u5feb\u7167\u64cd\u4f5c\u4e2d\u5fc3"),
        }

    def _add_tree(self, title: str) -> ttk.Treeview:
        frame = ttk.Frame(self.notebook)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("metric", "value"), show="headings", height=18)
        tree.heading("metric", text="\u9805\u76ee")
        tree.heading("value", text="\u6578\u503c")
        tree.grid(row=0, column=0, sticky="nsew")
        self.notebook.add(frame, text=title)
        return tree

    def run_backtest(self) -> None:
        self.status.configure(text="\u57f7\u884c\u4e2d...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        report = run_phase2_backtest(self.path_var.get())
        self.after(0, lambda: self._render(report))

    def run_sparse_triggers(self) -> None:
        self.status.configure(text="\u7a00\u758f Trigger \u5206\u6790\u4e2d...")
        threading.Thread(target=self._sparse_worker, daemon=True).start()

    def _sparse_worker(self) -> None:
        result = run_phase2_2_sparse_triggers(self.path_var.get())
        self.after(0, lambda: self._render_sparse(result))

    def run_prospective(self) -> None:
        self.status.configure(text="\u524d\u77bb\u9a57\u8b49\u8b80\u53d6\u4e2d...")
        threading.Thread(target=self._prospective_worker, daemon=True).start()

    def _prospective_worker(self) -> None:
        result = run_phase2_3_prospective(self.path_var.get())
        self.after(0, lambda: self._render_prospective(result))

    def run_snapshot_cycle(self) -> None:
        self.status.configure(text="\u5feb\u7167\u64cd\u4f5c\u4e2d...")
        threading.Thread(target=self._snapshot_worker, daemon=True).start()

    def _snapshot_worker(self) -> None:
        result = run_phase2_4_operation_cycle(self.path_var.get())
        self.after(0, lambda: self._render_snapshot_ops(result))

    def _render(self, report: dict) -> None:
        for tree in self.trees.values():
            tree.delete(*tree.get_children())
        dataset = report["dataset"]
        replay_rows = [
            ("\u7e3d\u671f\u6578", dataset["total_rows"]),
            ("\u5408\u6cd5\u671f\u6578", dataset["valid_rows"]),
            ("\u7b2c\u4e00\u500b issue", dataset["first_issue"]),
            ("\u6700\u5f8c issue", dataset["last_issue"]),
            ("\u7f3a\u6f0f issue", len(dataset["missing_issues"])),
            ("\u975e\u6cd5\u8cc7\u6599", dataset["invalid_rows"]),
        ]
        backtest_rows = [
            ("\u7e3d\u6a21\u64ec\u671f\u6578", report["total_simulations"]),
            ("\u6709\u6548\u6a21\u64ec\u671f\u6578", report["valid_simulations"]),
            ("\u5e73\u5747\u547d\u4e2d", report["average_hits"]),
            ("\u6700\u9ad8\u547d\u4e2d", report["max_hits"]),
            ("\u6700\u4f4e\u547d\u4e2d", report["min_hits"]),
            ("\u9ad8\u6a5f\u73875\u78bc\u5e73\u5747\u547d\u4e2d", report["average_high5_hits"]),
            ("\u8d85\u7d1a\u734e\u547d\u4e2d\u7387", report["super_hit_rate"]),
            ("\u5927\u5c0f\u547d\u4e2d\u7387", report["big_small_hit_rate"]),
            ("\u55ae\u96d9\u547d\u4e2d\u7387", report["odd_even_hit_rate"]),
            ("Look-ahead", "\u7121" if report["no_look_ahead"] else "\u6709"),
        ]
        confidence_rows = [
            ("\u9ad8\u4fe1\u5fc3\u7b56\u7565", report["high_confidence"]["high_confidence_strategy"]),
            ("\u689d\u4ef6\u8868\u73fe", report["high_confidence"]["conditions"]),
            ("\u689d\u4ef6\u6578\u91cf\u63d0\u5347", report["high_confidence"]["condition_count_lift"]),
        ]
        rule_rows = [
            (payload["rule_name_zh"], f"usage={payload['usage_count']}, avg={payload['average_score']}, success={payload['success_rate']}")
            for payload in report["rule_performance"].values()
        ]
        for tree_key, rows in [("replay", replay_rows), ("backtest", backtest_rows), ("confidence", confidence_rows), ("rules", rule_rows)]:
            for row in rows:
                self.trees[tree_key].insert("", "end", values=row)
        self.status.configure(text="\u5b8c\u6210")

    def _render_sparse(self, result: dict) -> None:
        tree = self.trees["sparse"]
        tree.delete(*tree.get_children())
        summary = result["phase2_2_summary"]
        split = summary["split"]
        rows = [
            ("Discovery \u671f\u6578", f"{split['discovery']['count']} / {split['discovery']['issue_range']}"),
            ("Validation \u671f\u6578", f"{split['validation']['count']} / {split['validation']['issue_range']}"),
            ("Final Holdout \u671f\u6578", f"{split['final_holdout']['count']} / {split['final_holdout']['issue_range']}"),
            ("\u8cc7\u6599 hash", summary["dataset_hash"]),
            ("Discovery Trigger", summary["generated_trigger_count"]),
            ("Discovery \u6649\u7d1a", summary["discovery_survivor_count"]),
            ("Validation \u6649\u7d1a", summary["validation_survivor_count"]),
            ("\u9810\u8a3b\u518a Trigger", summary["preregistered_trigger_count"]),
            ("Final Holdout \u901a\u904e", summary["final_passed_count"]),
            ("preregistration hash", summary["preregistration_hash"]),
            ("Final \u662f\u5426\u53ea\u57f7\u884c\u4e00\u6b21", "\u662f" if summary["final_holdout_only_executed_once"] else "\u5426"),
            ("Look-ahead", "\u7121" if summary["no_look_ahead"] else "\u6709"),
            ("\u7814\u7a76 Trigger", "\u6709" if summary["found_promotable_research_trigger"] else "\u672a\u767c\u73fe\u53ef\u6649\u7d1a Trigger"),
        ]
        for row in rows:
            tree.insert("", "end", values=row)
        self.status.configure(text="\u7a00\u758f Trigger \u5b8c\u6210")

    def _render_prospective(self, result: dict) -> None:
        tree = self.trees["prospective"]
        tree.delete(*tree.get_children())
        current = result["current_status"]
        rows = [
            ("Experiment ID", current["experiment_id"]),
            ("Registry hash", current["prospective_registry_hash"]),
            ("Trigger definition hash", current["trigger_definition_hash"]),
            ("\u524d\u77bb\u8d77\u59cb issue", current["prospective_start_issue"]),
            ("\u6b77\u53f2\u6700\u5f8c issue", current["historical_last_issue"]),
            ("\u524d\u77bb\u8cc7\u6599\u6578", current["prospective_targets_loaded"]),
            ("\u4e3b\u5206\u6790\u5408\u683c\u6578", current["eligible_primary_targets"]),
            ("Snapshot", current["prediction_snapshots"]),
            ("Retrospective reconstruction", current["retrospective_reconstruction_count"]),
            ("\u4e0b\u4e00 checkpoint", current["next_checkpoint"]),
            ("Checkpoint", current["checkpoint_status"]),
            ("\u72c0\u614b", current["status"]),
        ]
        for row in rows:
            tree.insert("", "end", values=row)
        self.status.configure(text="\u524d\u77bb\u9a57\u8b49\u5b8c\u6210")

    def _render_snapshot_ops(self, result: dict) -> None:
        tree = self.trees["snapshot_ops"]
        tree.delete(*tree.get_children())
        status = result["current_status"]
        rows = [
            ("Archive hash \u9a57\u8b49", "\u901a\u904e" if status["archive_hash_verified"] else "\u5931\u6557"),
            ("Registry hash", status["registry_hash"]),
            ("Trigger definition hash", status["trigger_definition_hash"]),
            ("\u6700\u65b0\u5408\u6cd5 issue", status["latest_valid_issue"]),
            ("\u4e0b\u4e00 target issue", status["next_target_issue"]),
            ("\u65b0\u5efa Snapshot", status["created_snapshot_target_issue"] or "\u672a\u5efa\u7acb"),
            ("Snapshot hash", status["created_snapshot_hash"]),
            ("Snapshot chain hash", status["created_snapshot_chain_hash"]),
            ("maximum_feature_issue", status["maximum_feature_issue"]),
            ("Pending Snapshot", status["pending_snapshot_count"]),
            ("\u5df2\u9a57\u8b49 Snapshot", status["validated_snapshot_count"]),
            ("Retrospective reconstruction", status["retrospective_reconstruction_count"]),
            ("\u4e0b\u4e00 checkpoint", status["next_checkpoint"]),
            ("Checkpoint", status["checkpoint_status"]),
            ("\u72c0\u614b", status["status"]),
        ]
        for row in rows:
            tree.insert("", "end", values=row)
        self.status.configure(text="\u5feb\u7167\u64cd\u4f5c\u5b8c\u6210")
