from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from services.external_ai_shadow import propose_shadow_numbers


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ExternalAIShadowTests(unittest.TestCase):
    def setUp(self):
        self.draw = {"issue": "115056999", "numbers": list(range(1, 21)), "super_number": 4}

    def test_disabled_by_default(self):
        with patch.dict("os.environ", {"EXTERNAL_AI_SHADOW_ENABLED": "0", "GROQ_API_KEY": "unused"}):
            self.assertEqual(propose_shadow_numbers(self.draw, {})["status"], "skipped")

    def test_missing_key(self):
        with patch.dict("os.environ", {"EXTERNAL_AI_SHADOW_ENABLED": "1", "GROQ_API_KEY": ""}):
            self.assertEqual(propose_shadow_numbers(self.draw, {})["reason"], "missing_api_key")

    def test_valid_result(self):
        answer = {"numbers": list(range(1, 21)), "top5": [1, 2, 3, 4, 5], "super_number": 6}
        body = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        with patch.dict("os.environ", {"EXTERNAL_AI_SHADOW_ENABLED": "1", "GROQ_API_KEY": "test"}):
            with patch("services.external_ai_shadow.request.urlopen", return_value=FakeResponse(body)):
                self.assertEqual(propose_shadow_numbers(self.draw, {})["status"], "ok")

    def test_duplicate_numbers_rejected(self):
        answer = {"numbers": [1] * 20, "top5": [1, 2, 3, 4, 5], "super_number": 6}
        body = {"choices": [{"message": {"content": json.dumps(answer)}}]}
        with patch.dict("os.environ", {"EXTERNAL_AI_SHADOW_ENABLED": "1", "GROQ_API_KEY": "test"}):
            with patch("services.external_ai_shadow.request.urlopen", return_value=FakeResponse(body)):
                self.assertEqual(propose_shadow_numbers(self.draw, {})["reason"], "invalid_model_output")


if __name__ == "__main__":
    unittest.main()
