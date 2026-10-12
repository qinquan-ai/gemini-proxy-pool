import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import (
    app,
    key_pool,
    REQUEST_TRACES,
    record_trace,
    build_route_candidates,
    extract_route_mode,
    extract_fallback_models,
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

    def test_09_model_concurrency_slots(self):
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        can_use, reason = key_pool.can_consume(key_info, "gemini-3.8-flash")
        self.assertTrue(can_use)
        self.assertEqual(reason, "Available")

        # Concurrency limit for gemini-3.8-flash is 2
        acq1 = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertIsNotNone(acq1)
        acq2 = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertIsNotNone(acq2)

        # 3rd acquire for gemini-3.8-flash should be blocked by ModelConcurrencyFull
        can_use3, reason3 = key_pool.can_consume(key_info, "gemini-3.8-flash")
        self.assertFalse(can_use3)
        self.assertEqual(reason3, "ModelConcurrencyFull")
        acq_blocked = key_pool.acquire_key(model="gemini-3.8-flash", target_key_name=key_name)
        self.assertIsNone(acq_blocked)

        # Another model (gemini-3.5-flash-lite) on same key can still be acquired concurrently
        can_use_flash, reason_flash = key_pool.can_consume(key_info, "gemini-3.5-flash-lite")
        self.assertTrue(can_use_flash)
        self.assertEqual(reason_flash, "Available")
        acq_flash = key_pool.acquire_key(model="gemini-3.5-flash-lite", target_key_name=key_name)
        self.assertIsNotNone(acq_flash)

        # Release and verify slot availability restores
        key_pool.release(acq1["key"], model="gemini-3.8-flash")
        key_pool.release(acq2["key"], model="gemini-3.8-flash")
        key_pool.release(acq_flash["key"], model="gemini-3.5-flash-lite")
        can_use_again, _ = key_pool.can_consume(key_info, "gemini-3.8-flash")
        self.assertTrue(can_use_again)

    def test_10_ranked_models_tiering(self):
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        ranked = key_pool.get_ranked_models(target_model="gemini-3.8-flash", key_info=key_info)
        self.assertGreater(len(ranked), 0)
        self.assertEqual(ranked[0]["model"], "gemini-3.8-flash")
        self.assertEqual(ranked[0]["tier"], "A")

        m_quota = key_pool._get_model_quota(key_info, "gemini-3.8-flash")
        m_quota["success_today"] = 14
        ranked_after_usage = key_pool.get_ranked_models(target_model="gemini-3.8-flash", key_info=key_info)
        target_item = next(item for item in ranked_after_usage if item["model"] == "gemini-3.8-flash")
        self.assertEqual(target_item["tier"], "B")
        self.assertEqual(target_item["usage_ratio"], 0.7)
        for a_item in [item for item in ranked_after_usage if item["tier"] == "A"]:
            self.assertLessEqual(a_item["usage_ratio"], 0.6)
        m_quota["success_today"] = 0

    def test_11_route_candidate_generation(self):
        key_name = key_pool.keys[0]["name"]

        cands_11 = build_route_candidates("11", "gemini-2.5-flash", target_key_name=key_name)
        self.assertEqual(len(cands_11), 1)
        self.assertEqual(cands_11[0][0]["name"], key_name)
        self.assertEqual(cands_11[0][1], "gemini-2.5-flash")

        cands_10 = build_route_candidates("10", "gemini-3.8-flash", target_key_name=key_name)
        self.assertGreater(len(cands_10), 1)
        self.assertTrue(all(cand[0]["name"] == key_name for cand in cands_10))
        models_10 = [cand[1] for cand in cands_10]
        self.assertIn("gemini-3.8-flash", models_10)

        cands_01 = build_route_candidates("01", "gemini-2.5-flash")
        self.assertGreater(len(cands_01), 0)
        self.assertTrue(all(cand[1] == "gemini-2.5-flash" for cand in cands_01))

        cands_00 = build_route_candidates("00", "gemini-2.5-flash")
        self.assertGreaterEqual(len(cands_00), len(cands_01))

    def test_12_cancellation_integrity(self):
        key_name = key_pool.keys[0]["name"]
        key_info = key_pool.find_key_by_name(key_name)
        initial_fail_count = key_info.get("failure_count", 0)
        slot_acq = key_pool.acquire_key(model="gemini-2.5-flash", target_key_name=key_name)
        self.assertIsNotNone(slot_acq)
        self.assertEqual(key_info["model_in_flight"]["gemini-2.5-flash"], 1)

        key_pool.release(slot_acq["key"], model="gemini-2.5-flash")
        self.assertEqual(key_info["model_in_flight"]["gemini-2.5-flash"], 0)
        self.assertEqual(key_info.get("failure_count", 0), initial_fail_count)


if __name__ == "__main__":
    unittest.main()
