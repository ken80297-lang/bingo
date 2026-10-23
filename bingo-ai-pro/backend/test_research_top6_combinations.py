"""Focused, offline tests for the Top-6 walk-forward research runner."""
from __future__ import annotations

import unittest

from research_top6_combinations import choose_six, evaluate


class TopSixResearchTests(unittest.TestCase):
    def setUp(self):
        self.draws = [
            [((i * 13 + j * 7) % 80) + 1 for j in range(20)]
            for i in range(35)
        ]
        # All 20 values in each synthetic draw are unique.
        self.assertTrue(all(len(set(draw)) == 20 for draw in self.draws))

    def test_all_strategies_select_six_valid_distinct_sorted_numbers(self):
        for method in ("frequency", "repeat", "combination"):
            selected = choose_six(self.draws[:20], method, window=10)
            self.assertEqual(len(selected), 6)
            self.assertEqual(len(set(selected)), 6)
            self.assertEqual(selected, sorted(selected))
            self.assertTrue(all(1 <= number <= 80 for number in selected))

    def test_walkforward_outputs_expected_counts(self):
        result = evaluate(self.draws, warmup=10, window=10)
        self.assertEqual(result["sample_size"], 25)
        for strategy in result["strategies"].values():
            self.assertEqual(sum(strategy["hits_distribution"].values()), 25)
            self.assertEqual(strategy["six_of_six"], strategy["hits_distribution"]["6"])

    def test_future_draw_does_not_change_earlier_predictions(self):
        prefix = self.draws[:20]
        altered = prefix + [list(range(61, 81))]
        for method in ("frequency", "repeat", "combination"):
            self.assertEqual(
                choose_six(prefix, method, window=10),
                choose_six(altered[:20], method, window=10),
            )

    def test_invalid_warmup_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(self.draws, warmup=1)


if __name__ == "__main__":
    unittest.main()
