from __future__ import annotations

from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import rule_rows
from desktop.ui.simulator_widgets import ScrollPage, data_tree, title


class RuleAnalysisPage(ScrollPage):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        title(self.body, "規則分析", 0)
        self.tree = data_tree(
            self.body,
            [
                ("name", "規則名稱", 140),
                ("count", "候選數", 70),
                ("sample", "樣本數", 70),
                ("avg", "平均命中", 80),
                ("random", "等候選數隨機值", 110),
                ("excess", "Excess hits", 90),
                ("lift", "Normalized lift", 110),
                ("p", "p-value", 90),
                ("fdr", "FDR/BH", 90),
                ("streak", "最大連續未命中", 120),
                ("status", "狀態", 100),
            ],
            1,
            18,
        )
        self._load()

    def _load(self) -> None:
        for row in rule_rows():
            p_value = row.get("empirical_p_value", "")
            self.tree.insert(
                "",
                "end",
                values=(
                    row.get("rule_name_zh") or row.get("rule_key"),
                    row.get("average_candidate_count", ""),
                    row.get("sample_size", ""),
                    row.get("actual_average_hits", ""),
                    row.get("expected_random_hits", ""),
                    row.get("excess_hits", ""),
                    row.get("normalized_lift", ""),
                    p_value,
                    "通過" if _float(p_value) < 0.05 else "未通過",
                    row.get("max_losing_streak", ""),
                    "研究用",
                ),
            )
        self.tree.insert("", "end", values=("候選集合至少命中一碼比例", "", "", "說明", "", "", "", "", "", "", "此數值受候選數量影響，不能直接代表預測能力"))


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0
