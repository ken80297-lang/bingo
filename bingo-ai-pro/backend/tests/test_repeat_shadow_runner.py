import unittest

from research_repeat_shadow import evaluate


class RepeatShadowRunnerTests(unittest.TestCase):
    def test_chronological_evaluation(self):
        draws = []
        for i in range(120):
            draws.append([((i * 7 + j) % 80) + 1 for j in range(20)])
        result = evaluate(draws, warmup=100)
        self.assertEqual(result["sample_size"], 20)
        self.assertGreaterEqual(result["candidate_mean_repeated_numbers"], 0)
        self.assertLessEqual(result["candidate_mean_repeated_numbers"], 20)

    def test_reject_insufficient_history(self):
        with self.assertRaises(ValueError):
            evaluate([list(range(1, 21))] * 5, warmup=100)


if __name__ == "__main__":
    unittest.main()
