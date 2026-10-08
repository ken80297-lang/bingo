"""Regression tests for read-only comparison API."""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from types import ModuleType

from api.external_ai_shadow import latest_shadow


class ShadowPageApiTests(unittest.TestCase):
    def test_missing_draw_does_not_query_database(self):
        official = ModuleType("database.official_draw_store")
        official.get_latest_official_draw = lambda: None
        with patch.dict("sys.modules", {"database": ModuleType("database"), "database.official_draw_store": official}):
            result = latest_shadow()
        self.assertIsNone(result["target_issue"])
        self.assertEqual(result["external_ai"]["status"], "waiting")

    def test_latest_model_wins(self):
        db = MagicMock()
        cursor = db.__enter__.return_value.cursor.return_value.__enter__.return_value
        timestamp = datetime(2026, 10, 8, tzinfo=timezone.utc)
        cursor.fetchall.return_value = [
            ("115057002", list(range(1, 21)), [1, 2, 3, 4, 5], 6, "pending", None, None, None, timestamp),
            ("115057002", list(range(21, 41)), [21, 22, 23, 24, 25], 26, "pending", None, None, None, timestamp),
            ("115057001", list(range(1, 21)), [1, 2, 3, 4, 5], 6, "verified", 7, 2, False, timestamp),
        ]
        official = ModuleType("database.official_draw_store")
        official.get_latest_official_draw = lambda: {"issue": "115057001"}
        postgres = ModuleType("database.postgres")
        postgres.get_connection = lambda: db
        with patch.dict("sys.modules", {"database": ModuleType("database"), "database.official_draw_store": official, "database.postgres": postgres}):
            result = latest_shadow()
        self.assertEqual(result["external_ai"]["numbers"], list(range(1, 21)))
        self.assertEqual(result["verification"]["hits"], 7)


if __name__ == "__main__":
    unittest.main()
