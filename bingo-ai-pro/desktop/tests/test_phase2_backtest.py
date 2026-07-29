import csv
from pathlib import Path

from desktop.core.phase2_export import export_phase2_report
from desktop.core.phase2_backtest import run_phase2_backtest
from desktop.core.replay_dataset import load_replay_dataset
from desktop.core.rule_replay import replay_all_rules


def test_phase2_backtest_report_and_no_lookahead(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=35)

    report = run_phase2_backtest(str(path), min_history=20)

    assert report["dataset"]["total_rows"] == 35
    assert report["valid_simulations"] == 15
    assert report["no_look_ahead"] is True
    assert "average_hits" in report
    assert "average_high5_hits" in report
    assert report["best_rule"] is not None
    assert report["worst_rule"] is not None
    assert report["rule_performance"]
    assert "high_confidence_strategy" in report["high_confidence"]
    assert "baseline_comparison" in report
    assert report["holdout"]["holdout_count"] > 0


def test_rule_replay_returns_fixed_rule_set_in_memory_only(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=25)
    dataset = load_replay_dataset(path)
    history = dataset.valid_draws[:20]

    rules = replay_all_rules(history, history[-1])

    assert len(rules) == 21
    assert [rule.rule_key for rule in rules][:3] == ["hot", "cold", "missing"]
    assert all(rule.status in {"ok", "empty"} for rule in rules)
    assert all(isinstance(rule.candidates, list) for rule in rules)


def test_phase2_actual_master_draws_report_and_audit():
    path = Path(r"C:\Users\ken80297\Desktop\master_draws.csv")

    report = run_phase2_backtest(str(path), min_history=100)

    assert path.exists()
    assert report["dataset"]["total_rows"] == 6090
    assert report["dataset"]["valid_rows"] == 6090
    assert report["dataset"]["warmup_rows"] == 100
    assert report["valid_simulations"] == 5990
    assert report["invalid_prediction_count"] == 0
    assert report["no_look_ahead"] is True
    assert all(row["passed"] for row in report["look_ahead_audit"])
    assert report["baseline_comparison"]["random_expected_20_hits"] == 5.0
    assert report["baseline_comparison"]["random_expected_high5_hits"] == 1.25


def test_phase2_export_outputs_required_files(tmp_path):
    path = Path(r"C:\Users\ken80297\Desktop\master_draws.csv")
    output_dir = tmp_path / "phase2_30day"

    result = export_phase2_report(str(path), output_dir, min_history=100)

    assert result["report"]["valid_simulations"] == 5990
    expected = {
        "dataset_validation.json",
        "backtest_summary.json",
        "backtest_by_issue.csv",
        "daily_summary.csv",
        "rule_performance.csv",
        "high_confidence_conditions.csv",
        "baseline_comparison.csv",
        "look_ahead_audit.csv",
        "phase2_report.txt",
    }
    assert expected == {item.name for item in output_dir.iterdir()}


def _write_master(path, count):
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "issue", "time", *[f"n{i:02d}" for i in range(1, 21)], "super", "big_small", "odd_even"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(count):
            numbers = [((index * 3 + i - 1) % 80) + 1 for i in range(1, 21)]
            row = {
                "date": "2026-01-01",
                "issue": str(200000 + index),
                "time": "10:00",
                "super": str(numbers[index % 20]),
                "big_small": "\u504f\u5927" if sum(1 for number in numbers if number >= 41) >= 10 else "\u504f\u5c0f",
                "odd_even": "\u55ae" if sum(1 for number in numbers if number % 2) > 10 else "\u96d9" if sum(1 for number in numbers if number % 2) < 10 else "\u5747\u8861",
            }
            row.update({f"n{i:02d}": str(numbers[i - 1]) for i in range(1, 21)})
            writer.writerow(row)
