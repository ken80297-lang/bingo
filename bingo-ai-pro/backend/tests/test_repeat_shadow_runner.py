import unittest
import csv
import tempfile
from pathlib import Path

from research_repeat_shadow import evaluate, read_draws


class RepeatShadowRunnerTests(unittest.TestCase):
    def test_chronological_evaluation(self):
        draws = []
        for i in range(120):
            draws.append([((i * 7 + j) % 80) + 1 for j in range(20)])
        result = evaluate(draws, warmup=100)
        self.assertEqual(result["sample_size"], 20)
        self.assertGreaterEqual(result["candidate_mean_repeated_numbers"], 0)
        self.assertLessEqual(result["candidate_mean_repeated_numbers"], 20)

    def test_csv_requires_chronological_unique_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draws.csv"
            fields = ["issue"] + [f"n{i}" for i in range(1, 21)]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for issue in (102, 101):
                    writer.writerow({"issue": issue, **{f"n{i}": i for i in range(1, 21)}})
            with self.assertRaisesRegex(ValueError, "strictly increasing"):
                read_draws(path)

    def test_csv_accepts_ordered_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draws.csv"
            fields = ["issue"] + [f"n{i}" for i in range(1, 21)]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for issue in (101, 102):
                    writer.writerow({"issue": issue, **{f"n{i}": i for i in range(1, 21)}})
            self.assertEqual(len(read_draws(path)), 2)

    def test_reject_insufficient_history(self):
        with self.assertRaises(ValueError):
            evaluate([list(range(1, 21))] * 5, warmup=100)


if __name__ == "__main__":
    unittest.main()
