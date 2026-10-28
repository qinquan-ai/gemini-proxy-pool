import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import (
    REQUEST_TRACES,
    app,
    build_candidates,
    key_pool,
    parse_route_spec,
    record_trace,
    slot_registry,
)
from starlette.requests import Request


def route_spec(lock_key=False, switch=False, fallbacks=None, target=None, model="gemini-3.5-flash-lite"):
    headers = {}
    if lock_key:
        headers["x-lock-key"] = "true"
    if switch:
        headers["x-allow-model-switch"] = "true"
    if fallbacks:
        headers["x-fallback-models"] = ",".join(fallbacks)
    if target:
        headers["x-target-key"] = target
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(k.encode(), str(v).encode()) for k, v in headers.items()],
        "query_string": b"",
    }
    return parse_route_spec(
        Request(scope),
        None,
        model=model,
        is_stream=False,
        timeouts_for=slot_registry.timeouts_for,
    )


class GatewayRefactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_01_index_page(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("StudioKey Gateway", resp.text)
        self.assertIn("cards-container", resp.text)
        self.assertIn("key-cards-grid", resp.text)
        self.assertIn("traces-body", resp.text)
        self.assertIn("key-detail-drawer", resp.text)

    def test_02_healthz(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("status", data)
        self.assertIn("version", data)

    def test_03_status_endpoint_key_attributes(self):
        resp = self.client.get("/v1/status")
        self.assertEqual(resp.status_code, 200)
        status_data = resp.json()
        self.assertIn("pool", status_data)
        pool = status_data["pool"]
        self.assertGreater(len(pool), 0)
        first_key = pool[0]
        self.assertIn("name", first_key)
        self.assertIn("models", first_key)
        self.assertIn("used_today_total", first_key)
        self.assertIn("limit_rpd_total", first_key)
        self.assertIn("gemini-3.5-flash-lite", first_key["models"])
        self.assertIn("today_req_count", first_key)
        self.assertIn("today_success_count", first_key)
        self.assertIn("today_failure_count", first_key)
        self.assertIn("today_rate_limit_count", first_key)
        self.assertIn("today_peak_tpm", first_key)
        self.assertIn("current_tpm", first_key)

    def test_04_key_toggle_and_unfreeze(self):
        key_name = key_pool.keys[0]["name"]
        resp = self.client.post(f"/v1/keys/{key_name}/unfreeze")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "ok")

        resp = self.client.post(f"/v1/keys/{key_name}/toggle")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("disabled"))

        resp = self.client.post(f"/v1/keys/{key_name}/toggle")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json().get("disabled"))

    def test_05_trace_recording(self):
        key_name = key_pool.keys[0]["name"]
        record_trace(
            protocol="Google Native",
            method="POST",
            endpoint="/v1beta/models/gemini-3.5-flash-lite:generateContent",
            model="gemini-3.5-flash-lite",
            key_name=key_name,
            status_code=200,
            latency_ms=123.4,
            tokens={"totalTokenCount": 50},
            fail_fast=True,
        )

        resp = self.client.get("/v1/traces")
        self.assertEqual(resp.status_code, 200)
        traces = resp.json()["traces"]
        self.assertGreaterEqual(len(traces), 1)
        latest = traces[0]
        self.assertEqual(latest["model"], "gemini-3.5-flash-lite")
        self.assertTrue(latest["fail_fast"])
        self.assertEqual(latest["status_code"], 200)
        self.assertEqual(latest["protocol"], "Google Native")

    def test_06_modular_ui_static_assets(self):
        resp = self.client.get("/assets/ui/index.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("defineUiComponents", resp.text)

        resp = self.client.get("/assets/ui/tokens/glass.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--glass-bg", resp.text)

        resp = self.client.get("/assets/ui/components/card/KeyCard.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("KeyCard", resp.text)

        resp = self.client.get("/assets/ui/components/chat/PromptChat.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("PromptChat", resp.text)

    def test_07_target_key_acquisition(self):
        key_name = key_pool.keys[0]["name"]
        acquired = key_pool.acquire_key(model="gemini-2.5-flash", target_key_name=key_name)
        self.assertIsNotNone(acquired)
        self.assertEqual(acquired["name"], key_name)
        key_pool.mark_success(acquired["key"], model="gemini-2.5-flash")

    def test_08_transient_cooling_and_min_cooldown(self):
        key_name = key_pool.keys[0]["name"]
        acquired = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertIsNotNone(acquired)
        key_pool.mark_failure(
            acquired["key"],
            reason="This model is currently experiencing high demand",
            cooldown_seconds=3,
            model="gemini-3.8-flash",
        )
        cooling_acq = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name, allow_transient_cooling=False)
        self.assertIsNone(cooling_acq)

        retry_acq = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name, allow_transient_cooling=True)
        self.assertIsNotNone(retry_acq)
        self.assertEqual(retry_acq["name"], key_name)

        min_cd = key_pool.get_min_cooldown_remaining(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertTrue(0.0 <= min_cd <= 3.0)
        key_pool.release(retry_acq["key"], model="gemini-3.8-flash")
        key_pool.unfreeze_key(key_name)

    def test_09_no_concurrency_gate(self):
        """并发不再作为准入条件：同一 (key, model) 可连续授予，靠令牌限速。"""
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        can_use, reason = key_pool.can_consume(key_info, "gemini-3.8-flash")
        self.assertTrue(can_use)
        self.assertEqual(reason, "Available")

        previous = slot_registry.mode
        slot_registry.mode = "shadow"
        try:
            leases = [slot_registry.acquire(key_info, "gemini-3.8-flash") for _ in range(3)]
            self.assertTrue(all(hasattr(item, "model") for item in leases))
        finally:
            slot_registry.mode = previous
        self.assertEqual(key_info["model_in_flight"].get("gemini-3.8-flash", 0), 3)
        for lease in leases:
            slot_registry.settle(lease, "cancelled")
        self.assertEqual(key_info["model_in_flight"]["gemini-3.8-flash"], 0)

    def test_10_rpd_exhaustion_removes_candidate(self):
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        m_quota = key_pool._get_model_quota(key_info, "gemini-3.8-flash")
        original = m_quota.get("used_today", 0)
        try:
            m_quota["used_today"] = m_quota["limit_rpd"]
            candidates = build_candidates(
                route_spec(lock_key=True, target=key_name, model="gemini-3.8-flash"),
                key_pool,
            )
            self.assertEqual(candidates, [])
        finally:
            m_quota["used_today"] = original

    def test_11_route_candidate_generation(self):
        key_name = key_pool.keys[0]["name"]
        models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
        backup = []
        for k in key_pool.keys:
            for m in models:
                q = key_pool._get_model_quota(k, m)
                backup.append((q, q.get("used_today", 0), q.get("cooldown_until", 0.0)))
                q["used_today"] = 0
                q["cooldown_until"] = 0.0
        try:
            cands_single = build_candidates(
                route_spec(lock_key=True, target=key_name), key_pool
            )
            self.assertEqual(len(cands_single), 1)
            self.assertEqual(cands_single[0][0]["name"], key_name)
            self.assertEqual(cands_single[0][1], "gemini-3.5-flash-lite")

            cands_family = build_candidates(
                route_spec(switch=True, fallbacks=["gemini-3.1-flash-lite"]), key_pool
            )
            self.assertEqual({c[1] for c in cands_family}, set(models))
            # first-fit：第一个模型的所有候选排在第二个模型之前
            self.assertEqual(cands_family[0][1], "gemini-3.5-flash-lite")

            cands_locked_switch = build_candidates(
                route_spec(
                    lock_key=True,
                    switch=True,
                    target=key_name,
                    fallbacks=["gemini-3.1-flash-lite"],
                ),
                key_pool,
            )
            self.assertTrue(all(c[0]["name"] == key_name for c in cands_locked_switch))
        finally:
            for q, used, cooldown in backup:
                q["used_today"] = used
                q["cooldown_until"] = cooldown

    def test_12_cancellation_integrity(self):
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        initial_fail_count = key_info.get("failure_count", 0)
        prior = slot_registry.mode
        slot_registry.mode = "shadow"
        try:
            lease = slot_registry.acquire(key_info, "gemini-2.5-flash")
        finally:
            slot_registry.mode = prior
        self.assertIsNotNone(lease)
        self.assertEqual(key_info["model_in_flight"]["gemini-2.5-flash"], 1)
        slot_registry.abort(lease)
        self.assertEqual(key_info["model_in_flight"]["gemini-2.5-flash"], 0)
        self.assertEqual(key_info.get("failure_count", 0), initial_fail_count)

if __name__ == "__main__":
    unittest.main()
