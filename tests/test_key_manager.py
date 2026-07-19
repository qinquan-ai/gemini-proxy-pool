import json
import tempfile
import unittest
from pathlib import Path

from app.core.key_manager import KeyManager


class MutableClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class KeyManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.manager = KeyManager(
            keys_value="one|key-one,two|key-two,three|key-three",
            clock=self.clock,
        )

    def test_round_robin(self):
        selected = [self.manager.acquire_key()["name"] for _ in range(4)]
        self.assertEqual(selected, ["one", "two", "three", "one"])

    def test_all_cooling_returns_none(self):
        for _ in range(3):
            key = self.manager.acquire_key()
            self.manager.mark_failure(
                key["key"],
                reason="quota",
                cooldown_seconds=60,
                rate_limited=True,
            )
        self.assertIsNone(self.manager.acquire_key())

    def test_key_recovers_after_cooldown(self):
        key = self.manager.acquire_key()
        self.manager.mark_failure(
            key["key"], reason="quota", cooldown_seconds=60, rate_limited=True
        )
        self.clock.value += 61
        names = [self.manager.acquire_key()["name"] for _ in range(3)]
        self.assertIn("one", names)

    def test_disabled_key_is_skipped(self):
        first = self.manager.acquire_key()
        self.manager.mark_failure(first["key"], reason="invalid", disable=True)
        selected = [self.manager.acquire_key()["name"] for _ in range(3)]
        self.assertNotIn("one", selected)

    def test_status_tracks_in_flight_and_success(self):
        selected = self.manager.acquire_key()
        status = self.manager.get_status()
        self.assertEqual(status["in_flight"], 1)

        self.manager.mark_success(selected["key"])
        status = self.manager.get_status()
        self.assertEqual(status["in_flight"], 0)
        self.assertEqual(status["total_successes"], 1)
        self.assertEqual(status["success_rate"], 100.0)

    def test_usage_and_health_state_survive_restart_without_storing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "key-pool.json"
            first = KeyManager(
                keys_value="primary|secret-api-key",
                clock=self.clock,
                state_file=state_file,
            )
            selected = first.acquire_key()
            first.mark_success(
                selected["key"],
                {
                    "promptTokenCount": 120,
                    "candidatesTokenCount": 30,
                    "thoughtsTokenCount": 40,
                    "totalTokenCount": 150,
                    "cachedContentTokenCount": 20,
                },
            )

            persisted_text = state_file.read_text(encoding="utf-8")
            self.assertNotIn("secret-api-key", persisted_text)
            self.assertEqual(json.loads(persisted_text)["version"], 1)

            restored = KeyManager(
                keys_value="primary|secret-api-key",
                clock=self.clock,
                state_file=state_file,
            ).get_status()
            self.assertEqual(restored["total_requests"], 1)
            self.assertEqual(restored["total_successes"], 1)
            self.assertEqual(restored["input_tokens"], 120)
            self.assertEqual(restored["output_tokens"], 30)
            self.assertEqual(restored["thought_tokens"], 40)
            self.assertEqual(restored["total_tokens"], 150)
            self.assertEqual(restored["cached_tokens"], 20)
            self.assertEqual(restored["in_flight"], 0)


if __name__ == "__main__":
    unittest.main()
