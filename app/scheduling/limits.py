"""令牌桶限速与配额账本。

单一职责：判定某个 (key, model) 此刻能否发出一次请求，以及一次请求结束后如何记账。
候选选择见 ``scheduling/policy.py``，请求执行见 ``consumer/dispatch.py``。

账本字段复用 KeyManager 的 ``model_quotas[model]``，不另存一份：

* ``used_today``      今日已发往上游的请求数，限流与面板都读它（权威账本）
* ``success_today``   今日成功数，仅统计，不参与限流判定
* ``cooldown_until``  冷却截止时刻

令牌与预留计数属于运行时状态，只在本模块内存中，不落盘。
"""

import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from app.core.key_manager import CANONICAL_MODELS

# 稳态占比与突发占比分开固定，不做配置项：
# 一旦可配就等于允许把 burst 调出 RPM 上界，硬保证立刻失效。
# 硬上限 = (RATE_RATIO + BURST_RATIO) × rpm = 0.9 × rpm
# 留 10% 缓冲：我方滑动窗口与上游固定窗口不重合时可能叠加
RATE_RATIO = 0.7
BURST_RATIO = 0.2

# 耗时样本只留原值供人工排查，不做分位统计（超时是右删失数据）
RECENT_COST_SAMPLES = 20


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def rate_limit_mode() -> str:
    raw = os.getenv("RATE_LIMIT_MODE", "shadow").strip().lower()
    return raw if raw in ("off", "shadow", "enforce") else "shadow"


@dataclass(frozen=True)
class TimeoutProfile:
    """一类请求的超时区间。低于 floor 直接拒，高于 ceiling 截断。"""

    name: str
    floor_s: float
    default_s: float
    ceiling_s: float
    ttfb_s: float | None = None

    def clamp(self, requested_s: float | None) -> tuple[float | None, str | None]:
        """返回 (生效超时, 错误码)。错误码非空表示应当拒绝，不投递。"""
        if requested_s is None:
            return self.default_s, None
        if requested_s < self.floor_s:
            return None, "deadline_below_floor"
        if requested_s > self.ceiling_s:
            return self.ceiling_s, "deadline_truncated"
        return requested_s, None


def build_timeout_profiles() -> dict[str, TimeoutProfile]:
    ttfb = _env_float("TTFB_STREAM_S", 15.0)
    return {
        "lite": TimeoutProfile(
            "lite",
            _env_float("TIMEOUT_LITE_FLOOR_S", 20.0),
            _env_float("TIMEOUT_LITE_DEFAULT_S", 40.0),
            _env_float("TIMEOUT_LITE_CEIL_S", 90.0),
            ttfb,
        ),
        "flash": TimeoutProfile(
            "flash",
            _env_float("TIMEOUT_FLASH_FLOOR_S", 45.0),
            _env_float("TIMEOUT_FLASH_DEFAULT_S", 90.0),
            _env_float("TIMEOUT_FLASH_CEIL_S", 180.0),
            ttfb,
        ),
        "text": TimeoutProfile(
            "text",
            _env_float("TIMEOUT_TEXT_FLOOR_S", 40.0),
            _env_float("TIMEOUT_TEXT_DEFAULT_S", 75.0),
            _env_float("TIMEOUT_TEXT_CEIL_S", 150.0),
            ttfb,
        ),
    }


TIMEOUT_PROFILES = build_timeout_profiles()
DEFAULT_TIMEOUT_PROFILE = TIMEOUT_PROFILES["flash"]


@dataclass(frozen=True)
class ModelQuota:
    """每模型一份静态配额，由 CANONICAL_MODELS 推导。"""

    model: str
    rpm: int
    rpd: int
    tpm: int
    tier: str
    timeouts: TimeoutProfile

    @property
    def rate(self) -> float:
        """令牌补充速率，个/秒。"""
        return self.rpm * RATE_RATIO / 60.0

    @property
    def burst(self) -> float:
        """桶容量。取 rpm 的 20%，与 RATE_RATIO（0.7）相加后硬上限为 0.9×rpm。

        用 round 而非 floor：安全系数是浮点数，15 × 0.19999999999999996 得 2.9999999999999996，
        floor 会掉到 2，白丢一个突发额度。
        """
        return float(max(1, round(self.rpm * BURST_RATIO)))

    @property
    def in_flight_bound(self) -> float:
        """在途上界 = burst + rate × ceiling。恒等式，只用于容量核算与泄漏告警。"""
        return self.burst + self.rate * self.timeouts.ceiling_s


