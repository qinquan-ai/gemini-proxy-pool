import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.scheduling.limits import (
    RATE_RATIO,
    SlotRegistry,
    SlotRejection,
    _Bucket,
    build_model_quota,
)


class MutableClock:
    def __init__(self, value: float = 1_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakePool:
    """最小 Key 池替身：账本字段与真实 KeyManager 保持一致。"""

    def __init__(self, limits: dict[str, int] | None = None, keys: int = 1):
        self.keys = [
            {
                "name": f"k{i + 1}",
                "key": f"secret-{i + 1}",
                "disabled": False,
                "model_in_flight": {},
                "in_flight": 0,
            }
            for i in range(keys)
        ]
        self.limits = limits or {}
        self._quotas: dict[tuple[str, str], dict] = {}
        self.usage_calls = 0

    def _get_model_quota(self, key_info: dict, model: str) -> dict:
        slot = (key_info["name"], model)
        if slot not in self._quotas:
            limit = self.limits.get(model, build_model_quota(model).rpd)
            self._quotas[slot] = {
                "used_today": 0,
                "success_today": 0,
                "failure_today": 0,
                "rate_limited_today": 0,
                "limit_rpd": limit,
                "cooldown_until": 0.0,
                "last_used_at": 0.0,
            }
        return self._quotas[slot]

    def _get_next_reset_epoch(self) -> float:
        return 1_000_000.0 + 3600.0

    def record_usage(self, key_str, usage=None, model=None):
        self.usage_calls += 1


class TokenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock()
        self.pool = FakePool()
        self.registry = SlotRegistry(self.pool, clock=self.clock, mode="enforce")
        self.key = self.pool.keys[0]

    def test_01_quota_math(self):
        quota = build_model_quota("gemini-3.5-flash-lite")
        self.assertEqual(quota.rpm, 15)
        self.assertAlmostEqual(quota.rate, 15 * RATE_RATIO / 60)
        self.assertEqual(quota.burst, 3)

        flash = build_model_quota("gemini-3.8-flash")
        self.assertEqual(flash.burst, 1)
        self.assertAlmostEqual(flash.rate, 5 * RATE_RATIO / 60)

        gemma = build_model_quota("gemma-4-31b-it")
        self.assertEqual(gemma.burst, 6)

    def test_02_burst_never_breaks_rpm_window(self):
        """任意 60s 窗口最多放行 burst + 60·rate，必须 ≤ RPM。"""
        for model in (
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-2.5-flash",
            "gemma-4-31b-it",
        ):
            quota = build_model_quota(model)
            window = quota.burst + 60 * quota.rate
            self.assertLessEqual(window, quota.rpm + 1e-9, model)

    def test_03_refill_is_continuous(self):
        model = "gemini-3.5-flash-lite"
        for _ in range(3):
            lease = self.registry.acquire(self.key, model, self.clock.value)
            self.assertNotIsInstance(lease, SlotRejection)
            self.registry.settle(lease, "cancelled")

        # 桶已空
        rejected = self.registry.acquire(self.key, model, self.clock.value)
        self.assertIsInstance(rejected, SlotRejection)
        self.assertEqual(rejected.reason, "token_wait")
        # 补一个令牌所需秒数 = 60 / (rpm × RATE_RATIO)，不写死数值
        expected_wait_s = 60.0 / (15 * RATE_RATIO)
        self.assertAlmostEqual(rejected.retry_after_ms / 1000, expected_wait_s, places=1)

        # 低速模型也必须能补满：flash 按同一比例补充
        flash_clock = MutableClock()
        flash_pool = FakePool()
        flash_registry = SlotRegistry(flash_pool, clock=flash_clock, mode="enforce")
        lease = flash_registry.acquire(flash_pool.keys[0], "gemini-3.8-flash")
        flash_registry.settle(lease, "cancelled")
        blocked = flash_registry.acquire(flash_pool.keys[0], "gemini-3.8-flash")
        self.assertEqual(blocked.reason, "token_wait")
        flash_clock.advance(60.0 / (5 * RATE_RATIO) + 0.5)
        refilled = flash_registry.acquire(flash_pool.keys[0], "gemini-3.8-flash")
        self.assertNotIsInstance(refilled, SlotRejection)

    def test_04_settlement_table(self):
        model = "gemini-3.5-flash-lite"
        quota = self.pool._get_model_quota(self.key, model)

        bucket = self.registry._buckets.setdefault(
            ("k1", model), _Bucket(self.clock.value, 3)
        )

        def run(outcome):
            # 推进时钟补令牌，并清掉上一次失败留下的冷却，隔离「结算口径」这一项
            self.clock.advance(20)
            quota["cooldown_until"] = 0.0
            bucket.fail_streak = 0
            lease = self.registry.acquire(self.key, model)
            self.assertNotIsInstance(lease, SlotRejection)
            self.registry.settle(lease, outcome)
            return quota["used_today"]

        self.assertEqual(run("ok"), 1)
        self.assertEqual(quota["success_today"], 1)
        self.assertEqual(run("server_error"), 2)
        self.assertEqual(run("timeout"), 3)
        self.assertEqual(run("client_error"), 3)
        self.assertEqual(run("cancelled"), 3)

        # 429 不记 used，但会冷却
        before = quota["used_today"]
        self.clock.advance(20)
        quota["cooldown_until"] = 0.0
        bucket.fail_streak = 0
        lease = self.registry.acquire(self.key, model)
        self.registry.settle(lease, "rate_limited", retry_after_s=7)
        self.assertEqual(quota["used_today"], before)
        self.assertGreater(quota["cooldown_until"], self.clock.value)

    def test_05_settle_is_idempotent(self):
        model = "gemini-3.5-flash-lite"
        quota = self.pool._get_model_quota(self.key, model)
        lease = self.registry.acquire(self.key, model)
        self.registry.settle(lease, "ok")
        self.registry.settle(lease, "ok")
        self.assertEqual(quota["used_today"], 1)
        self.assertEqual(self.key["model_in_flight"][model], 0)

    def test_06_rpd_is_not_oversold(self):
        model = "gemini-2.5-flash"
        self.pool.limits[model] = 2
        bucket = self.registry._buckets.setdefault(
            ("k1", model), _Bucket(self.clock.value, 1)
        )
        quota = self.pool._get_model_quota(self.key, model)
        grants = 0
        for _ in range(5):
            self.clock.advance(20)
            quota["cooldown_until"] = 0.0
            bucket.fail_streak = 0
            lease = self.registry.acquire(self.key, model)
            if isinstance(lease, SlotRejection):
                self.assertEqual(lease.reason, "rpd_exhausted")
                break
            grants += 1
            self.registry.settle(lease, "server_error")
        self.assertEqual(grants, 2)
        self.assertEqual(self.pool._get_model_quota(self.key, model)["used_today"], 2)

    def test_07_cooldown_is_per_slot(self):
        self.registry.settle(self.registry.acquire(self.key, "gemini-3.8-flash"), "rate_limited", retry_after_s=10)
        blocked = self.registry.acquire(self.key, "gemini-3.8-flash")
        self.assertEqual(blocked.reason, "cooling")
        self.assertGreater(blocked.retry_after_ms, 0)

        # 同 Key 的另一个模型不受影响
        other = self.registry.acquire(self.key, "gemini-3.6-flash")
        self.assertNotIsInstance(other, SlotRejection)

    def test_08_circuit_breaker_after_two_failures(self):
        model = "gemini-3.8-flash"
        for _ in range(2):
            self.clock.advance(20)
            lease = self.registry.acquire(self.key, model)
            self.assertNotIsInstance(lease, SlotRejection)
            self.registry.settle(lease, "server_error")
        quota = self.pool._get_model_quota(self.key, model)
        remaining = quota["cooldown_until"] - self.clock.value
        self.assertGreater(remaining, 200)

        # 成功一次立即清零
        self.registry.settle(self.registry.acquire(self.key, "gemini-3.6-flash"), "ok")

    def test_09_lazy_bucket_for_uncommon_model(self):
        model = "gemma-4-31b-it"
        self.assertNotIn(("k1", model), self.registry._buckets)
        lease = self.registry.acquire(self.key, model)
        self.assertNotIsInstance(lease, SlotRejection)
        self.assertIn(("k1", model), self.registry._buckets)
        self.assertEqual(self.registry.quota_for(model).rpm, 30)

    def test_10_shadow_mode_never_rejects_on_tokens_or_rpd(self):
        registry = SlotRegistry(self.pool, clock=self.clock, mode="shadow")
        model = "gemini-2.5-flash"
        self.pool.limits[model] = 1
        for _ in range(4):
            lease = registry.acquire(self.key, model)
            self.assertNotIsInstance(lease, SlotRejection)
            registry.settle(lease, "ok")
        stats = registry.stats()
        self.assertGreater(stats["would_exhaust"], 0)

    def test_11_off_mode_skips_tokens(self):
        registry = SlotRegistry(self.pool, clock=self.clock, mode="off")
        for _ in range(6):
            lease = registry.acquire(self.key, "gemini-3.8-flash")
            self.assertNotIsInstance(lease, SlotRejection)
            registry.settle(lease, "cancelled")
        bucket = registry._buckets[("k1", "gemini-3.8-flash")]
        self.assertEqual(bucket.tokens, registry.quota_for("gemini-3.8-flash").burst)

    def test_12_in_flight_bound_is_identity(self):
        quota = build_model_quota("gemini-3.5-flash-lite")
        self.assertAlmostEqual(
            quota.in_flight_bound, quota.burst + quota.rate * quota.timeouts.ceiling_s
        )
        self.assertGreater(quota.in_flight_bound, quota.burst)

    def test_13_timeout_profiles(self):
        lite = build_model_quota("gemini-3.5-flash-lite").timeouts
        self.assertEqual((lite.floor_s, lite.default_s, lite.ceiling_s), (20.0, 40.0, 90.0))
        flash = build_model_quota("gemini-3.8-flash").timeouts
        self.assertEqual((flash.floor_s, flash.default_s, flash.ceiling_s), (45.0, 90.0, 180.0))
        text = build_model_quota("gemma-4-31b-it").timeouts
        self.assertEqual((text.floor_s, text.default_s, text.ceiling_s), (40.0, 75.0, 150.0))

        self.assertEqual(lite.clamp(None), (40.0, None))
        self.assertEqual(lite.clamp(25), (25.0, None))
        self.assertEqual(lite.clamp(500), (90.0, "deadline_truncated"))
        self.assertEqual(lite.clamp(5), (None, "deadline_below_floor"))


if __name__ == "__main__":
    unittest.main()

