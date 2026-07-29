from __future__ import annotations

import csv
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from desktop.config import PROJECT_ROOT
from desktop.core.phase2_4_operations import run_phase2_4_operation_cycle, verify_operation_invariants
from desktop.core.replay_dataset import DEFAULT_MASTER_DRAWS_PATH, load_replay_dataset


OUTPUT_ROOT = PROJECT_ROOT / "desktop" / "output"
PROSPECTIVE_DIR = OUTPUT_ROOT / "phase2_3_prospective"
SETTINGS_PATH = PROJECT_ROOT / "desktop" / "config" / "user_settings.json"
LOG_PATH = PROJECT_ROOT / "desktop" / "logs" / "desktop_simulator.log"


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def safe_call(label: str, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:
        setup_logging()
        logging.exception("%s failed", label)
        raise


def load_user_settings() -> dict[str, Any]:
    defaults = {
        "default_csv_path": str(DEFAULT_MASTER_DRAWS_PATH),
        "default_output_path": str(OUTPUT_ROOT),
        "warmup": 100,
        "recent_limit": 100,
        "auto_load_on_start": True,
        "verify_hash_on_start": True,
        "open_report_on_finish": False,
        "show_advanced_statistics": True,
        "auto_mode": False,
    }
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults
    return defaults | payload


def save_user_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def dataset_status(csv_path: str | Path = DEFAULT_MASTER_DRAWS_PATH) -> dict[str, Any]:
    dataset = load_replay_dataset(csv_path)
    draws = dataset.valid_draws
    dates = [draw.date for draw in draws if draw.date]
    path = Path(csv_path)
    return {
        "path": str(path),
        "total_rows": dataset.summary.total_rows,
        "valid_rows": dataset.summary.valid_rows,
        "error_rows": len(dataset.summary.invalid_rows),
        "first_issue": dataset.summary.first_issue or "",
        "last_issue": dataset.summary.last_issue or "",
        "date_range": f"{min(dates)} ～ {max(dates)}" if dates else "",
        "dataset_hash": _file_sha256(path) if path.exists() else "",
        "invalid_rows": dataset.summary.invalid_rows,
        "draws": draws,
    }


def latest_backtest_summary() -> dict[str, Any] | None:
    path = OUTPUT_ROOT / "phase2_30day" / "backtest_summary.json"
    if not path.exists():
        return None
    return _read_json(path)


def latest_phase2_1_summary() -> dict[str, Any]:
    return {
        "overall": _read_json(OUTPUT_ROOT / "phase2_1_validation" / "overall_significance.json"),
        "high5": _read_json(OUTPUT_ROOT / "phase2_1_validation" / "high5_significance.json"),
        "super": _read_json(OUTPUT_ROOT / "phase2_1_validation" / "super_candidate_significance.json"),
        "walk": _read_json(OUTPUT_ROOT / "phase2_1_validation" / "walk_forward_summary.json"),
        "multiple": _read_csv(OUTPUT_ROOT / "phase2_1_validation" / "multiple_testing_results.csv"),
        "losing": _read_csv(OUTPUT_ROOT / "phase2_1_validation" / "losing_streak_analysis.csv"),
    }


def prospective_status() -> dict[str, Any]:
    current = _read_json(PROSPECTIVE_DIR / "current_status.json")
    snapshots = read_snapshots()
    validations = _read_csv(PROSPECTIVE_DIR / "validation_results.csv")
    manifest = _read_json(PROSPECTIVE_DIR / "prediction_snapshots_manifest.json")
    invariants = verify_operation_invariants(PROSPECTIVE_DIR)
    pending = next((row for row in snapshots if row.get("status") == "pending_result"), None)
    return {
        "current": current,
        "snapshots": snapshots,
        "validations": validations,
        "manifest": manifest,
        "invariants": invariants,
        "pending": pending,
    }


def read_snapshots() -> list[dict[str, Any]]:
    path = PROSPECTIVE_DIR / "prediction_snapshots.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_prospective_operation(csv_path: str | Path = DEFAULT_MASTER_DRAWS_PATH) -> dict[str, Any]:
    return run_phase2_4_operation_cycle(str(csv_path))


def single_issue_payload(csv_path: str | Path, target_issue: str) -> dict[str, Any]:
    from desktop.core.phase2_backtest import build_replay_prediction
    from desktop.core.rule_replay import replay_all_rules

    dataset = load_replay_dataset(csv_path)
    draws = dataset.valid_draws
    by_issue = {draw.issue: draw for draw in draws}
    target = by_issue.get(str(target_issue))
    history = [draw for draw in draws if int(draw.issue) < int(target_issue)]
    if not history:
        return {"target_issue": target_issue, "error": "沒有可用歷史資料"}
    source = history[-1]
    rules = replay_all_rules(history, source)
    prediction = build_replay_prediction(rules, history)
    actual_numbers = target.numbers if target else []
    hits = sorted(set(prediction.recommend_numbers) & set(actual_numbers)) if target else []
    return {
        "target_issue": str(target_issue),
        "maximum_feature_issue": source.issue,
        "history_count": len(history),
        "recommend_numbers": prediction.recommend_numbers,
        "high_probability_numbers": prediction.high_probability_numbers,
        "super_candidate": prediction.super_candidate,
        "big_small": prediction.big_small,
        "odd_even": prediction.odd_even,
        "actual_numbers": actual_numbers,
        "actual_super": target.super_number if target else None,
        "hit_numbers": hits,
        "hit_count": len(hits) if target else None,
        "status": "已驗證" if target else "尚無資料",
    }


def rule_rows() -> list[dict[str, Any]]:
    rows = _read_csv(OUTPUT_ROOT / "phase2_1_validation" / "rule_equal_size_random_baseline.csv")
    order = [
        "hot",
        "cold",
        "missing",
        "repeat",
        "tail",
        "gap",
        "cluster",
        "diagonal",
        "super",
        "laowanjia",
        "ladder",
        "partial_ladder",
        "extended_ladder",
        "reverse",
        "neighbor",
        "guide",
        "integrated",
        "sunset",
        "momentum",
        "super_number_trajectory_recovery",
        "cluster_aftershock_recovery",
    ]
    lookup = {row.get("rule_key"): row for row in rows}
    return [lookup[key] for key in order if key in lookup]


def timeline_rows() -> list[dict[str, Any]]:
    validations = {row.get("target_issue"): row for row in _read_csv(PROSPECTIVE_DIR / "validation_results.csv")}
    rows = []
    for snapshot in read_snapshots():
        validation = validations.get(snapshot["target_issue"], {})
        rows.append(
            {
                "target_issue": snapshot["target_issue"],
                "generated_at": snapshot["generated_at"],
                "top1": _join(snapshot.get("missing_top1")),
                "top2": _join(snapshot.get("missing_top2")),
                "top3": _join(snapshot.get("missing_top3")),
                "snapshot_hash": snapshot["snapshot_hash"],
                "result_time": validation.get("result_draw_time", ""),
                "top1_hit": validation.get("top1_hit_count", "尚未驗證"),
                "top2_hit": validation.get("top2_hit_count", "尚未驗證"),
                "top3_hit": validation.get("top3_hit_count", "尚未驗證"),
                "timing_valid": validation.get("timing_valid", "尚未驗證"),
                "eligible": validation.get("eligible_for_primary_analysis", "尚未驗證"),
                "validation_hash": validation.get("validation_hash", ""),
                "status": "等待結果" if not validation else "已驗證",
            }
        )
    return rows


def export_current_summary(output_dir: str | Path = PROSPECTIVE_DIR) -> dict[str, str]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    status = prospective_status()
    dataset = dataset_status(load_user_settings()["default_csv_path"])
    summary = {
        "csv": dataset["path"],
        "total_rows": dataset["total_rows"],
        "latest_issue": dataset["last_issue"],
        "pending_target": (status["pending"] or {}).get("target_issue", ""),
        "top1": _join((status["pending"] or {}).get("missing_top1")),
        "top2": _join((status["pending"] or {}).get("missing_top2")),
        "top3": _join((status["pending"] or {}).get("missing_top3")),
        "validation_count": status["current"].get("validation_count", 0),
    }
    csv_path = target / "desktop_summary_export.csv"
    json_path = target / "desktop_summary_export.json"
    report_path = target / "desktop_summary_report.txt"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary.keys()))
        writer.writeheader()
        writer.writerow(summary)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_summary_report(summary), encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "report": str(report_path)}


def _summary_report(summary: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Bingo AI Pro 桌面模擬器中文報告",
            f"CSV：{summary['csv']}",
            f"總期數：{summary['total_rows']}",
            f"最新 issue：{summary['latest_issue']}",
            f"Pending target：{summary['pending_target']}",
            f"Top 1：{summary['top1'] or '尚無'}",
            f"Top 2：{summary['top2'] or '尚無'}",
            f"Top 3：{summary['top3'] or '尚無'}",
            f"Validation count：{summary['validation_count']}",
        ]
    )


def output_directories() -> list[dict[str, str]]:
    return [
        {"name": "Phase 2", "path": str(OUTPUT_ROOT / "phase2_30day")},
        {"name": "Phase 2.1", "path": str(OUTPUT_ROOT / "phase2_1_validation")},
        {"name": "Phase 2.2", "path": str(OUTPUT_ROOT / "phase2_2_sparse_triggers")},
        {"name": "Phase 2.3 / 2.4", "path": str(PROSPECTIVE_DIR)},
    ]


def copy_to_clipboard_friendly_path(path: str) -> str:
    return str(Path(path))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = __import__("hashlib").sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _join(numbers: Any) -> str:
    if not numbers:
        return ""
    return "、".join(str(number) for number in numbers)
