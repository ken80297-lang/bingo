import csv
import json
from pathlib import Path

from desktop.core.phase2_4_operations import (
    EXPECTED_ARCHIVE_HASH,
    EXPECTED_REGISTRY_HASH,
    HISTORICAL_LAST_ISSUE,
    PROSPECTIVE_START_ISSUE,
    run_phase2_4_operation_cycle,
)
from desktop.core.phase2_3_prospective import PHASE2_2_PREREGISTRATION_HASH


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"
SNAPSHOT_HASH = "11910e9d8c06ec746c4a727ecc04bd80b540158ff863dc5fb0beb5c75b496e8f"


def test_phase2_4_1_current_master_waits_for_115041413(tmp_path):
    output = _operation_output(tmp_path)
    _seed_snapshot(output)

    before = (output / "prediction_snapshots.jsonl").read_text(encoding="utf-8")
    result = run_phase2_4_operation_cycle(MASTER, output, _phase2_2_source(tmp_path))
    after = (output / "prediction_snapshots.jsonl").read_text(encoding="utf-8")

    assert before == after
    assert result["created_snapshot"] is None
    assert result["current_status"]["skip_reason"] == "snapshot_already_exists"
    assert result["current_status"]["pending_snapshot_count"] == 1
    assert result["validation_manifest"]["validation_count"] == 0
    assert result["current_status"]["next_target_issue"] == PROSPECTIVE_START_ISSUE


def test_phase2_4_1_snapshot_candidates_and_hash_persisted(tmp_path):
    output = _operation_output(tmp_path)
    _seed_snapshot(output)

    result = run_phase2_4_operation_cycle(MASTER, output, _phase2_2_source(tmp_path))
    snapshot = json.loads((output / "prediction_snapshots.jsonl").read_text(encoding="utf-8").strip())

    assert snapshot["missing_top1"] == [64]
    assert snapshot["missing_top2"] == [64, 46]
    assert snapshot["missing_top3"] == [64, 46, 35]
    assert snapshot["snapshot_hash"] == SNAPSHOT_HASH
    assert result["prediction_snapshots_manifest"]["snapshot_hashes"][0]["snapshot_hash"] == SNAPSHOT_HASH


def test_phase2_4_1_validates_115041413_and_appends_115041414(tmp_path):
    output = _operation_output(tmp_path)
    _seed_snapshot(output)
    path = tmp_path / "future.csv"
    _write_master_with_115041413(path)
    original_first_line = (output / "prediction_snapshots.jsonl").read_text(encoding="utf-8").splitlines()[0]

    result = run_phase2_4_operation_cycle(str(path), output, _phase2_2_source(tmp_path))
    lines = (output / "prediction_snapshots.jsonl").read_text(encoding="utf-8").splitlines()

    assert lines[0] == original_first_line
    assert len(lines) == 2
    assert result["validation_manifest"]["validation_count"] == 1
    assert result["validation_manifest"]["eligible_primary_count"] == 1
    assert result["validation_manifest"]["latest_chain_hash"]
    validation = result["validation_results"][0]
    assert validation["target_issue"] == str(PROSPECTIVE_START_ISSUE)
    assert validation["snapshot_hash"] == SNAPSHOT_HASH
    assert validation["top1_expected_random_hits"] == 0.25
    assert validation["top2_expected_random_hits"] == 0.5
    assert validation["top3_expected_random_hits"] == 0.75
    assert validation["eligible_for_primary_analysis"] is True
    assert result["created_snapshot"]["target_issue"] == str(PROSPECTIVE_START_ISSUE + 1)
    assert result["created_snapshot"]["source_issue"] == str(PROSPECTIVE_START_ISSUE)


def test_phase2_4_1_duplicate_validation_rejected(tmp_path):
    output = _operation_output(tmp_path)
    _seed_snapshot(output)
    path = tmp_path / "future.csv"
    _write_master_with_115041413(path)

    first = run_phase2_4_operation_cycle(str(path), output, _phase2_2_source(tmp_path))
    second = run_phase2_4_operation_cycle(str(path), output, _phase2_2_source(tmp_path))

    assert first["validation_manifest"]["validation_count"] == 1
    assert second["validation_manifest"]["validation_count"] == 1
    assert len({row["target_issue"] for row in second["validation_results"]}) == 1


def test_phase2_4_1_multiple_new_issues_without_snapshot_are_retrospective(tmp_path):
    output = _operation_output(tmp_path)
    _seed_snapshot(output)
    path = tmp_path / "future_many.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 4, count=9)

    result = run_phase2_4_operation_cycle(str(path), output, _phase2_2_source(tmp_path))

    retrospective_targets = {row["target_issue"] for row in result["retrospective_reconstruction"]}
    assert str(PROSPECTIVE_START_ISSUE + 1) in retrospective_targets
    assert str(PROSPECTIVE_START_ISSUE + 3) in retrospective_targets
    assert result["created_snapshot"]["target_issue"] == str(PROSPECTIVE_START_ISSUE + 4)


def _operation_output(tmp_path: Path) -> Path:
    output = tmp_path / "phase2_3_prospective"
    output.mkdir(parents=True, exist_ok=True)
    (output / "archive_manifest.sha256").write_text(EXPECTED_ARCHIVE_HASH + "\n", encoding="ascii")
    (output / "prospective_registry.sha256").write_text(EXPECTED_REGISTRY_HASH + "\n", encoding="ascii")
    return output


def _phase2_2_source(tmp_path: Path) -> Path:
    path = tmp_path / "phase2_2_sparse_triggers"
    path.mkdir(parents=True, exist_ok=True)
    summary = {
        "dataset_hash": "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3",
        "preregistration_hash": PHASE2_2_PREREGISTRATION_HASH,
        "final_holdout_only_executed_once": True,
    }
    (path / "phase2_2_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (path / "preregistration.json").write_text(json.dumps({"registered_triggers": []}), encoding="utf-8")
    (path / "preregistration.sha256").write_text(PHASE2_2_PREREGISTRATION_HASH + "\n", encoding="ascii")
    return path


def _seed_snapshot(output: Path) -> None:
    snapshot = {
        "current_input_data_hash": "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3",
        "experiment_id": "phase_desktop_2_3_prospective_triggers",
        "generated_at": "2026-07-28T15:31:19+08:00",
        "generated_at_timezone": "Asia/Taipei",
        "generation_mode": "prospective_pre_result",
        "historical_data_hash": "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3",
        "history_last_issue": "115041412",
        "history_row_count": 6090,
        "maximum_feature_issue": "115041412",
        "missing_duration_gap_evidence": {},
        "missing_ranking_evidence": {"rule_confidence": 1, "rule_score": 0.65, "status": "ok"},
        "missing_top1": [64],
        "missing_top2": [64, 46],
        "missing_top3": [64, 46, 35],
        "registry_hash": EXPECTED_REGISTRY_HASH,
        "snapshot_hash": SNAPSHOT_HASH,
        "source_issue": "115041412",
        "status": "pending_result",
        "target_issue": "115041413",
        "tie_break_evidence": "ascending number order from Phase 2.2 missing rule implementation",
        "trigger_definition_hash": "0d0a92f0d0278a468764bf8670e7c2ba64c6f60c5841fc95d9cdc6dc6ba27eee",
        "trigger_ids": [
            "rule__missing__top1__score0_00__conf0_00",
            "rule__missing__top2__score0_00__conf0_00",
            "rule__missing__top3__score0_00__conf0_00",
        ],
    }
    (output / "prediction_snapshots.jsonl").write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _write_master_with_115041413(path: Path) -> None:
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 4, count=6)


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
