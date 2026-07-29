from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from desktop.core.phase2_backtest import run_phase2_backtest


DEFAULT_OUTPUT_DIR = Path("desktop") / "output" / "phase2_30day"


def export_phase2_report(csv_path: str, output_dir: str | Path = DEFAULT_OUTPUT_DIR, min_history: int = 100) -> dict[str, Any]:
    report = run_phase2_backtest(csv_path, min_history=min_history)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / "dataset_validation.json", report["dataset"])
    _write_json(target / "backtest_summary.json", _summary_payload(report))
    _write_csv(target / "backtest_by_issue.csv", _issue_rows(report))
    _write_csv(target / "daily_summary.csv", report["daily_summary"])
    _write_csv(target / "rule_performance.csv", report["rule_performance"].values())
    _write_csv(target / "high_confidence_conditions.csv", _confidence_rows(report))
    _write_csv(target / "baseline_comparison.csv", [report["baseline_comparison"]])
    _write_csv(target / "look_ahead_audit.csv", report["look_ahead_audit"])
    (target / "phase2_report.txt").write_text(_text_report(report), encoding="utf-8")
    return {"output_dir": str(target), "report": report}


def _summary_payload(report: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "total_simulations",
        "valid_simulations",
        "valid_prediction_count",
        "invalid_prediction_count",
        "average_hits",
        "max_hits",
        "min_hits",
        "average_high5_hits",
        "super_hit_rate",
        "big_small_hit_rate",
        "odd_even_hit_rate",
        "best_rule",
        "worst_rule",
        "holdout",
        "hit_distribution",
        "high5_distribution",
        "no_look_ahead",
    ]
    return {key: report.get(key) for key in keys}


def _issue_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in report["simulations"]:
        prediction = item["prediction"]
        rows.append(
            {
                "source_issue": item["source_issue"],
                "target_issue": item["target_issue"],
                "target_date": item["target_date"],
                "target_time": item["target_time"],
                "recommend_numbers": " ".join(str(n) for n in prediction["recommend_numbers"]),
                "high_probability_numbers": " ".join(str(n) for n in prediction["high_probability_numbers"]),
                "super_candidate": prediction["super_candidate"],
                "big_small": prediction["big_small"],
                "odd_even": prediction["odd_even"],
                "hits_20": item["hits_20"],
                "hits_high5": item["hits_high5"],
                "super_hit": item["super_hit"],
                "big_small_hit": item["big_small_hit"],
                "odd_even_hit": item["odd_even_hit"],
                "confidence": prediction["confidence"],
                "active_conditions": " ".join(prediction["active_conditions"]),
                "valid_prediction": item["valid_prediction"],
                "invalid_reason": item["invalid_reason"],
            }
        )
    return rows


def _confidence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, payload in report["high_confidence"]["conditions"].items():
        rows.append({"condition": name, **payload})
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: Any) -> None:
    rows = list(rows or [])
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _text_report(report: dict[str, Any]) -> str:
    best = report.get("best_rule") or {}
    worst = report.get("worst_rule") or {}
    return "\n".join(
        [
            "Phase Desktop 2 - 30 Day Replay / AI Backtest",
            f"CSV rows: {report['dataset']['total_rows']}",
            f"Valid rows: {report['dataset']['valid_rows']}",
            f"Warm-up rows: {report['dataset']['warmup_rows']}",
            f"Replay targets: {report['dataset']['replay_target_rows']}",
            f"Average hits: {report['average_hits']}",
            f"Average high5 hits: {report['average_high5_hits']}",
            f"Super hit rate: {report['super_hit_rate']}",
            f"Big/small hit rate: {report['big_small_hit_rate']}",
            f"Odd/even hit rate: {report['odd_even_hit_rate']}",
            f"Best rule: {best.get('rule_name_zh')} ({best.get('rule_key')})",
            f"Worst rule: {worst.get('rule_name_zh')} ({worst.get('rule_key')})",
            f"No look-ahead: {report['no_look_ahead']}",
        ]
    )

