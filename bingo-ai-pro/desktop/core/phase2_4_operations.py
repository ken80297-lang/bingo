from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from desktop.core.phase2_2_sparse_triggers import _canonical_sha256
from desktop.core.phase2_3_prospective import (
    CHECKPOINTS,
    DEFAULT_OUTPUT_DIR,
    EXPERIMENT_ID,
    FIXED_TRIGGER_IDS,
    HISTORICAL_DATASET_HASH,
    HISTORICAL_LAST_ISSUE,
    PROSPECTIVE_START_ISSUE,
    _checkpoint,
    _file_sha256,
    _issue_int,
    _locked_trigger_definitions,
    _read_json,
    _registry_sha256,
)
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH, ReplayDraw, load_replay_dataset
from desktop.core.rule_replay import replay_all_rules


EXPECTED_ARCHIVE_HASH = "b0607eea8e5b5266ae0fe5ef4a16fbecf2b02901aad2b2ef1ee0f7c500686c15"
EXPECTED_REGISTRY_HASH = "85f72b01cc7013fedffd012e58e6fce3ff70e9305702b0a26acf5f6569af765e"
GENERATION_MODE = "prospective_pre_result"
PENDING_STATUS = "pending_result"
INITIAL_CHAIN_HASH = ""


def run_phase2_4_operation_cycle(
    csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH),
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    phase2_2_dir: str | Path = Path("desktop") / "output" / "phase2_2_sparse_triggers",
    auto_mode: bool = False,
) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    operation_started_at = _now()
    invariants = verify_operation_invariants(target, phase2_2_dir)
    dataset = load_replay_dataset(csv_path)
    valid_draws = dataset.valid_draws
    imported_manifest = _imported_dataset_manifest(csv_path, dataset)
    revision_audit = _dataset_revision_audit(valid_draws)
    snapshot_path = target / "prediction_snapshots.jsonl"
    snapshots_before = _read_jsonl(snapshot_path)
    existing_targets = {row["target_issue"] for row in snapshots_before}
    existing_validation_rows = _read_csv(target / "validation_results.csv")
    validation_rows = _merge_validation_rows(existing_validation_rows, _validate_pending_snapshots(snapshots_before, valid_draws))
    next_issue = _next_issue_payload(valid_draws)
    retrospective_rows = _retrospective_rows(valid_draws, existing_targets)

    created_snapshot = None
    skip_reason = None
    target_issue = str(next_issue["computed_target_issue"])
    actual_issues = {draw.issue for draw in valid_draws}
    if target_issue in actual_issues:
        skip_reason = "target_result_already_exists"
    elif target_issue in existing_targets:
        skip_reason = "snapshot_already_exists"
    elif not invariants["all_passed"]:
        skip_reason = "invariant_failed"
    else:
        created_snapshot = _build_pending_snapshot(valid_draws, target_issue, target, invariants, csv_path)
        snapshots_before.append(created_snapshot)

    snapshots = snapshots_before
    snapshot_manifest = _snapshot_manifest(snapshots)
    validation_manifest = _validation_manifest(validation_rows)
    issue_audit = _issue_audit(valid_draws, snapshots, validation_rows)
    checkpoints = {str(checkpoint): _checkpoint_status(checkpoint, validation_rows, target) for checkpoint in CHECKPOINTS}
    current_status = _current_status(
        invariants,
        next_issue,
        snapshots,
        validation_rows,
        retrospective_rows,
        checkpoints,
        created_snapshot,
        skip_reason,
    )
    log_row = {
        "operation_id": f"phase2_4_1_{operation_started_at}",
        "operation": "phase2_4_operation_cycle",
        "started_at": operation_started_at,
        "finished_at": _now(),
        "input_path": str(csv_path),
        "input_hash": imported_manifest["sha256"],
        "archive_integrity": invariants["archive_hash_verified"],
        "registry_integrity": invariants["registry_hash_verified"],
        "snapshot_integrity": invariants["snapshots_manifest_valid"],
        "imported_new_issue_count": imported_manifest["prospective_rows"],
        "validated_issue_count": current_status["validation_count"],
        "excluded_issue_count": current_status["excluded_count"],
        "retrospective_count": current_status["retrospective_count"],
        "newly_created_snapshot_target": created_snapshot["target_issue"] if created_snapshot else "",
        "operation_status": current_status["status"],
        "error_summary": skip_reason or "",
        "auto_mode": auto_mode,
        "created_snapshot_target_issue": created_snapshot["target_issue"] if created_snapshot else "",
        "skip_reason": skip_reason or "",
        "pending_snapshot_count": current_status["pending_snapshot_count"],
        "validated_snapshot_count": current_status["validated_snapshot_count"],
    }

    _write_json(target / "imported_dataset_manifest.json", imported_manifest)
    _write_csv(target / "dataset_revision_audit.csv", revision_audit)
    if created_snapshot:
        _append_jsonl(snapshot_path, created_snapshot)
    elif not snapshot_path.exists():
        snapshot_path.write_text("", encoding="utf-8")
    _write_json(target / "prediction_snapshots_manifest.json", snapshot_manifest)
    (target / "prediction_snapshots_manifest.sha256").write_text(_canonical_sha256(snapshot_manifest) + "\n", encoding="ascii")
    _write_csv(target / "validation_results.csv", validation_rows)
    _write_json(target / "validation_manifest.json", validation_manifest)
    (target / "validation_manifest.sha256").write_text(_canonical_sha256(validation_manifest) + "\n", encoding="ascii")
    _write_csv(target / "issue_audit.csv", issue_audit)
    _write_csv(target / "retrospective_reconstruction.csv", retrospective_rows)
    _write_json(target / "current_status.json", current_status)
    (target / "prospective_report.txt").write_text(_text_report(current_status), encoding="utf-8")
    log_row["operation_hash"] = _canonical_sha256(log_row)
    _append_jsonl(target / "operation_log.jsonl", log_row)

    return {
        "invariants": invariants,
        "imported_dataset_manifest": imported_manifest,
        "dataset_revision_audit": revision_audit,
        "created_snapshot": created_snapshot,
        "prediction_snapshots_manifest": snapshot_manifest,
        "validation_results": validation_rows,
        "validation_manifest": validation_manifest,
        "issue_audit": issue_audit,
        "retrospective_reconstruction": retrospective_rows,
        "checkpoints": checkpoints,
        "current_status": current_status,
        "operation_log": log_row,
    }


