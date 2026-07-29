from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable

from desktop.core.phase2_1_validation import BOOTSTRAP_SEED, _two_sided_normal_p
from desktop.core.phase2_2_sparse_triggers import _canonical_sha256
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH, ReplayDraw, load_replay_dataset
from desktop.core.rule_replay import replay_all_rules


PHASE2_2_DIR = Path("desktop") / "output" / "phase2_2_sparse_triggers"
DEFAULT_OUTPUT_DIR = Path("desktop") / "output" / "phase2_3_prospective"
EXPERIMENT_ID = "phase_desktop_2_3_prospective_triggers"
CREATED_AT = "2026-07-28T00:00:00+08:00"
HISTORICAL_LAST_ISSUE = 115041412
PROSPECTIVE_START_ISSUE = 115041413
HISTORICAL_DATASET_HASH = "90ce402695af06973413e53e5bd2c93e7d2a0270d4a6de9d476c785c81464fd3"
PHASE2_2_PREREGISTRATION_HASH = "509ac2e2c6d35e7c154b0e8ad023ec3cf9b8f493b23827efad770bf0a931eaed"
CHECKPOINTS = (200, 500, 1000, 2000)
FIXED_TRIGGER_IDS = (
    "rule__missing__top1__score0_00__conf0_00",
    "rule__missing__top2__score0_00__conf0_00",
    "rule__missing__top3__score0_00__conf0_00",
)
PHASE2_2_REQUIRED_FILES = (
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
)


def export_phase2_3_prospective(
    csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH),
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    phase2_2_dir: str | Path = PHASE2_2_DIR,
    snapshot_mode: str = "retrospective_reconstruction",
) -> dict[str, Any]:
    result = run_phase2_3_prospective(csv_path, phase2_2_dir, snapshot_mode=snapshot_mode)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    phase2_2_target = Path(phase2_2_dir)
    _write_json(phase2_2_target / "archive_manifest.json", result["archive_manifest"])
    (phase2_2_target / "archive_manifest.sha256").write_text(result["archive_manifest_hash"] + "\n", encoding="ascii")

    _write_json(target / "archive_manifest.json", result["archive_manifest"])
    (target / "archive_manifest.sha256").write_text(result["archive_manifest_hash"] + "\n", encoding="ascii")
    _write_json(target / "prospective_registry.json", result["prospective_registry"])
    (target / "prospective_registry.sha256").write_text(result["prospective_registry_hash"] + "\n", encoding="ascii")
    _write_json(target / "trigger_definitions.json", result["trigger_definitions"])
    _write_jsonl(target / "prediction_snapshots.jsonl", result["prediction_snapshots"])
    _write_csv(target / "validation_results.csv", result["validation_results"])
    _write_csv(target / "issue_audit.csv", result["issue_audit"])
    _write_csv(target / "retrospective_reconstruction.csv", result["retrospective_reconstruction"])
    for checkpoint in CHECKPOINTS:
        _write_json(target / f"checkpoint_{checkpoint:04d}.json", result["checkpoints"][str(checkpoint)])
    _write_json(target / "current_status.json", result["current_status"])
    (target / "prospective_report.txt").write_text(_text_report(result), encoding="utf-8")
    return {"output_dir": str(target), "result": result}


