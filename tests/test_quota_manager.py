import json
import tempfile
import unittest
from pathlib import Path

from app.core.key_manager import KeyManager, PACIFIC_TZ


class MutableClock:
    def __init__(self, value: float = 1757300000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class QuotaManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(1757300000.0) # Fixed timestamp
        self.manager = KeyManager(
            keys_value="primary|key-one,secondary|key-two",
            clock=self.clock,
        )

    def test_model_quota_deduction(self):
        # Initial quota for flash model is 20
        status = self.manager.get_quota_status()
        summary = status["summary_by_model"]["gemini-3.5-flash"]
        self.assertEqual(summary["total_limit"], 40)
        self.assertEqual(summary["total_used"], 0)
        self.assertEqual(summary["total_remaining"], 40)

        # Acquire key for gemini-3.5-flash
        key = self.manager.acquire_key(model="gemini-3.5-flash")
        self.assertIsNotNone(key)
        self.manager.mark_success(key["key"], model="gemini-3.5-flash")

        # Quota should reflect 1 used
        status = self.manager.get_quota_status()
        summary = status["summary_by_model"]["gemini-3.5-flash"]
        self.assertEqual(summary["total_used"], 1)
        self.assertEqual(summary["total_remaining"], 39)

    def test_key_rotation_on_model_exhaustion(self):
        # Exhaust gemini-3.5-flash on key 1 (limit: 20)
        key1 = self.manager.acquire_key(model="gemini-3.5-flash")
        for _ in range(20):
            self.manager.mark_success(key1["key"], model="gemini-3.5-flash")

        # Next acquire for gemini-3.5-flash should route to key 2
        key2 = self.manager.acquire_key(model="gemini-3.5-flash")
        self.assertIsNotNone(key2)
        self.assertEqual(key2["name"], "secondary")

        # Exhaust key 2 as well
        for _ in range(20):
            self.manager.mark_success(key2["key"], model="gemini-3.5-flash")

        # Both keys exhausted for gemini-3.5-flash
        exhausted = self.manager.acquire_key(model="gemini-3.5-flash")
        self.assertIsNone(exhausted)

        # But another model (e.g. gemini-3.6-flash or gemini-3.5-flash-lite) should STILL be available!
        lite_key = self.manager.acquire_key(model="gemini-3.5-flash-lite")
        self.assertIsNotNone(lite_key)

    def test_daily_rollover_resets_quota_and_saves_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            mgr = KeyManager(
                keys_value="primary|key-one",
                clock=self.clock,
                state_file=state_file,
            )
            key = mgr.acquire_key(model="gemini-3.5-flash")
            for _ in range(5):
                mgr.mark_success(key["key"], model="gemini-3.5-flash")

            status = mgr.get_quota_status()
            self.assertEqual(status["summary_by_model"]["gemini-3.5-flash"]["total_used"], 5)

            # Advance clock by 24 hours (86400 seconds) to next Pacific Day
            self.clock.value += 86400.0
            mgr._check_and_reset_daily()

            # Quota used today should be reset to 0
            new_status = mgr.get_quota_status()
            self.assertEqual(new_status["summary_by_model"]["gemini-3.5-flash"]["total_used"], 0)
            self.assertEqual(new_status["summary_by_model"]["gemini-3.5-flash"]["total_remaining"], 20)

            # Yesterday's 5 uses should be recorded in history
            self.assertGreater(len(new_status["history"]), 0)


if __name__ == "__main__":
    unittest.main()