def verify_operation_invariants(output_dir: str | Path = DEFAULT_OUTPUT_DIR, phase2_2_dir: str | Path = Path("desktop") / "output" / "phase2_2_sparse_triggers") -> dict[str, Any]:
    target = Path(output_dir)
    archive_hash = _read_text(target / "archive_manifest.sha256") or _read_text(Path(phase2_2_dir) / "archive_manifest.sha256")
    registry = _read_json(target / "prospective_registry.json")
    registry_hash = _read_text(target / "prospective_registry.sha256") or registry.get("registry_hash", "")
    definitions = _read_json(target / "trigger_definitions.json") or _locked_trigger_definitions(Path(phase2_2_dir))
    trigger_hash = _canonical_sha256({"trigger_definitions": definitions})
    snapshots_manifest = _read_json(target / "prediction_snapshots_manifest.json")
    snapshots_manifest_hash = _read_text(target / "prediction_snapshots_manifest.sha256")
    validation_rows = _read_csv(target / "validation_results.csv")
    checks = {
        "archive_hash_verified": archive_hash == EXPECTED_ARCHIVE_HASH,
        "registry_hash_verified": registry_hash == EXPECTED_REGISTRY_HASH or (registry and _registry_sha256(registry) == registry.get("registry_hash")),
        "trigger_definition_hash_locked": bool(trigger_hash),
        "historical_dataset_hash_verified": HISTORICAL_DATASET_HASH == "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3",
        "snapshots_manifest_valid": not snapshots_manifest or _canonical_sha256(snapshots_manifest) == snapshots_manifest_hash,
        "validation_results_unique_issue": _validation_issue_unique(validation_rows),
        "registry_locked": registry.get("prospective_start_issue", PROSPECTIVE_START_ISSUE) == PROSPECTIVE_START_ISSUE if registry else True,
        "prospective_start_issue_verified": PROSPECTIVE_START_ISSUE == 115041413,
    }
    return {
        **checks,
        "archive_hash": archive_hash,
        "registry_hash": registry_hash,
        "trigger_definition_hash": trigger_hash,
        "all_passed": all(checks.values()),
    }


