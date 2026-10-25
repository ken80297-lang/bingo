import unittest
from services.external_ai_shadow_verification import verify_shadow_prediction


class ShadowVerificationTests(unittest.TestCase):
    def setUp(self):
        self.prediction = {
            "prediction_issue": "115057002",
            "numbers": list(range(1, 21)),
            "top5": [1, 2, 3, 4, 5],
            "super_number": 7,
        }
        self.draw = {
            "issue": "115057002",
            "numbers": list(range(1, 11)) + list(range(41, 51)),
            "super_number": 7,
        }

    def test_counts_and_super_hit(self):
        result = verify_shadow_prediction(self.prediction, self.draw)
        self.assertEqual(result["hit_count"], 10)
        self.assertEqual(result["top5_hit_count"], 5)
        self.assertTrue(result["super_hit"])

    def test_wrong_issue_rejected(self):
        self.draw["issue"] = "115057003"
        with self.assertRaisesRegex(ValueError, "prediction_target_mismatch"):
            verify_shadow_prediction(self.prediction, self.draw)

    def test_duplicate_draw_rejected(self):
        self.draw["numbers"] = [1] * 20
        with self.assertRaisesRegex(ValueError, "invalid_prediction_or_draw"):
            verify_shadow_prediction(self.prediction, self.draw)

    def test_super_must_be_in_draw(self):
        self.draw["super_number"] = 80
        with self.assertRaisesRegex(ValueError, "invalid_prediction_or_draw"):
            verify_shadow_prediction(self.prediction, self.draw)


if __name__ == "__main__":
    unittest.main()