def build_model_quota(model: str) -> ModelQuota:
    meta = CANONICAL_MODELS.get(model, {})
    tier = meta.get("tier", "flash")
    return ModelQuota(
        model=model,
        rpm=int(meta.get("limit_rpm") or 15),
        rpd=int(meta.get("limit_rpd") or 20),
        tpm=int(meta.get("tpm") or 250_000),
        tier=tier,
        timeouts=TIMEOUT_PROFILES.get(tier, DEFAULT_TIMEOUT_PROFILE),
    )


@dataclass
class Lease:
    """一次已授予的上游调用许可。settle 必须与 acquire 一一对应。"""

    key_info: dict
    model: str
    acquired_at: float
    settled: bool = False

    @property
    def key_name(self) -> str:
        return self.key_info.get("name", "N/A")

    @property
    def key(self) -> str:
        return self.key_info.get("key", "")


@dataclass
class SlotRejection:
    """不可授予的原因。retry_after_ms 是精确算式，不是估计。"""

    reason: str
    model: str
    key_name: str = ""
    retry_after_ms: int = 0
    detail: str = ""


class _Bucket:
    __slots__ = ("tokens", "last_refill", "reserved", "fail_streak")

    def __init__(self, now: float, burst: float):
        self.tokens = burst
        self.last_refill = now
        self.reserved = 0
        self.fail_streak = 0


