import unittest

from services.repeat_number_shadow import (
    repeat_count_distribution, consecutive_overlaps,
    repeat_streaks, select_shadow_twenty, walk_forward_repeat_rate,
)


class RepeatNumberShadowTests(unittest.TestCase):
    def test_null_distribution(self):
        p = repeat_count_distribution()
        self.assertAlmostEqual(sum(p.values()), 1)
        self.assertAlmostEqual(sum(k * v for k, v in p.items()), 5)

    def test_streak_and_overlap(self):
        a = list(range(1, 21))
        b = list(range(11, 31))
        self.assertEqual(consecutive_overlaps([a, b]), [10])
        s = repeat_streaks([a, b])
        self.assertEqual(s[11], 2)
        self.assertEqual(s[1], 0)

    def test_unrestricted_selection(self):
        scores = {n: float(81 - n) for n in range(1, 81)}
        r = select_shadow_twenty(scores, range(1, 21))
        self.assertEqual(r["repeat_count"], 20)
        r = select_shadow_twenty(scores, range(21, 41))
        self.assertEqual(r["repeat_count"], 0)

    def test_no_future_leakage(self):
        draws = [list(range(1, 21))] * 110
        result = walk_forward_repeat_rate(draws, minimum_history=100)
        self.assertEqual(result["evaluated_draws"], 10)
        self.assertEqual(result["evaluations"][0]["target_index"], 100)

    def test_tied_scores_rotate_without_low_number_bias(self):
        scores = {n: 1.0 for n in range(1, 81)}
        counts = {n: 0 for n in range(1, 81)}
        for seed in range(80):
            chosen = select_shadow_twenty(scores, range(1, 21), tie_seed=seed)
            for n in chosen["selected_numbers"]:
                counts[n] += 1
        self.assertEqual(set(counts.values()), {20})

    def test_invalid_draw(self):
        with self.assertRaises(ValueError):
            consecutive_overlaps([[1] * 20])


if __name__ == "__main__":
    unittest.main()