def run_phase2_3_prospective(
    csv_path: str = str(DEFAULT_MASTER_DRAWS_PATH),
    phase2_2_dir: str | Path = PHASE2_2_DIR,
    snapshot_mode: str = "retrospective_reconstruction",
) -> dict[str, Any]:
    phase2_2_path = Path(phase2_2_dir)
    archive_manifest = build_phase2_2_archive_manifest(phase2_2_path)
    archive_hash = _canonical_sha256(archive_manifest)
    trigger_definitions = _locked_trigger_definitions(phase2_2_path)
    trigger_definition_hash = _canonical_sha256({"trigger_definitions": trigger_definitions})
    registry = _prospective_registry(trigger_definitions, trigger_definition_hash, archive_hash)
    registry_hash = _registry_sha256(registry)
    registry["registry_hash"] = registry_hash

    dataset = load_replay_dataset(csv_path)
    historical = [draw for draw in dataset.valid_draws if _issue_int(draw.issue) <= HISTORICAL_LAST_ISSUE]
    prospective = [draw for draw in dataset.valid_draws if _issue_int(draw.issue) >= PROSPECTIVE_START_ISSUE]
    issue_audit = _issue_audit(dataset.valid_draws, prospective)
    snapshots = _prediction_snapshots(historical, prospective, trigger_definitions, trigger_definition_hash, snapshot_mode)
    validation = _validate_snapshots(snapshots, prospective)
    reconstruction = [row for row in validation if not row["eligible_for_primary_analysis"]]
    checkpoints = {str(checkpoint): _checkpoint(checkpoint, validation) for checkpoint in CHECKPOINTS}
    current = _current_status(
        archive_hash,
        registry_hash,
        trigger_definition_hash,
        prospective,
        snapshots,
        validation,
        checkpoints,
    )
    return {
        "archive_manifest": archive_manifest,
        "archive_manifest_hash": archive_hash,
        "prospective_registry": registry,
        "prospective_registry_hash": registry_hash,
        "trigger_definitions": trigger_definitions,
        "trigger_definition_hash": trigger_definition_hash,
        "prediction_snapshots": snapshots,
        "validation_results": validation,
        "issue_audit": issue_audit,
        "retrospective_reconstruction": reconstruction,
        "checkpoints": checkpoints,
        "current_status": current,
    }


def build_phase2_2_archive_manifest(phase2_2_dir: str | Path = PHASE2_2_DIR) -> dict[str, Any]:
    root = Path(phase2_2_dir)
    files = []
    for name in PHASE2_2_REQUIRED_FILES:
        path = root / name
        files.append(
            {
                "file_name": name,
                "relative_path": name,
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "sha256": _file_sha256(path) if path.exists() else "",
                "exists": path.exists(),
            }
        )
    summary = _read_json(root / "phase2_2_summary.json")
    return {
        "experiment_id": "phase_desktop_2_2_sparse_triggers",
        "created_at": CREATED_AT,
        "research_only": True,
        "dataset_hash": summary.get("dataset_hash", HISTORICAL_DATASET_HASH),
        "preregistration_hash": summary.get("preregistration_hash", PHASE2_2_PREREGISTRATION_HASH),
        "historical_last_issue": HISTORICAL_LAST_ISSUE,
        "final_holdout_executed_once": bool(summary.get("final_holdout_only_executed_once")),
        "sealed_dataset_reuse_allowed": False,
        "files": files,
    }


def _locked_trigger_definitions(phase2_2_dir: Path) -> list[dict[str, Any]]:
    prereg = _read_json(phase2_2_dir / "preregistration.json")
    registered = {row.get("trigger_id"): row for row in prereg.get("registered_triggers", [])}
    definitions = []
    for trigger_id in FIXED_TRIGGER_IDS:
        row = dict(registered.get(trigger_id) or {})
        if not row:
            top_k = int(trigger_id.split("__top", 1)[1].split("__", 1)[0])
            row = {
                "trigger_id": trigger_id,
                "trigger_name_zh": f"缺號 Top {top_k}",
                "trigger_version": "2.3.0",
                "trigger_family": "rule",
                "source": "rule",
                "rule_key": "missing",
                "rule_name_zh": "缺號",
                "top_k": top_k,
                "min_score": 0.0,
                "min_confidence": 0.0,
                "conditions": json.dumps({"rule_key": "missing", "top_k": top_k, "min_score": 0.0, "min_confidence": 0.0}, sort_keys=True),
                "research_only": True,
            }
        row["locked_for_phase2_3"] = True
        row["threshold_note"] = "score0_00/conf0_00 are inherited from Phase 2.2 and mean no additional score or confidence gate is applied."
        row["candidate_generation_locked"] = "Use the Phase 2.2 missing rule implementation; no lookback, tie-break, score, confidence, or ranking parameter may be changed."
        definitions.append(row)
    return definitions