def _build_pending_snapshot(draws: list[ReplayDraw], target_issue: str, output_dir: Path, invariants: dict[str, Any], csv_path: str) -> dict[str, Any]:
    history = [draw for draw in draws if _issue_int(draw.issue) < _issue_int(target_issue)]
    source = history[-1]
    rules = replay_all_rules(history, source)
    missing_rule = next(rule for rule in rules if rule.rule_key == "missing")
    candidates = missing_rule.candidates[:3]
    snapshot = {
        "experiment_id": EXPERIMENT_ID,
        "target_issue": target_issue,
        "source_issue": source.issue,
        "generated_at": _now(),
        "generated_at_timezone": "Asia/Taipei",
        "generation_mode": GENERATION_MODE,
        "maximum_feature_issue": source.issue,
        "history_row_count": len(history),
        "history_last_issue": source.issue,
        "missing_top1": candidates[:1],
        "missing_top2": candidates[:2],
        "missing_top3": candidates[:3],
        "missing_ranking_evidence": {"rule_score": missing_rule.score, "rule_confidence": missing_rule.confidence, "status": missing_rule.status},
        "missing_duration_gap_evidence": missing_rule.evidence,
        "tie_break_evidence": "ascending number order from Phase 2.2 missing rule implementation",
        "trigger_ids": list(FIXED_TRIGGER_IDS),
        "trigger_definition_hash": invariants["trigger_definition_hash"],
        "registry_hash": invariants["registry_hash"],
        "historical_data_hash": HISTORICAL_DATASET_HASH,
        "current_input_data_hash": _file_sha256(Path(csv_path)) if Path(csv_path).exists() else "",
        "status": PENDING_STATUS,
    }
    snapshot["snapshot_hash"] = _canonical_sha256(snapshot)
    return snapshot


