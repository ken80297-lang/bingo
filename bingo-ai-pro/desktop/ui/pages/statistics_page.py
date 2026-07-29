from __future__ import annotations

from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import latest_phase2_1_summary
from desktop.ui.simulator_widgets import ScrollPage, data_tree, kv_table, title


class StatisticsPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        title(self.body, "統計驗證", 0)
        data = latest_phase2_1_summary()
        kv_table(
            self.body,
            [
                ("20 碼顯著性", _metric(data["overall"])),
                ("AI 優先 5 碼顯著性", _metric(data["high5"])),
                ("超級獎顯著性", _metric(data["super"])),
                ("Fold 總數", (data["walk"].get("fold_count") if data["walk"] else "")),
                ("正 lift folds", (data["walk"].get("positive_lift_folds") if data["walk"] else "")),
                ("負 lift folds", (data["walk"].get("negative_lift_folds") if data["walk"] else "")),
                ("是否穩定", "仍需觀察"),
                ("FDR/BH 通過數", sum(1 for row in data["multiple"] if str(row.get("significant_bh_0_05")).lower() == "true")),
            ],
            1,
        )
        ttk.Label(self.body, text="Walk-forward", style="Title.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.walk_tree = data_tree(
            self.body,
            [
                ("fold", "Fold", 50),
                ("train", "訓練範圍", 190),
                ("test", "測試範圍", 190),
                ("sample", "樣本數", 70),
                ("avg", "平均命中", 80),
                ("base", "隨機基準", 80),
                ("lift", "Lift", 70),
                ("direction", "方向", 70),
            ],
            3,
            8,
        )
        self._load_walk()
        ttk.Label(self.body, text="Multiple testing / Losing streak", style="Title.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 0))
        self.multi_tree = data_tree(self.body, [("name", "項目", 220), ("p", "p-value", 100), ("bh", "FDR/BH", 100), ("note", "備註", 240)], 5, 8)
        for row in data["multiple"][:20]:
            self.multi_tree.insert("", "end", values=(row.get("test_name"), row.get("p_value"), row.get("significant_bh_0_05"), "研究用"))

    def _load_walk(self) -> None:
        import csv
        from desktop.core.simulator_services import OUTPUT_ROOT

        path = OUTPUT_ROOT / "phase2_1_validation" / "walk_forward_folds.csv"
        if not path.exists():
            return
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                lift = float(row.get("lift_vs_random_20") or 0)
                self.walk_tree.insert(
                    "",
                    "end",
                    values=(
                        row.get("fold"),
                        row.get("training_issue_range"),
                        row.get("validation_issue_range"),
                        row.get("validation_count"),
                        row.get("average_hits"),
                        5.0,
                        row.get("lift_vs_random_20"),
                        "正" if lift > 0 else "負",
                    ),
                )


def _metric(row: dict) -> str:
    if not row:
        return "尚無資料"
    return f"mean={row.get('mean') or row.get('hit_rate')} p={row.get('p_value_vs_baseline')} CI={row.get('confidence_interval_95')}"