def _prospective_registry(definitions: list[dict[str, Any]], trigger_hash: str, archive_hash: str) -> dict[str, Any]:
    registry = {
        "experiment_id": EXPERIMENT_ID,
        "created_at": CREATED_AT,
        "research_only": True,
        "prospective_start_issue": PROSPECTIVE_START_ISSUE,
        "historical_last_issue": HISTORICAL_LAST_ISSUE,
        "historical_dataset_hash": HISTORICAL_DATASET_HASH,
        "phase2_2_preregistration_hash": PHASE2_2_PREREGISTRATION_HASH,
        "phase2_2_archive_manifest_hash": archive_hash,
        "fixed_trigger_ids": list(FIXED_TRIGGER_IDS),
        "trigger_definitions": definitions,
        "trigger_definition_hash": trigger_hash,
        "primary_metric": "normalized_lift versus equal-size random",
        "secondary_metrics": ["average_hits", "precision", "any_hit_rate", "bootstrap_95_ci", "empirical_p_value", "max_losing_streak"],
        "minimum_sample_sizes": {"checkpoint": 200, "strict_success": 1000},
        "evaluation_checkpoints": list(CHECKPOINTS),
        "success_criteria": {
            "minimum_prospective_targets": 1000,
            "normalized_lift_gt": 0,
            "top1_precision_minimum": 0.27,
            "normalized_lift_minimum": 0.08,
            "fdr_bh_required": True,
            "snapshot_created_before_result_required": True,
        },
        "failure_criteria": [
            "1000 prospective targets with normalized_lift <= 0",
            "precision falls below equal-size random baseline",
            "FDR/BH correction fails at strict checkpoint",
            "snapshot missing or retrospective reconstruction only",
            "trigger definition hash changes",
        ],
        "random_seed": BOOTSTRAP_SEED,
        "registry_hash": "",
    }
    registry["registry_hash"] = _registry_sha256(registry)
    return registry


def _prediction_snapshots(
    historical: list[ReplayDraw],
    prospective: list[ReplayDraw],
    definitions: list[dict[str, Any]],
    trigger_hash: str,
    snapshot_mode: str,
) -> list[dict[str, Any]]:
    snapshots = []
    history = list(historical)
    for target in prospective:
        source = history[-1] if history else None
        if source is None or _issue_int(source.issue) >= _issue_int(target.issue):
            continue
        rules = replay_all_rules(history, source)
        missing_rule = next(rule for rule in rules if rule.rule_key == "missing")
        candidates = missing_rule.candidates[:3]
        snapshot = {
            "experiment_id": EXPERIMENT_ID,
            "target_issue": target.issue,
            "source_issue": source.issue,
            "generated_at": CREATED_AT,
            "maximum_feature_issue": source.issue,
            "missing_top1": candidates[:1],
            "missing_top2": candidates[:2],
            "missing_top3": candidates[:3],
            "candidate_generation_evidence": {
                "rule_key": "missing",
                "source_history_count": len(history),
                "source_last_issue": source.issue,
                "score_threshold": 0.0,
                "confidence_threshold": 0.0,
            },
            "historical_data_hash": HISTORICAL_DATASET_HASH,
            "trigger_definition_hash": trigger_hash,
            "status": "pending_result" if snapshot_mode == "pre_result" else "retrospective_reconstruction",
            "snapshot_created_before_result": snapshot_mode == "pre_result",
        }
        snapshot["snapshot_hash"] = _canonical_sha256(snapshot)
        snapshots.append(snapshot)
        history.append(target)
    return snapshots


