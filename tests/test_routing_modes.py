import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.main import (
    app,
    build_candidates,
    key_pool,
    parse_route_spec,
    slot_registry,
)
from app.scheduling.policy import DeadlineBelowFloor


def make_request(headers: dict | None = None, query: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [
            (k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()
        ],
        "query_string": query.encode(),
    }
    return Request(scope)


class FakePool:
    """与真实 Key 池解耦的候选测试替身：两个 Key，所有模型满额可用。"""

    def __init__(self):
        self.keys = [
            {"name": "k1", "key": "a", "disabled": False, "model_in_flight": {}},
            {"name": "k2", "key": "b", "disabled": False, "model_in_flight": {}},
        ]

    def _get_model_quota(self, key_info, model):
        return {
            "used_today": 0,
            "success_today": 0,
            "limit_rpd": 500,
            "cooldown_until": 0.0,
        }

    def can_consume(self, key_info, model, check_concurrency=True, now=None):
        return True, "Available"

    def find_key_by_name(self, name):
        return next((k for k in self.keys if k["name"] == name), None)


class RoutingModesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_01_switch_parsing(self):
        spec = parse_route_spec(
            make_request({"x-fallback-models": "gemini-3.1-flash-lite,gemini-2.5-flash-lite"}),
            None,
            model="gemini-3.5-flash-lite",
            is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertTrue(spec.allow_model_switch)
        self.assertFalse(spec.lock_key)
        self.assertEqual(
            spec.models,
            ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"],
        )
        self.assertEqual(spec.label, "family")

        # 没传数组 = 不换模型，数组里只有请求模型自己
        spec_single = parse_route_spec(
            make_request(),
            None,
            model="gemini-3.8-flash",
            is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertFalse(spec_single.allow_model_switch)
        self.assertEqual(spec_single.models, ["gemini-3.8-flash"])
        self.assertEqual(spec_single.label, "single")

        # 锁 Key 开关来自 header 或 query
        spec_lock = parse_route_spec(
            make_request({"x-lock-key": "true"}),
            None,
            model="gemini-3.8-flash",
            is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertTrue(spec_lock.lock_key)

    def test_02_candidates_matrix(self):
        pool = FakePool()

        def spec(lock_key: bool, switch: bool, models: list[str], target: str | None = None):
            return parse_route_spec(
                make_request(
                    {
                        **({"x-lock-key": "true"} if lock_key else {}),
                        **({"x-allow-model-switch": "true"} if switch else {}),
                        **({"x-target-key": target} if target else {}),
                        **({"x-fallback-models": ",".join(models[1:])} if switch else {}),
                    }
                ),
                None,
                model=models[0],
                is_stream=False,
                timeouts_for=slot_registry.timeouts_for,
            )

        # 01: 单模型 × 所有可用 Key
        c01 = build_candidates(spec(False, False, ["gemini-3.5-flash-lite"]), pool)
        self.assertEqual(len(c01), 2)
        self.assertTrue(all(c[1] == "gemini-3.5-flash-lite" for c in c01))

        # 11: 单 Key × 单模型
        c11 = build_candidates(spec(True, False, ["gemini-2.5-flash"], target="k1"), pool)
        self.assertEqual(len(c11), 1)
        self.assertEqual(c11[0][0]["name"], "k1")
        self.assertEqual(c11[0][1], "gemini-2.5-flash")

        # 10: 单 Key × 数组顺序
        c10 = build_candidates(
            spec(True, True, ["gemini-3.8-flash", "gemini-3.6-flash"], target="k1"), pool
        )
        self.assertEqual([c[1] for c in c10], ["gemini-3.8-flash", "gemini-3.6-flash"])
        self.assertTrue(all(c[0]["name"] == "k1" for c in c10))

        # 00: 数组顺序 × 每个模型的 Key
        c00 = build_candidates(
            spec(False, True, ["gemini-2.5-flash", "gemini-3.6-flash"]), pool
        )
        self.assertEqual(
            [c[1] for c in c00],
            ["gemini-2.5-flash", "gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.6-flash"],
        )

        # 数组顺序即优先级：不做相似度重排
        self.assertEqual(
            [c[1] for c in build_candidates(spec(False, True, ["gemini-3.8-flash", "gemini-3.5-flash-lite"]), pool)][0],
            "gemini-3.8-flash",
        )

    def test_03_timeout_admission(self):
        # 没报预算 -> default
        spec = parse_route_spec(
            make_request(), None, model="gemini-3.5-flash-lite", is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertEqual(spec.timeout_s, 40.0)

        # 低于 floor -> 直接拒
        with self.assertRaises(DeadlineBelowFloor):
            parse_route_spec(
                make_request({"x-request-timeout-ms": "2000"}),
                None,
                model="gemini-3.5-flash-lite",
                is_stream=False,
                timeouts_for=slot_registry.timeouts_for,
            )

        # 高于 ceiling -> 截断并留标记
        spec_clip = parse_route_spec(
            make_request({"x-request-timeout-ms": "600000"}),
            None,
            model="gemini-3.8-flash",
            is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertEqual(spec_clip.timeout_s, 180.0)
        self.assertEqual(spec_clip.timeout_note, "deadline_truncated")

        # 区间内按报的值
        spec_mid = parse_route_spec(
            make_request({"x-request-timeout-ms": "60000"}),
            None,
            model="gemini-3.8-flash",
            is_stream=False,
            timeouts_for=slot_registry.timeouts_for,
        )
        self.assertEqual(spec_mid.timeout_s, 60.0)

    def test_04_deadline_below_floor_returns_400(self):
        resp = self.client.post(
            "/v1/chat/completions",
            headers={"x-request-timeout-ms": "1000"},
            json={
                "model": "gemini-3.5-flash-lite",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()["error"]
        self.assertEqual(body["reason"], "deadline_below_floor")
        self.assertEqual(body["floor_ms"], 20000)
        self.assertEqual(resp.headers.get("x-slot-reason"), "deadline_below_floor")

    @patch("httpx.AsyncClient.get")
    def test_05_ping_endpoint_health(self, mock_get):
        mock_get.return_value = httpx.Response(
            status_code=200,
            json={"models": []},
            request=httpx.Request("GET", "https://generativelanguage.googleapis.com"),
        )
        key_name = key_pool.keys[0]["name"]
        resp = self.client.post(f"/v1/keys/{key_name}/ping")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("latency_ms", resp.json())

    def test_06_no_concurrency_gate(self):
        """P1 起并发数不再阻断授予：shadow 模式下令牌不足也照发，只记 would_wait。"""
        key_info = key_pool.keys[0]
        previous = slot_registry.mode
        slot_registry.mode = "shadow"
        try:
            granted = [
                slot_registry.acquire(key_info, "gemini-3.5-flash-lite") for _ in range(5)
            ]
            self.assertTrue(all(hasattr(item, "model") for item in granted))
            for lease in granted:
                slot_registry.settle(lease, "cancelled")
        finally:
            slot_registry.mode = previous
        self.assertEqual(key_info["model_in_flight"].get("gemini-3.5-flash-lite", 0), 0)

if __name__ == "__main__":
    unittest.main()
