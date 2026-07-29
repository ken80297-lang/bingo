import csv
import json
from pathlib import Path

from desktop.core.phase2_3_prospective import (
    CHECKPOINTS,
    FIXED_TRIGGER_IDS,
    HISTORICAL_LAST_ISSUE,
    PHASE2_2_PREREGISTRATION_HASH,
    PROSPECTIVE_START_ISSUE,
    _registry_sha256,
    export_phase2_3_prospective,
    run_phase2_3_prospective,
)


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"


def test_phase2_3_registry_immutability_and_fixed_triggers():
    result = run_phase2_3_prospective(MASTER)
    registry = result["prospective_registry"]

    assert registry["registry_hash"] == _registry_sha256(registry)
    assert registry["phase2_2_preregistration_hash"] == PHASE2_2_PREREGISTRATION_HASH
    assert [row["trigger_id"] for row in result["trigger_definitions"]] == list(FIXED_TRIGGER_IDS)
    assert all(row["locked_for_phase2_3"] for row in result["trigger_definitions"])
    assert result["current_status"]["trigger_definition_hash"] == result["trigger_definition_hash"]


def test_phase2_3_historical_issue_exclusion_and_no_current_prospective_data():
    result = run_phase2_3_prospective(MASTER)

    assert result["current_status"]["prospective_start_issue"] == PROSPECTIVE_START_ISSUE
    assert result["current_status"]["historical_last_issue"] == HISTORICAL_LAST_ISSUE
    assert result["current_status"]["prospective_targets_loaded"] == 0
    assert result["prediction_snapshots"] == []
    assert result["validation_results"] == []
    assert all(not row["included_in_prospective"] for row in result["issue_audit"])
    assert all(row["historical_excluded"] for row in result["issue_audit"])


def test_phase2_3_pre_result_snapshots_are_eligible(tmp_path):
    path = tmp_path / "future.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 80, count=90)

    result = run_phase2_3_prospective(str(path), snapshot_mode="pre_result")

    assert result["current_status"]["prospective_targets_loaded"] == 9
    assert result["current_status"]["prediction_snapshots"] == 9
    assert all(snapshot["snapshot_created_before_result"] for snapshot in result["prediction_snapshots"])
    assert all(row["eligible_for_primary_analysis"] for row in result["validation_results"])
    assert all(int(snapshot["maximum_feature_issue"]) < int(snapshot["target_issue"]) for snapshot in result["prediction_snapshots"])


def test_phase2_3_retrospective_reconstruction_is_excluded(tmp_path):
    path = tmp_path / "future.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 80, count=90)

    result = run_phase2_3_prospective(str(path), snapshot_mode="retrospective_reconstruction")

    assert result["prediction_snapshots"]
    assert all(not snapshot["snapshot_created_before_result"] for snapshot in result["prediction_snapshots"])
    assert all(not row["eligible_for_primary_analysis"] for row in result["validation_results"])
    assert result["retrospective_reconstruction"]
    assert result["current_status"]["retrospective_reconstruction_count"] == len(result["validation_results"])


def test_phase2_3_checkpoints_and_three_trigger_fdr(tmp_path):
    path = tmp_path / "future.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 250, count=470)

    result = run_phase2_3_prospective(str(path), snapshot_mode="pre_result")

    assert result["checkpoints"]["200"]["status"] == "complete"
    assert result["checkpoints"]["500"]["status"] == "not_reached"
    fdr = result["checkpoints"]["200"]["fdr_bh_results"]
    assert len(fdr) == 3
    assert {row["trigger_id"] for row in fdr} == set(FIXED_TRIGGER_IDS)
    assert all("fdr_bh_threshold" in row for row in fdr)


def test_phase2_3_export_outputs_required_files(tmp_path):
    path = tmp_path / "master.csv"
    output = tmp_path / "phase2_3_prospective"
    phase2_2 = tmp_path / "phase2_2_sparse_triggers"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 10, count=10)
    _write_phase2_2_archive_source(phase2_2)

    result = export_phase2_3_prospective(str(path), output, phase2_2)

    expected = {
        "archive_manifest.json",
        "archive_manifest.sha256",
        "prospective_registry.json",
        "prospective_registry.sha256",
        "trigger_definitions.json",
        "prediction_snapshots.jsonl",
        "validation_results.csv",
        "issue_audit.csv",
        "retrospective_reconstruction.csv",
        "checkpoint_0200.json",
        "checkpoint_0500.json",
        "checkpoint_1000.json",
        "checkpoint_2000.json",
        "current_status.json",
        "prospective_report.txt",
    }
    assert expected == {item.name for item in output.iterdir()}
    assert (phase2_2 / "archive_manifest.json").exists()
    assert (phase2_2 / "archive_manifest.sha256").exists()
    assert result["result"]["current_status"]["backend_modified"] is False


def _write_master(path: Path, start_issue: int, count: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "issue", "time", *[f"n{i:02d}" for i in range(1, 21)], "super", "big_small", "odd_even"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(count):
            numbers = [((index * 5 + i - 1) % 80) + 1 for i in range(1, 21)]
            row = {
                "date": "2026-07-28",
                "issue": str(start_issue + index),
                "time": "10:00",
                "super": str(numbers[index % 20]),
                "big_small": "big",
                "odd_even": "odd",
            }
            row.update({f"n{i:02d}": str(numbers[i - 1]) for i in range(1, 21)})
            writer.writerow(row)


def _write_phase2_2_archive_source(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    summary = {
        "dataset_hash": "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3",
        "preregistration_hash": PHASE2_2_PREREGISTRATION_HASH,
        "final_holdout_only_executed_once": True,
    }
    prereg = {"registered_triggers": []}
    for name in [
        "dataset_split.json",
        "discovery_candidates.csv",
        "discovery_survivors.csv",
        "validation_results.csv",
        "final_holdout_results.csv",
        "trigger_losing_streaks.csv",
        "trigger_daily_stability.csv",
        "candidate_number_concentration.csv",
        "super_trigger_results.csv",
        "multiple_testing_results.csv",
        "phase2_2_report.txt",
    ]:
        (path / name).write_text("", encoding="utf-8")
    (path / "phase2_2_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (path / "preregistration.json").write_text(json.dumps(prereg), encoding="utf-8")
    (path / "preregistration.sha256").write_text(PHASE2_2_PREREGISTRATION_HASH + "\n", encoding="ascii")