def _validate_snapshots(snapshots: list[dict[str, Any]], prospective: list[ReplayDraw]) -> list[dict[str, Any]]:
    actual_by_issue = {draw.issue: draw for draw in prospective}
    rows = []
    for snapshot in snapshots:
        actual = actual_by_issue.get(snapshot["target_issue"])
        if not actual:
            continue
        for trigger_id in FIXED_TRIGGER_IDS:
            top_k = int(trigger_id.split("__top", 1)[1].split("__", 1)[0])
            candidates = snapshot[f"missing_top{top_k}"]
            hits = len(set(candidates) & set(actual.numbers))
            expected = top_k * 20 / 80
            eligible = (
                snapshot["snapshot_created_before_result"]
                and _issue_int(snapshot["maximum_feature_issue"]) < _issue_int(snapshot["target_issue"])
                and _issue_int(snapshot["target_issue"]) >= PROSPECTIVE_START_ISSUE
            )
            rows.append(
                {
                    "target_issue": snapshot["target_issue"],
                    "trigger_id": trigger_id,
                    "candidates": " ".join(str(number) for number in candidates),
                    "candidate_count": top_k,
                    "actual_numbers": " ".join(str(number) for number in actual.numbers),
                    "hit_count": hits,
                    "precision": round(hits / top_k, 6),
                    "any_hit": hits > 0,
                    "expected_random_hits": expected,
                    "excess_hits": round(hits - expected, 6),
                    "normalized_lift": round((hits - expected) / expected, 6),
                    "snapshot_created_before_result": snapshot["snapshot_created_before_result"],
                    "eligible_for_primary_analysis": eligible,
                    "snapshot_hash": snapshot["snapshot_hash"],
                }
            )
    return rows


