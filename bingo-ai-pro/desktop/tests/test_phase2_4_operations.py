import csv
import json
from pathlib import Path

from desktop.core.phase2_4_operations import (
    EXPECTED_ARCHIVE_HASH,
    EXPECTED_REGISTRY_HASH,
    HISTORICAL_LAST_ISSUE,
    PROSPECTIVE_START_ISSUE,
    _snapshot_manifest,
    run_phase2_4_operation_cycle,
    verify_operation_invariants,
)
from desktop.core.phase2_3_prospective import PHASE2_2_PREREGISTRATION_HASH


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"


def test_phase2_4_first_prospective_snapshot(tmp_path):
    output = _operation_output(tmp_path)

    result = run_phase2_4_operation_cycle(MASTER, output, _phase2_2_source(tmp_path))
    snapshot = result["created_snapshot"]

    assert result["invariants"]["all_passed"] is True
    assert snapshot["target_issue"] == str(PROSPECTIVE_START_ISSUE)
    assert snapshot["source_issue"] == str(HISTORICAL_LAST_ISSUE)
    assert snapshot["maximum_feature_issue"] == str(HISTORICAL_LAST_ISSUE)
    assert snapshot["generation_mode"] == "prospective_pre_result"
    assert snapshot["status"] == "pending_result"
    assert result["current_status"]["pending_snapshot_count"] == 1
    assert result["prediction_snapshots_manifest"]["snapshot_count"] == 1
    assert result["prediction_snapshots_manifest"]["latest_chain_hash"]


def test_phase2_4_duplicate_snapshot_rejected(tmp_path):
    output = _operation_output(tmp_path)
    phase2_2 = _phase2_2_source(tmp_path)

    first = run_phase2_4_operation_cycle(MASTER, output, phase2_2)
    second = run_phase2_4_operation_cycle(MASTER, output, phase2_2)

    assert first["created_snapshot"] is not None
    assert second["created_snapshot"] is None
    assert second["current_status"]["skip_reason"] == "snapshot_already_exists"
    assert second["prediction_snapshots_manifest"]["snapshot_count"] == 1


def test_phase2_4_snapshot_hash_chain_append_only(tmp_path):
    output = _operation_output(tmp_path)
    phase2_2 = _phase2_2_source(tmp_path)
    first = run_phase2_4_operation_cycle(MASTER, output, phase2_2)
    original_manifest = first["prediction_snapshots_manifest"]

    snapshots = [json.loads(line) for line in (output / "prediction_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    recomputed = _snapshot_manifest(snapshots)

    assert recomputed["latest_chain_hash"] == original_manifest["latest_chain_hash"]
    assert recomputed["snapshot_hashes"] == original_manifest["snapshot_hashes"]


def test_phase2_4_result_before_snapshot_excluded(tmp_path):
    output = _operation_output(tmp_path)
    path = tmp_path / "future_exists.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 4, count=6)

    result = run_phase2_4_operation_cycle(str(path), output, _phase2_2_source(tmp_path))

    assert result["created_snapshot"]["target_issue"] == str(PROSPECTIVE_START_ISSUE + 1)
    assert result["retrospective_reconstruction"]
    assert result["current_status"]["retrospective_reconstruction_count"] == 1


def test_phase2_4_pending_snapshot_validation(tmp_path):
    output = _operation_output(tmp_path)
    phase2_2 = _phase2_2_source(tmp_path)
    run_phase2_4_operation_cycle(MASTER, output, phase2_2)
    path = tmp_path / "future_after_snapshot.csv"
    _write_master(path, start_issue=HISTORICAL_LAST_ISSUE - 4, count=6)

    result = run_phase2_4_operation_cycle(str(path), output, phase2_2)

    assert result["created_snapshot"]["target_issue"] == str(PROSPECTIVE_START_ISSUE + 1)
    assert len(result["validation_results"]) == 1
    row = result["validation_results"][0]
    assert row["target_issue"] == str(PROSPECTIVE_START_ISSUE)
    assert row["eligible_for_primary_analysis"] is True
    assert row["validation_hash"]


def test_phase2_4_issue_progression_and_checkpoint_not_reached(tmp_path):
    output = _operation_output(tmp_path)

    result = run_phase2_4_operation_cycle(MASTER, output, _phase2_2_source(tmp_path))

    assert result["current_status"]["latest_valid_issue"] == HISTORICAL_LAST_ISSUE
    assert result["current_status"]["next_target_issue"] == PROSPECTIVE_START_ISSUE
    assert result["checkpoints"]["200"]["status"] == "not_reached"
    assert result["current_status"]["next_checkpoint"] == 200


def test_phase2_4_invariant_verification(tmp_path):
    output = _operation_output(tmp_path)

    invariants = verify_operation_invariants(output, _phase2_2_source(tmp_path))

    assert invariants["archive_hash_verified"] is True
    assert invariants["registry_hash_verified"] is True
    assert invariants["prospective_start_issue_verified"] is True


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
    prereg = {"registered_triggers": []}
    (path / "phase2_2_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (path / "preregistration.json").write_text(json.dumps(prereg), encoding="utf-8")
    (path / "preregistration.sha256").write_text(PHASE2_2_PREREGISTRATION_HASH + "\n", encoding="ascii")
    return path


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
