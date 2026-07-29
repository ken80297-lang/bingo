import csv
from pathlib import Path

from desktop.core.replay_dataset import load_replay_dataset


def test_dataset_loader_sorts_validates_and_reports_invalid_rows(tmp_path):
    path = tmp_path / "master_draws.csv"
    _write_rows(
        path,
        [
            _row("2026-01-01", "100002", 2),
            _row("2026-01-01", "100001", 1),
            {**_row("2026-01-01", "100003", 3), "super": "80"},
            _row("2026-01-01", "100002", 4),
        ],
    )

    dataset = load_replay_dataset(path)

    assert dataset.summary.total_rows == 4
    assert dataset.summary.valid_rows == 1
    assert dataset.summary.first_issue == "100001"
    assert "100002" in dataset.summary.duplicate_issues
    assert dataset.summary.invalid_rows


def test_master_draws_6090_rows_integration():
    path = Path(r"C:\Users\ken80297\Desktop\master_draws.csv")

    dataset = load_replay_dataset(path)

    assert path.exists()
    assert dataset.summary.total_rows == 6090
    assert dataset.summary.valid_rows == 6090
    assert dataset.summary.first_issue == "115035323"
    assert dataset.summary.last_issue == "115041412"
    assert dataset.summary.duplicate_issues == []
    assert dataset.summary.invalid_rows == []
    assert all(draw.big_small in {"偏大", "偏小", "均衡"} for draw in dataset.valid_draws[:20])
    assert all(draw.odd_even in {"單", "雙", "均衡"} for draw in dataset.valid_draws[:20])


def _write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["date", "issue", "time", *[f"n{i:02d}" for i in range(1, 21)], "super", "big_small", "odd_even"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _row(date, issue, offset):
    numbers = [((offset + i - 1) % 80) + 1 for i in range(1, 21)]
    row = {"date": date, "issue": issue, "time": "10:00", "super": str(numbers[5]), "big_small": "small", "odd_even": "even"}
    row.update({f"n{i:02d}": str(numbers[i - 1]) for i in range(1, 21)})
    return row
