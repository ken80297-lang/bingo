import csv
import json
from pathlib import Path

from desktop.core.phase2_2_sparse_triggers import (
    _canonical_sha256,
    analyze_sparse_triggers,
    export_phase2_2_sparse_triggers,
    run_phase2_2_sparse_triggers,
)
from desktop.core.phase2_backtest import run_phase2_backtest


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"


def test_phase2_2_split_isolation_and_no_lookahead(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=180)
    phase2 = run_phase2_backtest(str(path), min_history=30)

    result = analyze_sparse_triggers(phase2, str(path), min_discovery_count=5, min_validation_count=5, min_final_count=5)

    split = result["dataset_split"]
    assert split["discovery"]["count"] == 75
    assert split["validation"]["count"] == 37
    assert split["final_holdout"]["count"] == 38
    assert int(split["discovery"]["last_issue"]) < int(split["validation"]["first_issue"])
    assert int(split["validation"]["last_issue"]) < int(split["final_holdout"]["first_issue"])
    assert result["phase2_2_summary"]["final_holdout_unread_before_preregistration"] is True
    assert result["phase2_2_summary"]["no_look_ahead"] is True


def test_phase2_2_sparse_candidate_count_and_low_sample_rejection(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=170)
    phase2 = run_phase2_backtest(str(path), min_history=30)

    result = analyze_sparse_triggers(phase2, str(path), min_discovery_count=9999, min_validation_count=9999, min_final_count=9999)

    assert result["discovery_candidates"]
    assert all(row["candidate_count"] <= 5 for row in result["discovery_candidates"])
    assert result["discovery_survivors"] == []
    assert result["validation_results"] == []
    assert result["preregistration"]["registered_triggers"] == []
    assert result["final_holdout_results"] == []


def test_phase2_2_preregistration_hash_is_immutable(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=180)
    phase2 = run_phase2_backtest(str(path), min_history=30)

    result = analyze_sparse_triggers(phase2, str(path), min_discovery_count=5, min_validation_count=5, min_final_count=5)
    prereg = result["preregistration"]

    assert prereg["preregistration_hash"] == _canonical_sha256(prereg)
    mutated = json.loads(json.dumps(prereg))
    mutated["random_seed"] = 1
    assert prereg["preregistration_hash"] != _canonical_sha256(mutated)


def test_phase2_2_export_outputs_required_files(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_master(path, count=180)
    output_dir = tmp_path / "phase2_2_sparse_triggers"

    result = export_phase2_2_sparse_triggers(str(path), output_dir, min_history=30)

    assert result["result"]["phase2_2_summary"]["final_holdout_only_executed_once"] is True
    expected = {
        "dataset_split.json",
        "discovery_candidates.csv",
        "discovery_survivors.csv",
        "validation_results.csv",
        "preregistration.json",
        "preregistration.sha256",
        "final_holdout_results.csv",
        "trigger_losing_streaks.csv",
        "trigger_daily_stability.csv",
        "candidate_number_concentration.csv",
        "super_trigger_results.csv",
        "multiple_testing_results.csv",
        "phase2_2_summary.json",
        "phase2_2_report.txt",
    }
    assert expected == {item.name for item in output_dir.iterdir()}


def test_phase2_2_actual_master_smoke():
    path = Path(MASTER)

    result = run_phase2_2_sparse_triggers(str(path))

    summary = result["phase2_2_summary"]
    assert path.exists()
    assert summary["dataset_total_rows"] == 6090
    assert summary["replay_valid_simulations"] == 5990
    assert summary["split"]["discovery"]["count"] == 2995
    assert summary["split"]["validation"]["count"] == 1497
    assert summary["split"]["final_holdout"]["count"] == 1498
    assert summary["final_holdout_only_executed_once"] is True
    assert summary["final_holdout_unread_before_preregistration"] is True
    assert summary["no_look_ahead"] is True


def _write_master(path, count):
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "issue", "time", *[f"n{i:02d}" for i in range(1, 21)], "super", "big_small", "odd_even"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(count):
            numbers = [((index * 7 + i - 1) % 80) + 1 for i in range(1, 21)]
            row = {
                "date": f"2026-01-{(index // 200) + 1:02d}",
                "issue": str(300000 + index),
                "time": "10:00",
                "super": str(numbers[index % 20]),
                "big_small": "\u504f\u5927" if sum(1 for number in numbers if number >= 41) >= 10 else "\u504f\u5c0f",
                "odd_even": "\u55ae" if sum(1 for number in numbers if number % 2) > 10 else "\u96d9" if sum(1 for number in numbers if number % 2) < 10 else "\u5747\u8861",
            }
            row.update({f"n{i:02d}": str(numbers[i - 1]) for i in range(1, 21)})
            writer.writerow(row)