class SlotRegistry:
    """(key, model) 粒度的令牌桶与账本。

    线程安全：面板接口与请求协程可能在不同线程，统一用 RLock 保护。
    """

    def __init__(self, key_pool, clock=None, mode: str | None = None):
        self._pool = key_pool
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._quotas: dict[str, ModelQuota] = {}
        self._recent: dict[tuple[str, str], deque] = {}
        self._recent_outcomes: dict[tuple[str, str], deque] = {}
        self._counters: dict[tuple[str, str], dict[str, int]] = {}
        self._would_wait = 0
        self._would_exhaust = 0
        self.mode = mode or rate_limit_mode()
        self.transient_cooldown_s = _env_float("TRANSIENT_COOLDOWN_S", 3.0)
        self.fail_threshold = int(_env_float("SLOT_FAIL_THRESHOLD", 2.0))
        self.fail_cooldown_s = _env_float("SLOT_FAIL_COOLDOWN_S", 300.0)

    # ---------- 查询 ----------

    def quota_for(self, model: str) -> ModelQuota:
        with self._lock:
            quota = self._quotas.get(model)
            if quota is None:
                quota = build_model_quota(model)
                self._quotas[model] = quota
            return quota

    def timeouts_for(self, model: str) -> TimeoutProfile:
        return self.quota_for(model).timeouts

    def _bucket(self, key_name: str, model: str, now: float) -> _Bucket:
        slot = (key_name, model)
        bucket = self._buckets.get(slot)
        if bucket is None:
            bucket = _Bucket(now, self.quota_for(model).burst)
            self._buckets[slot] = bucket
        return bucket

    def _refill(self, bucket: _Bucket, quota: ModelQuota, now: float) -> None:
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(quota.burst, bucket.tokens + elapsed * quota.rate)
        bucket.last_refill = now

    def _slot_state(self, key_info: dict, model: str, now: float) -> dict:
        quota = self.quota_for(model)
        m_quota = self._pool._get_model_quota(key_info, model)
        bucket = self._bucket(key_info.get("name", ""), model, now)
        self._refill(bucket, quota, now)
        used = int(m_quota.get("used_today", 0))
        reserved = bucket.reserved
        # 上限以账本里记的 limit_rpd 为准：状态文件可能保留旧值，
        # 静态表只作为首次创建时的初值。
        limit = int(m_quota.get("limit_rpd", quota.rpd))
        cooldown_until = float(m_quota.get("cooldown_until", 0.0) or 0.0)
        return {
            "quota": quota,
            "m_quota": m_quota,
            "bucket": bucket,
            "used": used,
            "reserved": reserved,
            "limit": limit,
            "remaining": max(0, limit - used - reserved),
            "cooldown_s": max(0.0, cooldown_until - now),
            "tokens": bucket.tokens,
        }

    def snapshot(self, key_info: dict, model: str) -> dict:
        """给面板与 /v1/quota 用的只读快照。

        不创建桶：没被请求过的槽位按「满桶」报告，避免面板把 11 个模型全部实例化。
        """
        with self._lock:
            now = self._clock()
            quota = self.quota_for(model)
            m_quota = self._pool._get_model_quota(key_info, model)
            name = key_info.get("name", "")
            bucket = self._buckets.get((name, model))
            used = int(m_quota.get("used_today", 0))
            limit = int(m_quota.get("limit_rpd", quota.rpd))
            if bucket is None:
                reserved, tokens = 0, quota.burst
            else:
                self._refill(bucket, quota, now)
                reserved, tokens = bucket.reserved, bucket.tokens
            cooldown = float(m_quota.get("cooldown_until", 0.0) or 0.0) - now
            recent = self._recent.get((name, model), ())
            counters = self._counters.get((name, model), {})
            return {
                "limit_rpd": limit,
                "used_today": used,
                "remaining": max(0, limit - used - reserved),
                "in_flight": reserved,
                "tokens": round(tokens, 2),
                "burst": quota.burst,
                "rate": round(quota.rate, 4),
                "cooldown_remaining_sec": max(0, int(cooldown)),
                "last_cost_ms": int(recent[-1]) if recent else 0,
                "recent_cost_ms": list(recent),
                "rate_limited_today": int(counters.get("rate_limited", 0)),
                "timeout_today": int(counters.get("timeout", 0)),
                "failure_today": int(counters.get("server_error", 0)),
                "success_today": int(m_quota.get("success_today", 0)),
            }

    def failure_ratio(self, key_info: dict, model: str) -> float:
        """最近 20 次里超时 + 5xx 的占比，用于告警 3。"""
        with self._lock:
            outcomes = self._recent_outcomes.get((key_info.get("name", ""), model), ())
            if not outcomes:
                return 0.0
            bad = sum(1 for item in outcomes if item in ("server_error", "timeout"))
            return bad / len(outcomes)

    def stats(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "slots": len(self._buckets),
                "would_wait": self._would_wait,
                "would_exhaust": self._would_exhaust,
                "alerts": self._leak_alerts(),
            }

    def _leak_alerts(self) -> list[str]:
        alerts = []
        for key_info in getattr(self._pool, "keys", []):
            for model, count in (key_info.get("model_in_flight") or {}).items():
                if count <= 0:
                    continue
                bound = self.quota_for(model).in_flight_bound
                if count > bound * 1.5:
                    alerts.append(
                        f"{key_info.get('name')}/{model} in_flight={count} > 1.5x bound={bound:.1f}"
                    )
        return alerts

    # ---------- 授予 ----------

    def acquire(self, key_info: dict, model: str, now: float | None = None):
        """尝试授予一次调用许可。返回 Lease 或 SlotRejection，不阻塞。"""
        with self._lock:
            cur = self._clock() if now is None else now
            if not key_info:
                return SlotRejection("no_candidate", model, detail="空 Key")
            if key_info.get("disabled"):
                return SlotRejection("key_disabled", model, key_info.get("name", ""))

            state = self._slot_state(key_info, model, cur)
            quota: ModelQuota = state["quota"]
            bucket: _Bucket = state["bucket"]
            name = key_info.get("name", "")

            cooling = state["cooldown_s"]
            if cooling > 0:
                return SlotRejection(
                    "cooling", model, name, retry_after_ms=int(cooling * 1000) + 1
                )

            exhausted = state["remaining"] <= 0
            need_tokens = state["tokens"] < 1.0
            wait_s = 0.0
            if need_tokens:
                wait_s = (1.0 - state["tokens"]) / max(quota.rate, 1e-9)

            if self.mode == "enforce":
                if exhausted:
                    return SlotRejection(
                        "rpd_exhausted", model, name, retry_after_ms=self._reset_in_ms(cur)
                    )
                if need_tokens:
                    return SlotRejection(
                        "token_wait", model, name, retry_after_ms=int(wait_s * 1000) + 1
                    )
            else:
                # shadow：只统计「本来会拒绝多少次」，不拦请求
                if exhausted:
                    self._would_exhaust += 1
                if need_tokens:
                    self._would_wait += 1

            if self.mode != "off":
                bucket.tokens -= 1.0
            bucket.reserved += 1
            mf = key_info.setdefault("model_in_flight", {})
            mf[model] = mf.get(model, 0) + 1
            key_info["in_flight"] = sum(mf.values())
            return Lease(key_info=key_info, model=model, acquired_at=cur)

    def _reset_in_ms(self, now: float) -> int:
        try:
            return max(0, int((self._pool._get_next_reset_epoch() - now) * 1000))
        except Exception:
            return 0

    # ---------- 结算 ----------

    def settle(
        self,
        lease: Lease,
        outcome: str,
        *,
        usage: dict | None = None,
        cost_ms: float | None = None,
        retry_after_s: float | None = None,
        cooldown_s: float | None = None,
    ) -> None:
        """结算一次调用。

        顺序固定：先 reserved−1，再决定 used+0|1。账户只加一次，不做「加完再减」。
        outcome 取值：ok / rate_limited / client_error / server_error / timeout / cancelled
        """
        with self._lock:
            if lease.settled:
                # 重复结算只做一次：先 reserved−1 再 used+0|1 是不可逆的
                return
            lease.settled = True
            now = self._clock()
            key_info = lease.key_info
            model = lease.model
            name = key_info.get("name", "")
            quota = self.quota_for(model)
            m_quota = self._pool._get_model_quota(key_info, model)
            bucket = self._bucket(name, model, now)
            self._refill(bucket, quota, now)

            bucket.reserved = max(0, bucket.reserved - 1)
            mf = key_info.setdefault("model_in_flight", {})
            mf[model] = max(0, mf.get(model, 0) - 1)
            key_info["in_flight"] = sum(mf.values())

            if outcome in ("ok", "server_error", "timeout"):
                m_quota["used_today"] = int(m_quota.get("used_today", 0)) + 1
            if outcome == "ok":
                m_quota["success_today"] = int(m_quota.get("success_today", 0)) + 1
                m_quota["last_used_at"] = now
                bucket.fail_streak = 0

            if outcome == "rate_limited":
                m_quota["rate_limited_today"] = int(
                    m_quota.get("rate_limited_today", 0)
                ) + 1
                pause = retry_after_s if retry_after_s else 5.0
                self._apply_cooldown(m_quota, now, min(pause, 30.0))
            elif outcome in ("server_error", "timeout"):
                bucket.fail_streak += 1
                pause = cooldown_s if cooldown_s is not None else self.transient_cooldown_s
                if bucket.fail_streak >= self.fail_threshold:
                    pause = self.fail_cooldown_s
                if pause > 0:
                    self._apply_cooldown(m_quota, now, pause)
            elif outcome == "client_error":
                bucket.fail_streak = 0

            if cost_ms is not None:
                self._recent.setdefault((name, model), deque(maxlen=RECENT_COST_SAMPLES)).append(
                    int(cost_ms)
                )
            outcomes = self._recent_outcomes.setdefault((name, model), deque(maxlen=20))
            outcomes.append(outcome)
            counters = self._counters.setdefault((name, model), {})
            counters[outcome] = counters.get(outcome, 0) + 1

            if usage and key_info.get("key"):
                self._pool.record_usage(key_info["key"], usage, model=model)

    @staticmethod
    def _apply_cooldown(m_quota: dict, now: float, seconds: float) -> None:
        m_quota["cooldown_until"] = max(
            float(m_quota.get("cooldown_until", 0.0) or 0.0), now + seconds
        )

    def abort(self, lease: Lease) -> None:
        """网关侧取消：请求没抵达上游，不记账。"""
        self.settle(lease, "cancelled")

    def forget(self, key_name: str, model: str | None = None) -> None:
        """Key 被禁用或解冻后清掉本地桶，避免残留负令牌。"""
        with self._lock:
            for slot in list(self._buckets):
                if slot[0] == key_name and (model is None or slot[1] == model):
                    self._buckets.pop(slot, None)