def _checkpoint(checkpoint: int, validation_rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible_issues = sorted({row["target_issue"] for row in validation_rows if row["eligible_for_primary_analysis"]}, key=int)
    if len(eligible_issues) < checkpoint:
        return {
            "checkpoint": checkpoint,
            "status": "not_reached",
            "eligible_prospective_targets": len(eligible_issues),
            "required_targets": checkpoint,
            "trigger_results": [],
            "fdr_bh_results": [],
        }
    issue_set = set(eligible_issues[:checkpoint])
    rows = [row for row in validation_rows if row["target_issue"] in issue_set and row["eligible_for_primary_analysis"]]
    trigger_results = [_trigger_metric(trigger_id, [row for row in rows if row["trigger_id"] == trigger_id]) for trigger_id in FIXED_TRIGGER_IDS]
    fdr = _fdr_bh(trigger_results)
    return {
        "checkpoint": checkpoint,
        "status": "complete",
        "eligible_prospective_targets": len(issue_set),
        "required_targets": checkpoint,
        "trigger_results": trigger_results,
        "fdr_bh_results": fdr,
    }


def _trigger_metric(trigger_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    hits = [row["hit_count"] for row in rows]
    top_k = int(trigger_id.split("__top", 1)[1].split("__", 1)[0])
    expected = top_k * 20 / 80
    avg = mean(hits) if hits else 0
    precision = sum(hits) / max(1, len(hits) * top_k)
    se = pstdev([hit - expected for hit in hits]) / math.sqrt(len(hits)) if len(hits) > 1 else 0
    p_value = _two_sided_normal_p((avg - expected) / se) if se else 1
    ci = _mean_ci(hits)
    return {
        "trigger_id": trigger_id,
        "sample_size": len(rows),
        "average_hits": round(avg, 6),
        "precision": round(precision, 6),
        "any_hit_rate": round(sum(1 for hit in hits if hit > 0) / len(hits), 6) if hits else 0,
        "expected_random_hits": expected,
        "normalized_lift": round((avg - expected) / expected, 6) if expected else 0,
        "bootstrap_ci_lower": ci["lower"],
        "bootstrap_ci_upper": ci["upper"],
        "empirical_p_value": round(p_value, 10),
        "max_losing_streak": _max_losing_streak([hit == 0 for hit in hits]),
    }


def _fdr_bh(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted([dict(row) for row in rows], key=lambda item: item["empirical_p_value"])
    m = max(1, len(ordered))
    for rank, row in enumerate(ordered, start=1):
        row["fdr_bh_threshold"] = round(rank / m * 0.05, 10)
        row["significant_bh_0_05"] = row["empirical_p_value"] <= row["fdr_bh_threshold"]
    return ordered


def _current_status(
    archive_hash: str,
    registry_hash: str,
    trigger_hash: str,
    prospective: list[ReplayDraw],
    snapshots: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    checkpoints: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    eligible = sorted({row["target_issue"] for row in validation if row["eligible_for_primary_analysis"]}, key=int)
    return {
        "experiment_id": EXPERIMENT_ID,
        "archive_manifest_hash": archive_hash,
        "prospective_registry_hash": registry_hash,
        "trigger_definition_hash": trigger_hash,
        "prospective_start_issue": PROSPECTIVE_START_ISSUE,
        "historical_last_issue": HISTORICAL_LAST_ISSUE,
        "prospective_targets_loaded": len(prospective),
        "eligible_primary_targets": len(eligible),
        "prediction_snapshots": len(snapshots),
        "retrospective_reconstruction_count": len([row for row in validation if not row["eligible_for_primary_analysis"]]),
        "next_checkpoint": next((item for item in CHECKPOINTS if len(eligible) < item), None),
        "checkpoint_status": {key: value["status"] for key, value in checkpoints.items()},
        "backend_modified": False,
        "production_database_modified": False,
        "status": "waiting_for_new_issue" if not prospective else "retrospective_only_until_pre_result_snapshots_exist",
    }


def _issue_audit(draws: list[ReplayDraw], prospective: list[ReplayDraw]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for draw in draws:
        counts[draw.issue] += 1
    rows = []
    for draw in draws:
        issue = _issue_int(draw.issue)
        rows.append(
            {
                "issue": draw.issue,
                "included_in_prospective": issue >= PROSPECTIVE_START_ISSUE,
                "historical_excluded": issue <= HISTORICAL_LAST_ISSUE,
                "duplicate_issue": counts[draw.issue] > 1,
                "valid_row": draw.valid,
                "audit_status": "prospective" if draw in prospective else "historical_excluded",
            }
        )
    return rows


def _text_report(result: dict[str, Any]) -> str:
    current = result["current_status"]
    return "\n".join(
        [
            "Phase Desktop 2.3 - Prospective Trigger Archive and Unseen Data Validation",
            f"Archive manifest hash: {result['archive_manifest_hash']}",
            f"Prospective registry hash: {result['prospective_registry_hash']}",
            f"Prospective start issue: {PROSPECTIVE_START_ISSUE}",
            f"Fixed triggers: {len(FIXED_TRIGGER_IDS)}",
            f"Prospective targets loaded: {current['prospective_targets_loaded']}",
            f"Eligible primary targets: {current['eligible_primary_targets']}",
            f"Prediction snapshots: {current['prediction_snapshots']}",
            f"Retrospective reconstruction: {current['retrospective_reconstruction_count']}",
            f"Status: {current['status']}",
            "Conclusion: research_only; no backend, production database, collector, learning, or prediction publish writes.",
        ]
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _registry_sha256(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized["registry_hash"] = ""
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue_int(issue: str) -> int:
    return int(issue) if str(issue).isdigit() else -1


def _mean_ci(values: list[int]) -> dict[str, float]:
    if not values:
        return {"lower": 0, "upper": 0}
    avg = mean(values)
    se = pstdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0
    return {"lower": round(avg - 1.96 * se, 6), "upper": round(avg + 1.96 * se, 6)}


def _max_losing_streak(failures: Iterable[bool]) -> int:
    current = 0
    maximum = 0
    for failed in failures:
        current = current + 1 if failed else 0
        maximum = max(maximum, current)
    return maximum


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows or [])
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    clean_rows = []
    for row in rows:
        clean_rows.append({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    fieldnames = sorted({key for row in clean_rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(clean_rows)
