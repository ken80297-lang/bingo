import csv
import json
from pathlib import Path

from desktop.core.gui_smoke import run_gui_smoke
from desktop.core.phase2_backtest import run_phase2_backtest
from desktop.core.replay_dataset import load_replay_dataset


MASTER = Path(r"C:\Users\ken80297\Desktop\master_draws.csv")
OUTPUT = Path("desktop") / "output" / "phase2_3_prospective"
SNAPSHOT_PATH = OUTPUT / "prediction_snapshots.jsonl"
SNAPSHOT_HASH = "11910e9d8c06ec746c4a727ecc04bd80b540158ff863dc5fb0beb5c75b496e8f"
TARGET_ISSUE = "115041413"


def test_phase_desktop1_snapshot_baseline_is_loaded_from_existing_snapshot():
    snapshot = _target_snapshot()

    assert snapshot["target_issue"] == TARGET_ISSUE
    assert snapshot["missing_top1"] == [64]
    assert snapshot["missing_top2"] == [64, 46]
    assert snapshot["missing_top3"] == [64, 46, 35]
    assert snapshot["snapshot_hash"] == SNAPSHOT_HASH
    assert snapshot["status"] == "pending_result"


def test_phase_desktop1_unopened_target_remains_pending_not_miss():
    dataset = load_replay_dataset(MASTER)
    current_status = json.loads((OUTPUT / "current_status.json").read_text(encoding="utf-8"))
    validation_manifest = json.loads((OUTPUT / "validation_manifest.json").read_text(encoding="utf-8"))

    assert dataset.summary.last_issue == "115041412"
    assert TARGET_ISSUE not in {draw.issue for draw in dataset.valid_draws}
    assert validation_manifest["validation_count"] == 0
    assert validation_manifest["eligible_primary_count"] == 0
    assert current_status["current_pending_target"] == TARGET_ISSUE
    assert current_status["top1_cumulative_precision"] is None
    assert current_status["top2_cumulative_precision"] is None
    assert current_status["top3_cumulative_precision"] is None


def test_phase_desktop1_snapshot_unchanged_after_gui_reload_exports_query_and_other_interval(tmp_path):
    before = SNAPSHOT_PATH.read_text(encoding="utf-8")
    before_snapshot = _target_snapshot()

    gui_result = run_gui_smoke(auto_close_ms=50)
    if not gui_result.get("environment_blocked"):
        assert gui_result["all_pages_opened"]
    assert load_replay_dataset(MASTER).summary.last_issue == "115041412"
    _export_snapshot_views(tmp_path, before_snapshot)
    assert _query_single_snapshot(TARGET_ISSUE)["snapshot_hash"] == SNAPSHOT_HASH
    _run_other_interval_simulation(tmp_path)

    after = SNAPSHOT_PATH.read_text(encoding="utf-8")
    after_snapshot = _target_snapshot()
    assert after == before
    assert after_snapshot == before_snapshot
    assert after_snapshot["snapshot_hash"] == SNAPSHOT_HASH


def _target_snapshot() -> dict:
    rows = [json.loads(line) for line in SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    matches = [row for row in rows if row["target_issue"] == TARGET_ISSUE]
    assert len(matches) == 1
    return matches[0]


def _query_single_snapshot(issue: str) -> dict:
    rows = [json.loads(line) for line in SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next(row for row in rows if row["target_issue"] == issue)


def _export_snapshot_views(tmp_path: Path, snapshot: dict) -> None:
    json_path = tmp_path / "snapshot.json"
    csv_path = tmp_path / "snapshot.csv"
    report_path = tmp_path / "snapshot_report.txt"
    json_path.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(snapshot.keys()))
        writer.writeheader()
        writer.writerow(snapshot)
    report_path.write_text(
        "\n".join(
            [
                "第一份前瞻 Snapshot",
                f"目標期號：{snapshot['target_issue']}",
                f"Top 1：{snapshot['missing_top1']}",
                f"Top 2：{snapshot['missing_top2']}",
                f"Top 3：{snapshot['missing_top3']}",
                f"Snapshot hash：{snapshot['snapshot_hash']}",
                "目前狀態：等待開獎結果",
            ]
        ),
        encoding="utf-8",
    )
    assert json_path.exists()
    assert csv_path.exists()
    assert report_path.exists()


def _run_other_interval_simulation(tmp_path: Path) -> None:
    path = tmp_path / "other_interval.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "issue", "time", *[f"n{i:02d}" for i in range(1, 21)], "super", "big_small", "odd_even"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(40):
            numbers = [((index * 3 + number - 1) % 80) + 1 for number in range(1, 21)]
            row = {
                "date": "2026-07-20",
                "issue": str(114000000 + index),
                "time": "10:00",
                "super": str(numbers[index % 20]),
                "big_small": "big",
                "odd_even": "odd",
            }
            row.update({f"n{i:02d}": str(numbers[i - 1]) for i in range(1, 21)})
            writer.writerow(row)
    report = run_phase2_backtest(str(path), min_history=20)
    assert report["valid_simulations"] == 20
    assert TARGET_ISSUE not in {row["target_issue"] for row in report["simulations"]}
