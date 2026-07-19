import os
import unittest

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ["GEMINI_KEYS"] = "test|fake-key"
os.environ["KEY_POOL_STATE_FILE"] = ""

from app.core.thought_signatures import thought_signature_store
from app.main import (
    app,
    build_generation_config,
    extract_response_parts,
    is_allowed_native_gemini_path,
    normalize_model,
    retry_after_seconds,
    usage_metadata_from_sse_tail,
)


class AppHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def tearDown(self):
        thought_signature_store.clear()

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

    def test_native_sse_usage_metadata_is_extracted(self):
        usage = usage_metadata_from_sse_tail(
            b'data: {"candidates":[]}\n\n'
            b'data: {"usageMetadata":{"promptTokenCount":12,'
            b'"candidatesTokenCount":3,"totalTokenCount":15}}\n\n'
        )
        self.assertEqual(usage["totalTokenCount"], 15)

    def test_response_tool_call_caches_thought_signature(self):
        _, tool_calls, finish_reason = extract_response_parts(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "lookup",
                                        "args": {"value": 7},
                                    },
                                    "thoughtSignature": "encrypted-signature",
                                }
                            ]
                        }
                    }
                ]
            }
        )
        call = tool_calls[0]
        self.assertEqual(finish_reason, "tool_calls")
        self.assertEqual(
            thought_signature_store.resolve(
                call["id"], "lookup", {"value": 7}
            ),
            "encrypted-signature",
        )

    def test_invalid_model_is_rejected(self):
        with self.assertRaises(HTTPException):
            normalize_model("../../bad")

    def test_native_gemini_gateway_path_allowlist(self):
        self.assertTrue(
            is_allowed_native_gemini_path(
                "models/gemini-3-flash-preview:generateContent"
            )
        )
        self.assertTrue(is_allowed_native_gemini_path("models"))
        self.assertFalse(is_allowed_native_gemini_path("files"))
        self.assertFalse(is_allowed_native_gemini_path("../admin"))
        response = self.client.post("/v1beta/files", json={})
        self.assertEqual(response.status_code, 404)

    def test_dashboard_and_status_endpoints(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/assets/dashboard.js").status_code, 200)
        self.assertEqual(self.client.get("/assets/styles/tokens.css").status_code, 200)
        status = self.client.get("/v1/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["total_keys"], 1)
        self.assertIn("哔哩哔哩", status.json()["supported_video_sources"])
        self.assertIn("recent_video_jobs", status.json())
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_responses_rejects_server_side_history_lookup(self):
        response = self.client.post(
            "/v1/responses",
            json={
                "model": "gemini-2.5-flash",
                "previous_response_id": "resp_previous",
                "input": "continue",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_responses_rejects_hosted_tools(self):
        response = self.client.post(
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
