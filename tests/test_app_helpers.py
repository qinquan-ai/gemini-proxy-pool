import os
import unittest

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ["GEMINI_KEYS"] = "test|fake-key"

from app.main import app, build_generation_config, normalize_model, retry_after_seconds


class AppHelperTests(unittest.TestCase):
    def test_generation_config_translation(self):
        config = build_generation_config(
            {
                "temperature": 0.3,
                "top_p": 0.8,
                "max_completion_tokens": 512,
                "stop": "END",
            }
        )
        self.assertEqual(config["temperature"], 0.3)
        self.assertEqual(config["topP"], 0.8)
        self.assertEqual(config["maxOutputTokens"], 512)
        self.assertEqual(config["stopSequences"], ["END"])

    def test_retry_after_header(self):
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(429, headers={"retry-after": "17"}, request=request)
        self.assertEqual(retry_after_seconds(response), 17)

    def test_invalid_model_is_rejected(self):
        with self.assertRaises(HTTPException):
            normalize_model("../../bad")

    def test_dashboard_and_status_endpoints(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/assets/dashboard.js").status_code, 200)
            self.assertEqual(client.get("/assets/styles/tokens.css").status_code, 200)
            status = client.get("/v1/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["total_keys"], 1)
            self.assertEqual(client.get("/healthz").status_code, 200)

    def test_responses_rejects_server_side_history_lookup(self):
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={
                    "model": "gemini-2.5-flash",
                    "previous_response_id": "resp_previous",
                    "input": "continue",
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_responses_rejects_hosted_tools(self):
        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={
                    "model": "gemini-2.5-flash",
                    "input": "search",
                    "tools": [{"type": "web_search"}],
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("web_search", response.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