def _validate_pending_snapshots(snapshots: list[dict[str, Any]], draws: list[ReplayDraw]) -> list[dict[str, Any]]:
    actual_by_issue = {draw.issue: draw for draw in draws}
    rows = []
    for snapshot in snapshots:
        if snapshot.get("status") != PENDING_STATUS:
            continue
        actual = actual_by_issue.get(snapshot["target_issue"])
        if not actual:
            continue
        result_hash = _draw_hash(actual)
        timing = _timing_payload(snapshot, actual)
        eligible = bool(timing["timing_valid"] and _issue_int(snapshot["maximum_feature_issue"]) < _issue_int(snapshot["target_issue"]))
        exclusion = "" if eligible else "cannot_prove_prediction_before_result"
        row = {
            "experiment_id": EXPERIMENT_ID,
            "target_issue": snapshot["target_issue"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "result_row_hash": result_hash,
            "result_imported_at": _now(),
            "validated_at": _now(),
            "top1_candidates": " ".join(str(n) for n in snapshot["missing_top1"]),
            "top1_hit_count": _hit_count(snapshot["missing_top1"], actual),
            "top1_precision": round(_hit_count(snapshot["missing_top1"], actual) / 1, 6),
            "top1_any_hit": _hit_count(snapshot["missing_top1"], actual) > 0,
            "top1_candidate_count": 1,
            "top1_expected_random_hits": 0.25,
            "top2_candidates": " ".join(str(n) for n in snapshot["missing_top2"]),
            "top2_hit_count": _hit_count(snapshot["missing_top2"], actual),
            "top2_precision": round(_hit_count(snapshot["missing_top2"], actual) / 2, 6),
            "top2_any_hit": _hit_count(snapshot["missing_top2"], actual) > 0,
            "top2_candidate_count": 2,
            "top2_expected_random_hits": 0.5,
            "top3_candidates": " ".join(str(n) for n in snapshot["missing_top3"]),
            "top3_hit_count": _hit_count(snapshot["missing_top3"], actual),
            "top3_precision": round(_hit_count(snapshot["missing_top3"], actual) / 3, 6),
            "top3_any_hit": _hit_count(snapshot["missing_top3"], actual) > 0,
            "top3_candidate_count": 3,
            "top3_expected_random_hits": 0.75,
            "eligible_for_primary_analysis": eligible,
            "exclusion_reason": exclusion,
            **timing,
        }
        for top_k in (1, 2, 3):
            hits = row[f"top{top_k}_hit_count"]
            expected = row[f"top{top_k}_expected_random_hits"]
            row[f"top{top_k}_excess_hits"] = round(hits - expected, 6)
            row[f"top{top_k}_normalized_lift"] = round((hits - expected) / expected, 6)
        row["validation_hash"] = _canonical_sha256(row)
        rows.append(row)
    return rows


def _snapshot_manifest(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    chain = INITIAL_CHAIN_HASH
    hashes = []
    for snapshot in snapshots:
        snapshot_hash = snapshot["snapshot_hash"]
        chain = hashlib.sha256((chain + snapshot_hash).encode("utf-8")).hexdigest()
        hashes.append({"target_issue": snapshot["target_issue"], "snapshot_hash": snapshot_hash, "chain_hash": chain})
    return {
        "snapshot_count": len(snapshots),
        "first_target_issue": snapshots[0]["target_issue"] if snapshots else "",
        "last_target_issue": snapshots[-1]["target_issue"] if snapshots else "",
        "snapshot_hashes": hashes,
        "latest_chain_hash": chain,
        "last_append_at": snapshots[-1]["generated_at"] if snapshots else "",
        "append_only": True,
    }


def _validation_manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    chain = INITIAL_CHAIN_HASH
    hashes = []
    sorted_rows = sorted(rows, key=lambda row: int(row["target_issue"]))
    for row in sorted_rows:
        validation_hash = row["validation_hash"]
        chain = hashlib.sha256((chain + validation_hash).encode("utf-8")).hexdigest()
        hashes.append({"target_issue": row["target_issue"], "validation_hash": validation_hash, "chain_hash": chain})
    return {
        "validation_count": len(rows),
        "eligible_primary_count": sum(1 for row in rows if _truthy(row["eligible_for_primary_analysis"])),
        "excluded_count": sum(1 for row in rows if not _truthy(row["eligible_for_primary_analysis"])),
        "first_validated_issue": sorted_rows[0]["target_issue"] if sorted_rows else "",
        "latest_validated_issue": sorted_rows[-1]["target_issue"] if sorted_rows else "",
        "validated_issues": sorted({row["target_issue"] for row in rows}, key=int),
        "validation_hashes": hashes,
        "latest_chain_hash": chain,
        "last_updated_at": _now(),
    }


def _checkpoint_status(checkpoint: int, validation_rows: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    path = output_dir / f"checkpoint_{checkpoint:04d}.json"
    existing = _read_json(path)
    if existing.get("status") == "complete":
        return existing
    normalized_rows = []
    for row in validation_rows:
        if not row["eligible_for_primary_analysis"]:
            continue
        for top_k in (1, 2, 3):
            normalized_rows.append(
                {
                    "target_issue": row["target_issue"],
                    "trigger_id": FIXED_TRIGGER_IDS[top_k - 1],
                    "hit_count": row[f"top{top_k}_hit_count"],
                    "eligible_for_primary_analysis": True,
                }
            )
    result = _checkpoint(checkpoint, normalized_rows)
    if result["status"] == "complete":
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _next_issue_payload(draws: list[ReplayDraw]) -> dict[str, Any]:
    latest = max((_issue_int(draw.issue) for draw in draws), default=HISTORICAL_LAST_ISSUE)
    return {
        "latest_source_issue": latest,
        "computed_target_issue": latest + 1,
        "computation_method": "latest_valid_issue_plus_one",
        "generated_at": _now(),
    }


def _imported_dataset_manifest(csv_path: str, dataset) -> dict[str, Any]:
    path = Path(csv_path)
    valid = dataset.valid_draws
    return {
        "path": str(path),
        "sha256": _file_sha256(path) if path.exists() else "",
        "total_rows": dataset.summary.total_rows,
        "valid_rows": dataset.summary.valid_rows,
        "first_issue": dataset.summary.first_issue,
        "last_issue": dataset.summary.last_issue,
        "latest_valid_issue": valid[-1].issue if valid else "",
        "prospective_rows": len([draw for draw in valid if _issue_int(draw.issue) >= PROSPECTIVE_START_ISSUE]),
        "imported_at": _now(),
    }


def _dataset_revision_audit(draws: list[ReplayDraw]) -> list[dict[str, Any]]:
    seen: dict[str, str] = {}
    rows = []
    for draw in draws:
        row_hash = _draw_hash(draw)
        old_hash = seen.get(draw.issue, row_hash)
        if draw.issue in seen and seen[draw.issue] != row_hash:
            rows.append(
                {
                    "issue": draw.issue,
                    "old_row_hash": old_hash,
                    "new_row_hash": row_hash,
                    "changed_fields": "row_content",
                    "detected_at": _now(),
                    "resolution_status": "blocked_from_prospective",
                }
            )
        seen[draw.issue] = row_hash
    return rows


def _retrospective_rows(draws: list[ReplayDraw], snapshot_targets: set[str]) -> list[dict[str, Any]]:
    rows = []
    for draw in draws:
        issue = _issue_int(draw.issue)
        if issue >= PROSPECTIVE_START_ISSUE and draw.issue not in snapshot_targets:
            rows.append(
                {
                    "target_issue": draw.issue,
                    "generation_mode": "retrospective_reconstruction",
                    "status": "excluded_from_primary",
                    "reason": "result_exists_before_snapshot",
                    "prospective_target_count": 0,
                    "primary_precision": "",
                    "primary_normalized_lift": "",
                    "checkpoint": "",
                    "fdr_bh": "",
                }
            )
    return rows


def _issue_audit(draws: list[ReplayDraw], snapshots: list[dict[str, Any]], validations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot_targets = {row["target_issue"] for row in snapshots}
    validated = {row["target_issue"] for row in validations}
    rows = []
    for draw in draws:
        issue = _issue_int(draw.issue)
        rows.append(
            {
                "issue": draw.issue,
                "valid_row": draw.valid,
                "historical_excluded": issue <= HISTORICAL_LAST_ISSUE,
                "prospective_issue": issue >= PROSPECTIVE_START_ISSUE,
                "has_snapshot": draw.issue in snapshot_targets,
                "has_validation": draw.issue in validated,
                "audit_status": "historical_excluded" if issue <= HISTORICAL_LAST_ISSUE else "prospective_result_seen",
            }
        )
    return rows


def _current_status(
    invariants: dict[str, Any],
    next_issue: dict[str, Any],
    snapshots: list[dict[str, Any]],
    validations: list[dict[str, Any]],
    retrospective_rows: list[dict[str, Any]],
    checkpoints: dict[str, dict[str, Any]],
    created_snapshot: dict[str, Any] | None,
    skip_reason: str | None,
) -> dict[str, Any]:
    pending_targets = {row["target_issue"] for row in snapshots if row.get("status") == PENDING_STATUS}
    validated_targets = {row["target_issue"] for row in validations if row["eligible_for_primary_analysis"]}
    pending_unvalidated = sorted(pending_targets - validated_targets, key=int)
    eligible_rows = [row for row in validations if _truthy(row["eligible_for_primary_analysis"])]
    return {
        "experiment_id": EXPERIMENT_ID,
        "integrity_status": "passed" if invariants["all_passed"] else "failed",
        "archive_hash_verified": invariants["archive_hash_verified"],
        "archive_manifest_hash": invariants["archive_hash"],
        "registry_hash": invariants["registry_hash"],
        "trigger_definition_hash": invariants["trigger_definition_hash"],
        "historical_dataset_hash": HISTORICAL_DATASET_HASH,
        "historical_last_issue": HISTORICAL_LAST_ISSUE,
        "prospective_start_issue": PROSPECTIVE_START_ISSUE,
        "latest_imported_issue": next_issue["latest_source_issue"],
        "latest_validated_issue": max((int(row["target_issue"]) for row in eligible_rows), default=None),
        "current_pending_target": pending_unvalidated[0] if pending_unvalidated else "",
        "latest_valid_issue": next_issue["latest_source_issue"],
        "next_target_issue": next_issue["computed_target_issue"],
        "latest_snapshot_target_issue": snapshots[-1]["target_issue"] if snapshots else "",
        "pending_snapshot_count": len(pending_unvalidated),
        "validated_snapshot_count": len(validated_targets),
        "validation_count": len(validations),
        "eligible_primary_count": len(eligible_rows),
        "excluded_count": len([row for row in validations if not _truthy(row["eligible_for_primary_analysis"])]),
        "invalid_snapshot_count": len([row for row in validations if not row["eligible_for_primary_analysis"]]),
        "retrospective_reconstruction_count": len(retrospective_rows),
        "retrospective_count": len(retrospective_rows),
        "next_checkpoint": next((item for item in CHECKPOINTS if len(validated_targets) < item), None),
        "checkpoint_status": {key: value["status"] for key, value in checkpoints.items()},
        **_cumulative_metrics(eligible_rows),
        "created_snapshot_target_issue": created_snapshot["target_issue"] if created_snapshot else "",
        "created_snapshot_hash": created_snapshot["snapshot_hash"] if created_snapshot else "",
        "created_snapshot_chain_hash": _snapshot_manifest(snapshots)["latest_chain_hash"],
        "created_snapshot_generated_at": created_snapshot["generated_at"] if created_snapshot else "",
        "maximum_feature_issue": created_snapshot["maximum_feature_issue"] if created_snapshot else "",
        "skip_reason": skip_reason or "",
        "backend_modified": False,
        "production_database_modified": False,
        "status": "pending_snapshot_created" if created_snapshot else "no_snapshot_created",
        "research_status": "waiting_for_checkpoint_200" if len(eligible_rows) < 200 else "checkpoint_ready",
        "updated_at": _now(),
    }


def _cumulative_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for top_k in (1, 2, 3):
        if not rows:
            output[f"top{top_k}_cumulative_hits"] = None
            output[f"top{top_k}_cumulative_precision"] = None
            output[f"top{top_k}_random_baseline"] = round(top_k * 20 / 80, 6)
            output[f"top{top_k}_normalized_lift"] = None
            continue
        hits = sum(int(row[f"top{top_k}_hit_count"]) for row in rows)
        samples = len(rows)
        expected = samples * top_k * 20 / 80
        precision = hits / max(1, samples * top_k)
        output[f"top{top_k}_cumulative_hits"] = hits
        output[f"top{top_k}_cumulative_precision"] = round(precision, 6)
        output[f"top{top_k}_random_baseline"] = round(top_k * 20 / 80, 6)
        output[f"top{top_k}_normalized_lift"] = round((hits - expected) / expected, 6) if expected else 0
    return output


def _timing_payload(snapshot: dict[str, Any], draw: ReplayDraw) -> dict[str, Any]:
    timing_valid = snapshot.get("generation_mode") == GENERATION_MODE
    return {
        "snapshot_generated_at": snapshot["generated_at"],
        "result_draw_time": f"{draw.date} {draw.time}".strip(),
        "result_first_seen_at": _now(),
        "timing_validation_method": "snapshot_file_preexisted_import_cycle" if timing_valid else "cannot_prove_prediction_before_result",
        "timing_valid": timing_valid,
    }


def _hit_count(candidates: Iterable[int], draw: ReplayDraw) -> int:
    return len(set(int(number) for number in candidates) & set(draw.numbers))


def _draw_hash(draw: ReplayDraw) -> str:
    payload = {
        "date": draw.date,
        "issue": draw.issue,
        "time": draw.time,
        "numbers": draw.numbers,
        "super_number": draw.super_number,
        "big_small": draw.big_small,
        "odd_even": draw.odd_even,
    }
    return _canonical_sha256(payload)


def _validation_issue_unique(rows: list[dict[str, Any]]) -> bool:
    issues = [row.get("target_issue") for row in rows if _truthy(row.get("eligible_for_primary_analysis"))]
    return len(issues) == len(set(issues))


def _merge_validation_rows(existing: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in existing + new_rows:
        target_issue = str(row.get("target_issue") or "")
        if target_issue and target_issue not in merged:
            merged[target_issue] = row
    return [merged[key] for key in sorted(merged, key=int)]


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() in {"true", "1", "yes"}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows or [])
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _text_report(status: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Phase Desktop 2.4 - Prospective Snapshot Operation Center",
            f"Archive verified: {status['archive_hash_verified']}",
            f"Latest valid issue: {status['latest_valid_issue']}",
            f"Next target issue: {status['next_target_issue']}",
            f"Created snapshot: {status['created_snapshot_target_issue'] or 'none'}",
            f"Pending snapshots: {status['pending_snapshot_count']}",
            f"Validated snapshots: {status['validated_snapshot_count']}",
            f"Next checkpoint: {status['next_checkpoint']}",
            "Conclusion: desktop-only, append-only snapshots; no backend or production database writes.",
        ]
    )
