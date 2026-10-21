from services.top_k_shadow_evaluation import rank_candidates, evaluate_selection, random_baseline
import unittest


class TopKShadowEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.twenty = list(range(1, 21))
        self.scores = {n: float(n) for n in self.twenty}

    def test_top5_top6_nested_and_within_production(self):
        result = rank_candidates(self.twenty, self.scores)
        self.assertEqual(result["top5"], [20, 19, 18, 17, 16])
        self.assertEqual(result["top6"], [20, 19, 18, 17, 16, 15])
        self.assertTrue(set(result["top6"]) <= set(self.twenty))

    def test_tie_break_is_deterministic(self):
        result = rank_candidates(self.twenty, {n: 1 for n in self.twenty})
        self.assertEqual(result["top5"], [1, 2, 3, 4, 5])

    def test_scoring_and_baselines(self):
        self.assertEqual(evaluate_selection([1, 2, 3, 4, 5], list(range(1, 21))), 5)
        self.assertEqual(evaluate_selection([1, 2, 3, 4, 5, 80], list(range(1, 21))), 5)
        self.assertEqual(random_baseline(5), 1.25)
        self.assertEqual(random_baseline(6), 1.5)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            rank_candidates([1] * 20, self.scores)
        with self.assertRaises(ValueError):
            rank_candidates(self.twenty, {1: 1})
        with self.assertRaises(ValueError):
            evaluate_selection([1] * 5, list(range(1, 21)))


if __name__ == "__main__":
    unittest.main()
