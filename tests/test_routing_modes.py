import os
import sys
import unittest
from unittest.mock import patch
from starlette.requests import Request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi.testclient import TestClient
from app.main import (
    app,
    key_pool,
    build_route_candidates,
    extract_route_mode,
    extract_fallback_models,
)


class RoutingModesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_01_route_mode_extraction(self):
        # 1. Test header extraction directly
        scope_header = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"x-route-mode", b"11")],
        }
        self.assertEqual(extract_route_mode(Request(scope_header)), "11")

        # 2. Test query parameter extraction
        scope_query = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "query_string": b"route_mode=10",
        }
        self.assertEqual(extract_route_mode(Request(scope_query)), "10")

        # 3. Test body dictionary fallback extraction
        empty_req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""})
        self.assertEqual(extract_route_mode(empty_req, {"route_mode": "01"}), "01")
        self.assertEqual(extract_route_mode(empty_req, {"routing_mode": "00"}), "00")

    def test_02_candidates_matrix(self):
        key_name = key_pool.keys[0]["name"]

        c11 = build_route_candidates("11", "gemini-2.5-flash", target_key_name=key_name)
        self.assertEqual(len(c11), 1)
        self.assertEqual(c11[0][0]["name"], key_name)
        self.assertEqual(c11[0][1], "gemini-2.5-flash")

        c10 = build_route_candidates("10", "gemini-3.8-flash", target_key_name=key_name)
        self.assertGreaterEqual(len(c10), 2)
        self.assertTrue(all(c[0]["name"] == key_name for c in c10))
        self.assertEqual(c10[0][1], "gemini-3.8-flash")

        c01 = build_route_candidates("01", "gemini-3.5-flash-lite")
        self.assertEqual(len(c01), len(key_pool.keys))
        self.assertTrue(all(c[1] == "gemini-3.5-flash-lite" for c in c01))

        c00 = build_route_candidates("00", "gemini-2.5-flash")
        self.assertGreaterEqual(len(c00), len(key_pool.keys))

    @patch("httpx.AsyncClient.get")
    def test_03_ping_endpoint_health(self, mock_get):
        mock_response = httpx.Response(
            status_code=200,
            json={"models": []},
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com"),
        )
        mock_get.return_value = mock_response

        key_name = key_pool.keys[0]["name"]
        resp = self.client.post(f"/v1/keys/{key_name}/ping")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn("latency_ms", data)
        if data.get("status") == "ok":
            self.assertIn("models_health", data)
            self.assertTrue(data.get("auth_valid"))
            self.assertIn("deprioritized_models", data)

    def test_04_cancellation_integrity(self):
        key_info = key_pool.keys[0]
        key_name = key_info["name"]
        initial_fail = key_info.get("failure_count", 0)
        initial_in_flight = key_info.setdefault("model_in_flight", {}).get("gemini-3.8-flash", 0)

        acq = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertIsNotNone(acq)
        self.assertEqual(key_info["model_in_flight"]["gemini-3.8-flash"], initial_in_flight + 1)

        key_pool.release(acq["key"], model="gemini-3.8-flash")
        self.assertEqual(key_info["model_in_flight"]["gemini-3.8-flash"], initial_in_flight)
        self.assertEqual(key_info.get("failure_count", 0), initial_fail)


if __name__ == "__main__":
    unittest.main()
