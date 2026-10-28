import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 官方 Google AI Studio 模型配额（Free Tier）
# 校准口径：AI Studio Rate Limit 页，2026-10-28 04:26 实测，详见 docs/gemini_rate_limits_status.md
#   limit_rpm: 每分钟请求上限（令牌桶的 rate 来源）
#   limit_rpd: 每日请求上限（配额记账口径）
#   limit_tpd: 每日 Token 上限
#   tpm:       每分钟 Token 限额
#   tier:      模型族 lite | flash | text。既用于候选筛选（只允许同族互换），也用于展示。
#              配额一律按模型自身的 limit_rpm/limit_rpd 记账，不由 tier 推导——
#              2.5-flash-lite 属 lite 族但只有 10 RPM / 20 RPD。
#
# 已剔除（不得再出现在任何路由、候选、记账里）：
#   gemini-2.0-flash / gemini-2.0-flash-lite / gemini-2.5-pro / gemini-1.5-flash / gemini-1.5-pro
#   gemini-3-flash（该 ID 在 API 层 404，页面的 "Gemini 3 Flash" 实际对应 -preview）
CANONICAL_MODELS = {
    # ---------- Lite 梯队：15 RPM / 250K TPM / 500 RPD ----------
    "gemini-3.5-flash-lite": {
        "limit_rpm": 15,
        "limit_rpd": 500,
        "limit_tpd": 10_000_000,
        "tpm": 250_000,
        "tier": "lite",
        "is_core": True,
        "category": "Lite 主力",
    },
    "gemini-3.1-flash-lite": {
        "limit_rpm": 15,
        "limit_rpd": 500,
        "limit_tpd": 10_000_000,
        "tpm": 250_000,
        "tier": "lite",
        "is_core": True,
        "category": "Lite 备用",
    },
    # ---------- 非 Lite 梯队：5 RPM / 250K TPM / 20 RPD ----------
    "gemini-3.8-flash": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 3_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": True,
        "category": "旗舰 Flash",
    },
    # 限流页已无条目，但 2026-10-28 实测可调用（返回 503 high demand，说明请求打到了模型，不是 404）。
    # 按能力排 3.8 之后；拥堵由熔断摘除，不靠降序绕开。
    "gemini-3.7-flash": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 3_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": False,
        "category": "Flash 主力（页面未列）",
    },
    "gemini-3.6-flash": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 3_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": True,
        "category": "主力 Flash",
    },
    # 限流页已无条目，但 2026-10-28 实测 200 正常返回（1.5s）。仍属非 Lite 梯队，参与默认调度。
    "gemini-3.5-flash": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 3_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": False,
        "category": "Flash 备用（页面未列）",
    },
    "gemini-2.5-flash": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 10_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": False,
        "category": "经典 Flash",
    },
    # 名字带 Lite，但实测配额是 10 RPM / 20 RPD，只有 3.5-lite 的 1/25。
    # 必须按非 Lite 记账，否则第 21 次就撞 429。
    "gemini-2.5-flash-lite": {
        "limit_rpm": 10,
        "limit_rpd": 20,
        "limit_tpd": 10_000_000,
        "tpm": 250_000,
        "tier": "lite",
        "is_core": False,
        "category": "经典 Lite（按非 Lite 记账）",
    },
    # ---------- 不常用：在支持数组内，但默认不参与自动降级 ----------
    "gemini-3-flash-preview": {
        "limit_rpm": 5,
        "limit_rpd": 20,
        "limit_tpd": 3_000_000,
        "tpm": 250_000,
        "tier": "flash",
        "is_core": False,
        "category": "预览通道",
    },
    "gemma-4-31b-it": {
        "limit_rpm": 30,
        "limit_rpd": 14400,
        "limit_tpd": 20_000_000,
        "tpm": 16_000,
        "tier": "text",
        "is_core": False,
        "category": "开源文本（TPM 仅 16K）",
    },
    "gemma-4-26b-a4b-it": {
        "limit_rpm": 30,
        "limit_rpd": 14400,
        "limit_tpd": 20_000_000,
        "tpm": 16_000,
        "tier": "text",
        "is_core": False,
        "category": "开源文本（TPM 仅 16K）",
    },
}

# 不常用模型：必须是 CANONICAL_MODELS 的子集。
# 作用：显式指定模型名仍可路由，但自动降级链与默认候选集不包含它们。
UNCOMMON_MODELS = (
    "gemini-3-flash-preview",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
)

# 常用模型 = 支持数组去掉不常用部分，保持定义顺序
PRIMARY_MODELS = tuple(m for m in CANONICAL_MODELS if m not in UNCOMMON_MODELS)

# 已废除的模型：状态文件里可能留着历史消耗记录，但不得再出现在候选、面板与额度接口里
DEPRECATED_MODELS = frozenset(
    {
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-2.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-3-flash",
        "gemini-3.1-pro-preview",
    }
)

DEFAULT_MODEL_RPD = {m: meta["limit_rpd"] for m, meta in CANONICAL_MODELS.items()}
DEFAULT_FALLBACK_RPD = 20
DEFAULT_FALLBACK_TPD = 2_000_000

# Google AI Studio resets free quotas daily at 00:00 Pacific Time (PT)
# PT in daylight saving time is PDT (UTC-7)
PACIFIC_TZ = timezone(timedelta(hours=-7))

PERSISTED_FIELDS = {
    "req_count",
    "success_count",
    "failure_count",
    "rate_limit_count",
    "input_tokens",
    "output_tokens",
    "thought_tokens",
    "total_tokens",
    "cached_tokens",
    "today_input_tokens",
    "today_output_tokens",
    "today_total_tokens",
    "today_req_count",
    "today_success_count",
    "today_failure_count",
    "today_rate_limit_count",
    "today_peak_tpm",
    "cooldown_until",
    "disabled",
    "last_error",
    "last_used_at",
    "model_quotas",
}


class KeyManager:
    def __init__(
        self,
        keys_value: str | None = None,
        clock: Callable[[], float] | None = None,
        state_file: str | Path | None = None,
    ):
        self.keys: list[dict] = []
        self.current_index = 0
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self.last_reset_date: str = ""
        self.quota_history: dict[str, dict] = {}
        self.verification_batches: list[dict] = []

        if state_file is not None:
            self.state_file = Path(state_file).resolve()
        elif keys_value is None:
            configured_state = os.getenv(
                "KEY_POOL_STATE_FILE", "data/key_pool_state.json"
            ).strip()
            self.state_file = (
                Path(configured_state).resolve() if configured_state else None
            )
        else:
            self.state_file = None
        self.default_rate_limit_cooldown = int(
            os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "5")
        )
        self.transient_cooldown = int(os.getenv("TRANSIENT_COOLDOWN_SECONDS", "3"))

        keys_str = os.getenv("GEMINI_KEYS", "") if keys_value is None else keys_value
        if keys_str:
            # 支持两种分隔：换行与逗号；支持两种标识：“account_id|key” 与旧的 “名称|key”
            import re as _re

            from app.core.accounts import load_accounts as _load_accounts

            _accounts = _load_accounts()
            for item in _re.split(r"[\n,]+", keys_str.replace("\\n", "\n")):
                item = item.strip()
                if "|" not in item:
                    continue
                ident, key = item.split("|", 1)
                ident, key = ident.strip(), key.strip()
                if not key:
                    continue
                # 只接受 account_id 作为标识（旧的「名称|key」已废弃）
                if not ident.isdigit():
                    logging.getLogger(__name__).warning(
                        "GEMINI_KEYS 条目标识不是 account_id，已忽略：%r（格式应为 <account_id>|<api_key>）",
                        ident,
                    )
                    continue
                acc_id = int(ident)
                name = _accounts.get(acc_id, {}).get("name") or f"account-{acc_id}"
                self._add_key(name, key, account_id=acc_id)
        elif keys_value is None:
            single_key = os.getenv("GEMINI_API_KEY", "").strip()
            if single_key:
                self._add_key("Default", single_key)
        self._load_state()
        self._check_and_reset_daily()

    def _add_key(self, name: str, key: str, account_id: int = 0):
        self.keys.append(
            {
                "name": name,
                "key": key,
                "account_id": account_id,
                "req_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "rate_limit_count": 0,
                "today_req_count": 0,
                "today_success_count": 0,
                "today_failure_count": 0,
                "today_rate_limit_count": 0,
                "today_peak_tpm": 0,
                "recent_tokens": deque(),
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "today_input_tokens": 0,
                "today_output_tokens": 0,
                "today_total_tokens": 0,
                "in_flight": 0,
                "model_in_flight": {},
                "cooldown_until": 0.0,
                "disabled": False,
                "last_error": "",
                "last_used_at": 0.0,
                "model_quotas": {},
            }
        )

    def _get_pacific_date(self) -> str:
        now = self._clock()
        dt = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(PACIFIC_TZ)
        return dt.strftime("%Y-%m-%d")

    def _get_next_reset_epoch(self) -> float:
        now = self._clock()
        dt = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(PACIFIC_TZ)
        next_midnight = datetime(dt.year, dt.month, dt.day, tzinfo=PACIFIC_TZ) + timedelta(days=1)
        return next_midnight.timestamp()

    def _check_and_reset_daily(self):
        today_pt = self._get_pacific_date()
        if not self.last_reset_date:
            self.last_reset_date = today_pt
            return

        if today_pt > self.last_reset_date:
            logger.info("Pacific date rollover: %s -> %s. Performing daily quota reset.", self.last_reset_date, today_pt)
            yesterday_record = {}
            for k in self.keys:
                m_quotas = k.get("model_quotas", {})
                k_record = {}
                for m_name, m_stats in m_quotas.items():
                    if m_stats.get("used_today", 0) > 0:
                        k_record[m_name] = {
                            "used": m_stats.get("used_today", 0),
                            "success": m_stats.get("success_today", 0),
                            "rate_limited": m_stats.get("rate_limited_today", 0),
                        }
                if k_record:
                    yesterday_record[k["name"]] = k_record

            if yesterday_record:
                self.quota_history[self.last_reset_date] = yesterday_record
                sorted_days = sorted(self.quota_history.keys())
                if len(sorted_days) > 30:
                    for old_day in sorted_days[:-30]:
                        self.quota_history.pop(old_day, None)

            for k in self.keys:
                k["today_input_tokens"] = 0
                k["today_output_tokens"] = 0
                k["today_total_tokens"] = 0
                k["today_req_count"] = 0
                k["today_success_count"] = 0
                k["today_failure_count"] = 0
                k["today_rate_limit_count"] = 0
                k["today_peak_tpm"] = 0
                if "recent_tokens" in k and isinstance(k["recent_tokens"], deque):
                    k["recent_tokens"].clear()
                for m_stats in k.get("model_quotas", {}).values():
                    m_stats["used_today"] = 0
                    m_stats["success_today"] = 0
                    m_stats["failure_today"] = 0
                    m_stats["rate_limited_today"] = 0
                    m_stats["cooldown_until"] = 0.0
                    m_stats["today_tokens"] = 0
                    m_stats["last_used_at"] = 0.0

            self.last_reset_date = today_pt
            self._persist_state()

    def _get_key_tpm(self, key_info: dict, now: float) -> tuple[int, int]:
        dq = key_info.get("recent_tokens")
        if not isinstance(dq, deque):
            dq = deque()
            key_info["recent_tokens"] = dq
        cutoff = now - 60.0
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        current_tpm = sum(item[1] for item in dq)
        peak_tpm = max(key_info.get("today_peak_tpm", 0), current_tpm)
        key_info["today_peak_tpm"] = peak_tpm
        return current_tpm, peak_tpm

    @staticmethod
    def _normalize_model(model: str | None) -> str:
        if not model:
            return ""
        m = model.strip()
        if m.startswith("models/"):
            m = m[7:]
        if ":" in m:
            m = m.split(":", 1)[0]
        return m

    def _get_model_quota(self, key_info: dict, model: str) -> dict:
        m = self._normalize_model(model)
        if "model_quotas" not in key_info or not isinstance(key_info["model_quotas"], dict):
            key_info["model_quotas"] = {}
        if m not in key_info["model_quotas"]:
            canon = CANONICAL_MODELS.get(m, {})
            limit_rpd = canon.get("limit_rpd", DEFAULT_FALLBACK_RPD)
            limit_tpd = canon.get("limit_tpd", DEFAULT_FALLBACK_TPD)
            key_info["model_quotas"][m] = {
                "used_today": 0,
                "success_today": 0,
                "failure_today": 0,
                "rate_limited_today": 0,
                "limit_rpd": limit_rpd,
                "limit_tpd": limit_tpd,
                "today_tokens": 0,
                "cooldown_until": 0.0,
                "last_used_at": 0.0,
            }
        return key_info["model_quotas"][m]

    def get_min_cooldown_remaining(
        self,
        model: str | None = None,
        target_key_name: str | None = None,
    ) -> float:
        """检查是否有 Key 仅处于短时瞬态退避冷却中（<= 5s），返回最短剩余等待秒数；若无或已长时耗尽，返回 0.0"""
        with self._lock:
            now = self._clock()
            normalized = self._normalize_model(model)
            candidates = [self.find_key_by_name(target_key_name)] if target_key_name else self.keys
            min_cd = float("inf")
            found_transient = False
            for k in candidates:
                if not k or k.get("disabled"):
                    continue
                if k.get("cooldown_until", 0.0) > now + 30.0:
                    continue
                if normalized:
                    m_quota = self._get_model_quota(k, normalized)
                    if m_quota.get("used_today", 0) >= m_quota.get("limit_rpd", DEFAULT_FALLBACK_RPD):
                        continue
                    cd = m_quota.get("cooldown_until", 0.0) - now
                    if 0 < cd <= 5.0:
                        min_cd = min(min_cd, cd)
                        found_transient = True
                else:
                    cd = k.get("cooldown_until", 0.0) - now
                    if 0 < cd <= 5.0:
                        min_cd = min(min_cd, cd)
                        found_transient = True
            return min_cd if found_transient else 0.0

    def can_consume(
        self,
        key_info: dict | None,
        model: str,
        check_concurrency: bool = True,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """原子性判定某 Key 对特定模型当前是否可消费并返回状态归因"""
        if not key_info:
            return False, "KeyNotFound"
        if key_info.get("disabled", False):
            return False, "KeyDisabled"
        cur_time = now if now is not None else self._clock()
        if key_info.get("cooldown_until", 0.0) > cur_time + 30.0:
            return False, "KeyCooling"
        m = self._normalize_model(model)
        if m:
            m_quota = self._get_model_quota(key_info, m)
            limit_rpd = m_quota.get("limit_rpd", DEFAULT_FALLBACK_RPD)
            used_rpd = m_quota.get("used_today", 0)
            if used_rpd >= limit_rpd:
                return False, "RpdExhausted"
            if m_quota.get("cooldown_until", 0.0) > cur_time:
                return False, "ModelCooling"
        # 并发数不再是准入条件：P1 起由 TokenBucket 的令牌与 RPD 共同决定吞吐。
        # check_concurrency 参数保留只为不破坏调用方签名。
        return True, "Available"
 
    def can_consume_bool(
        self,
        key_info: dict | None,
        model: str,
        check_concurrency: bool = True,
        now: float | None = None,
    ) -> bool:
        """原子性判定某 Key 对特定模型当前是否可消费（仅返回布尔值）"""
        ok, _ = self.can_consume(
            key_info, model, check_concurrency=check_concurrency, now=now
        )
        return ok

    def get_ranked_models(
        self,
        target_model: str,
        key_info: dict | None = None,
        custom_fallbacks: list[str] | None = None,
    ) -> list[dict]:
        """
        计算模型候选队列，实现 RPD > 0.6 后置与动态余量加权排序:
        Tier A (优先池): usage_ratio <= 0.6 且剩余配额 > 0
        Tier B (后置池): usage_ratio > 0.6 且剩余配额 > 0
        Score = remaining_rpd * (1 - usage_ratio) * similarity
        """
        with self._lock:
            now = self._clock()
            target_norm = self._normalize_model(target_model)

            if custom_fallbacks:
                raw_candidates = [self._normalize_model(m) for m in custom_fallbacks if m]
            else:
                # 默认候选只取同族的常用模型：lite 只换 lite，flash 只换 flash，text 只换 text。
                # 跨族一律不换——那要么烧 flash 的 20 RPD，要么把音频任务丢给 TPM 16K 的 gemma。
                family = CANONICAL_MODELS.get(target_norm, {}).get("tier")
                if family:
                    raw_candidates = [
                        m
                        for m in PRIMARY_MODELS
                        if CANONICAL_MODELS.get(m, {}).get("tier") == family
                    ]
                else:
                    raw_candidates = list(PRIMARY_MODELS)
            if target_norm and target_norm not in raw_candidates:
                raw_candidates.insert(0, target_norm)

            candidates = []
            seen = set()
            for m in raw_candidates:
                if m and m not in seen:
                    seen.add(m)
                    candidates.append(m)

            tier_a = []
            tier_b = []

            for m in candidates:
                if key_info:
                    q = self._get_model_quota(key_info, m)
                    used_rpd = q.get("used_today", 0)
                    limit_rpd = q.get("limit_rpd", DEFAULT_FALLBACK_RPD)
                    is_cooling = q.get("cooldown_until", 0.0) > now
                else:
                    active_keys = [k for k in self.keys if not k.get("disabled")]
                    if active_keys:
                        used_rpd = sum(self._get_model_quota(k, m).get("used_today", 0) for k in active_keys) // len(active_keys)
                        limit_rpd = CANONICAL_MODELS.get(m, {}).get("limit_rpd", DEFAULT_FALLBACK_RPD)
                        is_cooling = all(self._get_model_quota(k, m).get("cooldown_until", 0.0) > now for k in active_keys)
                    else:
                        used_rpd = 0
                        limit_rpd = DEFAULT_FALLBACK_RPD
                        is_cooling = False

                remaining_rpd = max(0, limit_rpd - used_rpd)
                if remaining_rpd <= 0:
                    continue

                usage_ratio = round(used_rpd / limit_rpd, 3) if limit_rpd > 0 else 1.0

                if m == target_norm:
                    similarity = 1.0
                elif "lite" in m and "lite" in target_norm:
                    similarity = 0.95
                elif "flash" in m and "flash" in target_norm:
                    similarity = 0.90
                elif "lite" in m:
                    similarity = 0.85
                elif "flash" in m:
                    similarity = 0.80
                else:
                    similarity = 0.70

                item = {
                    "model": m,
                    "usage_ratio": usage_ratio,
                    "remaining_rpd": remaining_rpd,
                    "limit_rpd": limit_rpd,
                    "used_rpd": used_rpd,
                    "similarity": similarity,
                    "is_cooling": is_cooling,
                }

                if usage_ratio <= 0.6:
                    item["tier"] = "A"
                    item["score"] = round(remaining_rpd * similarity, 2)
                    tier_a.append(item)
                else:
                    item["tier"] = "B"
                    score = remaining_rpd * max(0.01, (1.0 - usage_ratio)) * similarity
                    item["score"] = round(score, 3)
                    tier_b.append(item)

            tier_a.sort(key=lambda x: (x["is_cooling"], -x["similarity"], x["usage_ratio"], -x["remaining_rpd"]))
            tier_b.sort(key=lambda x: (x["is_cooling"], -x["score"], -x["remaining_rpd"]))

            return tier_a + tier_b

    def acquire_key(
        self,
        model: str | None = None,
        target_key_name: str | None = None,
        allow_transient_cooling: bool = False,
        check_concurrency: bool = True,
    ) -> dict | None:
        with self._lock:
            if not self.keys:
                return None

            self._check_and_reset_daily()
            now = self._clock()
            normalized_model = self._normalize_model(model)

            # 若明确指定目标 Key，优先且仅尝试该 Key
            if target_key_name:
                key_info = self.find_key_by_name(target_key_name)
                if key_info:
                    if not key_info["disabled"] and key_info["cooldown_until"] <= now:
                        if normalized_model:
                            m_quota = self._get_model_quota(key_info, normalized_model)
                            if m_quota["used_today"] >= m_quota["limit_rpd"]:
                                return None
                            if allow_transient_cooling:
                                if m_quota.get("cooldown_until", 0.0) > now + 15.0:
                                    return None
                            elif m_quota.get("cooldown_until", 0.0) > now:
                                return None
                        key_info["req_count"] += 1
                        key_info["today_req_count"] = key_info.get("today_req_count", 0) + 1
                        key_info["last_used_at"] = now
                        if normalized_model:
                            m_quota = self._get_model_quota(key_info, normalized_model)
                            m_quota["used_today"] += 1
                            key_info.setdefault("model_in_flight", {})[normalized_model] = (
                                key_info.get("model_in_flight", {}).get(normalized_model, 0) + 1
                            )
                            key_info["in_flight"] = sum(key_info.setdefault("model_in_flight", {}).values())
                        else:
                            key_info["in_flight"] = key_info.get("in_flight", 0) + 1
                        self._persist_state()
                        return {"name": key_info["name"], "key": key_info["key"]}
                return None

            for _ in range(len(self.keys)):
                idx = self.current_index
                self.current_index = (self.current_index + 1) % len(self.keys)
                key_info = self.keys[idx]

                if key_info["disabled"] or key_info["cooldown_until"] > now:
                    continue

                if normalized_model:
                    m_quota = self._get_model_quota(key_info, normalized_model)
                    if m_quota["used_today"] >= m_quota["limit_rpd"]:
                        continue
                    if allow_transient_cooling:
                        if m_quota.get("cooldown_until", 0.0) > now + 15.0:
                            continue
                    elif m_quota.get("cooldown_until", 0.0) > now:
                        continue

                key_info["req_count"] += 1
                key_info["today_req_count"] = key_info.get("today_req_count", 0) + 1
                key_info["last_used_at"] = now
                if normalized_model:
                    m_quota = self._get_model_quota(key_info, normalized_model)
                    m_quota["used_today"] += 1
                    key_info.setdefault("model_in_flight", {})[normalized_model] = (
                        key_info.get("model_in_flight", {}).get(normalized_model, 0) + 1
                    )
                    key_info["in_flight"] = sum(key_info.setdefault("model_in_flight", {}).values())
                else:
                    key_info["in_flight"] = key_info.get("in_flight", 0) + 1
                self._persist_state()
                return {"name": key_info["name"], "key": key_info["key"]}

            return None

    def record_usage(
        self,
        key_str: str,
        usage: dict | None = None,
        model: str | None = None,
        success: bool = True,
    ) -> None:
        """只更新 Token 曲线与全局计数。

        P1 起请求路径的 in_flight 与模型级账本由 SlotRegistry 负责，
        这里必须保持「不碰 in_flight、不碰 success_today」，否则会与结算重复计数。
        """
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            if success:
                key_info["success_count"] += 1
                key_info["today_success_count"] = key_info.get("today_success_count", 0) + 1
                key_info["last_error"] = ""

            tokens_delta = 0
            if usage:
                input_tokens = int(usage.get("promptTokenCount") or 0)
                output_tokens = int(usage.get("candidatesTokenCount") or 0)
                total_tokens = int(
                    usage.get("totalTokenCount") or input_tokens + output_tokens
                )
                tokens_delta = total_tokens
                key_info["input_tokens"] += input_tokens
                key_info["output_tokens"] += output_tokens
                key_info["thought_tokens"] += int(usage.get("thoughtsTokenCount") or 0)
                key_info["total_tokens"] += total_tokens
                key_info["cached_tokens"] += int(
                    usage.get("cachedContentTokenCount") or 0
                )
                key_info["today_input_tokens"] = (
                    key_info.get("today_input_tokens", 0) + input_tokens
                )
                key_info["today_output_tokens"] = (
                    key_info.get("today_output_tokens", 0) + output_tokens
                )
                key_info["today_total_tokens"] = (
                    key_info.get("today_total_tokens", 0) + total_tokens
                )

            now = self._clock()
            if tokens_delta > 0:
                dq = key_info.get("recent_tokens")
                if not isinstance(dq, deque):
                    dq = deque()
                    key_info["recent_tokens"] = dq
                dq.append((now, tokens_delta))
                self._get_key_tpm(key_info, now)
            if model and tokens_delta:
                m_quota = self._get_model_quota(key_info, model)
                m_quota["today_tokens"] = m_quota.get("today_tokens", 0) + tokens_delta
            self._persist_state()

    def mark_success(
        self,
        key_str: str,
        usage: dict | None = None,
        model: str | None = None,
        count_in_flight: bool = True,
    ):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            if count_in_flight:
                if model:
                    norm_m = self._normalize_model(model)
                    key_info.setdefault("model_in_flight", {})[norm_m] = max(
                        0, key_info.get("model_in_flight", {}).get(norm_m, 0) - 1
                    )
                    key_info["in_flight"] = sum(
                        key_info.setdefault("model_in_flight", {}).values()
                    )
                else:
                    key_info["in_flight"] = max(0, key_info.get("in_flight", 0) - 1)
            key_info["success_count"] += 1
            key_info["today_success_count"] = key_info.get("today_success_count", 0) + 1
            key_info["last_error"] = ""

            tokens_delta = 0
            if usage:
                input_tokens = int(usage.get("promptTokenCount") or 0)
                output_tokens = int(usage.get("candidatesTokenCount") or 0)
                total_tokens = int(
                    usage.get("totalTokenCount")
                    or input_tokens + output_tokens
                )
                tokens_delta = total_tokens
                key_info["input_tokens"] += input_tokens
                key_info["output_tokens"] += output_tokens
                key_info["thought_tokens"] += int(
                    usage.get("thoughtsTokenCount") or 0
                )
                key_info["total_tokens"] += total_tokens
                key_info["cached_tokens"] += int(
                    usage.get("cachedContentTokenCount") or 0
                )
                key_info["today_input_tokens"] = key_info.get("today_input_tokens", 0) + input_tokens
                key_info["today_output_tokens"] = key_info.get("today_output_tokens", 0) + output_tokens
                key_info["today_total_tokens"] = key_info.get("today_total_tokens", 0) + total_tokens

            now = self._clock()
            if tokens_delta > 0:
                dq = key_info.get("recent_tokens")
                if not isinstance(dq, deque):
                    dq = deque()
                    key_info["recent_tokens"] = dq
                dq.append((now, tokens_delta))
                self._get_key_tpm(key_info, now)

            if model:
                m_quota = self._get_model_quota(key_info, model)
                m_quota["success_today"] += 1
                m_quota["today_tokens"] = m_quota.get("today_tokens", 0) + tokens_delta
                m_quota["last_used_at"] = now
            self._persist_state()

    def mark_failure(
        self,
        key_str: str,
        *,
        reason: str,
        cooldown_seconds: int = 0,
        disable: bool = False,
        rate_limited: bool = False,
        model: str | None = None,
        count_in_flight: bool = True,
    ):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            if count_in_flight:
                if model:
                    norm_m = self._normalize_model(model)
                    key_info.setdefault("model_in_flight", {})[norm_m] = max(
                        0, key_info.get("model_in_flight", {}).get(norm_m, 0) - 1
                    )
                    key_info["in_flight"] = sum(
                        key_info.setdefault("model_in_flight", {}).values()
                    )
                else:
                    key_info["in_flight"] = max(0, key_info.get("in_flight", 0) - 1)
            key_info["failure_count"] += 1
            key_info["today_failure_count"] = key_info.get("today_failure_count", 0) + 1
            key_info["last_error"] = reason[:240]
            key_info["disabled"] = disable
            now = self._clock()

            if model:
                m_quota = self._get_model_quota(key_info, model)
                m_quota["failure_today"] += 1
                if rate_limited:
                    m_quota["rate_limited_today"] += 1
                    reason_lower = reason.lower()
                    has_daily_kw = (
                        "queries per day" in reason_lower
                        or "per day" in reason_lower
                        or ("quota" in reason_lower and "day" in reason_lower)
                    )
                    is_daily_exhausted = has_daily_kw or (m_quota["used_today"] >= m_quota["limit_rpd"])
                    if is_daily_exhausted:
                        cd = max(cooldown_seconds, int(self._get_next_reset_epoch() - now))
                        logger.warning(
                            "Model %s on %s daily quota exhausted. Cooldown %ds until reset",
                            model, key_info["name"], cd
                        )
                    else:
                        # 突发分钟限流或拥塞: 仅冷却 3~5 秒 (或上游 Retry-After，最高不超过 30s)
                        cd = max(3, min(cooldown_seconds, 30)) if cooldown_seconds > 0 else 5
                        logger.info(
                            "Model %s on %s temporary RPM spike. Brief cooldown: %ds",
                            model, key_info["name"], cd
                        )
                    m_quota["cooldown_until"] = max(m_quota["cooldown_until"], now + cd)
                elif cooldown_seconds > 0:
                    # 瞬态 503 / 500 / 网络异常: 仅短期退避该特定模型
                    m_quota["cooldown_until"] = max(
                        m_quota["cooldown_until"], now + cooldown_seconds
                    )
            elif cooldown_seconds > 0:
                # 仅在非特定模型级错误时才冷却整个物理 Key (例如 403)
                key_info["cooldown_until"] = max(
                    key_info["cooldown_until"], now + cooldown_seconds
                )

            if rate_limited:
                key_info["rate_limit_count"] += 1
                key_info["today_rate_limit_count"] = key_info.get("today_rate_limit_count", 0) + 1
            self._persist_state()

    def release(self, key_str: str, model: str | None = None):
        with self._lock:
            key_info = self._find_key(key_str)
            if key_info:
                if model:
                    norm_m = self._normalize_model(model)
                    key_info.setdefault("model_in_flight", {})[norm_m] = max(
                        0, key_info.get("model_in_flight", {}).get(norm_m, 0) - 1
                    )
                    key_info["in_flight"] = sum(key_info.setdefault("model_in_flight", {}).values())
                else:
                    key_info["in_flight"] = max(0, key_info.get("in_flight", 0) - 1)
                self._persist_state()

    def _find_key(self, key_str: str) -> dict | None:
        return next((item for item in self.keys if item["key"] == key_str), None)

    def find_key_by_name(self, name: str) -> dict | None:
        with self._lock:
            return next((item for item in self.keys if item["name"] == name), None)

    def unfreeze_key(self, name: str) -> bool:
        with self._lock:
            key_info = self.find_key_by_name(name)
            if not key_info:
                return False
            key_info["cooldown_until"] = 0.0
            key_info["last_error"] = ""
            for m_stats in key_info.get("model_quotas", {}).values():
                m_stats["cooldown_until"] = 0.0
            self._persist_state()
            return True

    def toggle_key(self, name: str) -> bool | None:
        with self._lock:
            key_info = self.find_key_by_name(name)
            if not key_info:
                return None
            key_info["disabled"] = not key_info.get("disabled", False)
            self._persist_state()
            return key_info["disabled"]

    def get_status(self) -> dict:
        with self._lock:
            self._check_and_reset_daily()
            now = self._clock()
            pool = []
            for key_info in self.keys:
                cooldown = max(0, int(key_info["cooldown_until"] - now))
                if key_info["disabled"]:
                    status = "Disabled"
                elif cooldown > 0:
                    status = "Cooling"
                else:
                    status = "Active"

                # 提取该 Key 各模型的实时配额与状态，优先采用 MRU 动态置顶（最新使用的模型排在最前）
                m_map = {}
                base_models = list(CANONICAL_MODELS.keys())
                # 仅将今日有实际调用的额外冷门模型追加至扩展区，避免历史遗留脏模型破坏等高性
                extra_models = [
                    m
                    for m in key_info.get("model_quotas", {}).keys()
                    if m not in CANONICAL_MODELS
                    and m not in DEPRECATED_MODELS
                    and key_info["model_quotas"][m].get("used_today", 0) > 0
                ]
                all_models = base_models + extra_models

                def _mru_sort_key(m_name: str):
                    q_data = key_info.get("model_quotas", {}).get(m_name, {})
                    last_used = float(q_data.get("last_used_at", 0.0) or 0.0)
                    base_idx = base_models.index(m_name) if m_name in base_models else 999
                    return (0 if last_used > 0 else 1, -last_used, base_idx)

                all_models.sort(key=_mru_sort_key)

                used_today_total = 0
                limit_rpd_total = 0
                for m in all_models:
                    q = self._get_model_quota(key_info, m)
                    limit_rpd = q.get("limit_rpd", DEFAULT_FALLBACK_RPD)
                    limit_tpd = q.get("limit_tpd", DEFAULT_FALLBACK_TPD)
                    used_rpd = q.get("used_today", 0)
                    used_tpd = q.get("today_tokens", 0)
                    remaining_rpd = max(0, limit_rpd - used_rpd)
                    is_cooling = q.get("cooldown_until", 0.0) > now
                    m_status = (
                        "Disabled"
                        if key_info["disabled"]
                        else (
                            "RateLimited"
                            if is_cooling
                            else ("Exhausted" if remaining_rpd == 0 else "Active")
                        )
                    )
                    canon_meta = CANONICAL_MODELS.get(m, {})
                    is_core = canon_meta.get("is_core", False)
                    category = canon_meta.get("category", "扩展模型")
                    m_in_flight = key_info.get("model_in_flight", {}).get(m, 0)

                    m_map[m] = {
                        "limit_rpd": limit_rpd,
                        "used_today": used_rpd,
                        "remaining": remaining_rpd,
                        "limit_tpd": limit_tpd,
                        "used_tokens_today": used_tpd,
                        "status": m_status,
                        "is_core": is_core,
                        "category": category,
                        "cooldown_remaining_sec": max(0, int(q.get("cooldown_until", 0.0) - now)),
                        "in_flight": m_in_flight,
                        "last_used_at": q.get("last_used_at", 0.0),
                    }
                    used_today_total += used_rpd
                    limit_rpd_total += limit_rpd

                current_tpm, peak_tpm = self._get_key_tpm(key_info, now)
                active_models = [
                    {"model": m, "in_flight": count}
                    for m, count in key_info.get("model_in_flight", {}).items()
                    if count > 0
                ]

                pool.append(
                    {
                        "name": key_info["name"],
                        "status": status,
                        "disabled": key_info["disabled"],
                        "req_count": key_info["req_count"],
                        "success_count": key_info["success_count"],
                        "failure_count": key_info["failure_count"],
                        "rate_limit_count": key_info["rate_limit_count"],
                        "today_req_count": key_info.get("today_req_count", 0),
                        "today_success_count": key_info.get("today_success_count", 0),
                        "today_failure_count": key_info.get("today_failure_count", 0),
                        "today_rate_limit_count": key_info.get("today_rate_limit_count", 0),
                        "current_tpm": current_tpm,
                        "today_peak_tpm": peak_tpm,
                        "input_tokens": key_info["input_tokens"],
                        "output_tokens": key_info["output_tokens"],
                        "thought_tokens": key_info["thought_tokens"],
                        "total_tokens": key_info["total_tokens"],
                        "cached_tokens": key_info["cached_tokens"],
                        "today_input_tokens": key_info.get("today_input_tokens", 0),
                        "today_output_tokens": key_info.get("today_output_tokens", 0),
                        "today_total_tokens": key_info.get("today_total_tokens", 0),
                        "in_flight": key_info["in_flight"],
                        "model_in_flight": key_info.get("model_in_flight", {}),
                        "active_models": active_models,
                        "cooldown_remaining_sec": cooldown,
                        "last_error": key_info["last_error"],
                        "last_used_at": key_info["last_used_at"],
                        "key_prefix": self._mask_key(key_info["key"]),
                        "models": m_map,
                        "used_today_total": used_today_total,
                        "limit_rpd_total": limit_rpd_total,
                        "tpm_limit": 1000000,
                        "last_check_time": now,
                    }
                )

            next_reset_epoch = self._get_next_reset_epoch()
            total_requests = sum(item["req_count"] for item in self.keys)
            total_successes = sum(item["success_count"] for item in self.keys)
            total_tokens = sum(item["total_tokens"] for item in self.keys)
            return {
                "total_keys": len(self.keys),
                "available_keys": sum(item["status"] == "Active" for item in pool),
                "cooling_keys": sum(item["status"] == "Cooling" for item in pool),
                "disabled_keys": sum(item["status"] == "Disabled" for item in pool),
                "in_flight": sum(item["in_flight"] for item in self.keys),
                "total_requests": total_requests,
                "total_successes": total_successes,
                "input_tokens": sum(item["input_tokens"] for item in self.keys),
                "output_tokens": sum(item["output_tokens"] for item in self.keys),
                "thought_tokens": sum(item["thought_tokens"] for item in self.keys),
                "total_tokens": total_tokens,
                "cached_tokens": sum(item["cached_tokens"] for item in self.keys),
                "quota_reset_in_hours": round(max(0.0, (next_reset_epoch - now) / 3600), 1),
                "next_reset_epoch": next_reset_epoch,
                "success_rate": (
                    round(total_successes / total_requests * 100, 1)
                    if total_requests
                    else 0.0
                ),
                "pool": pool,
            }

    def get_quota_status(self) -> dict:
        with self._lock:
            self._check_and_reset_daily()
            now = self._clock()
            today_pt = self._get_pacific_date()
            next_reset_epoch = self._get_next_reset_epoch()
            seconds_to_reset = max(0, int(next_reset_epoch - now))

            keys_summary = []
            for k in self.keys:
                m_map = {}
                all_models = sorted(
                    {
                        m
                        for m in list(DEFAULT_MODEL_RPD.keys())
                        + list(k.get("model_quotas", {}).keys())
                        if m not in DEPRECATED_MODELS
                    }
                )
                for m in all_models:
                    q = self._get_model_quota(k, m)
                    limit = q["limit_rpd"]
                    used = q["used_today"]
                    remaining = max(0, limit - used)
                    is_cooling = q["cooldown_until"] > now
                    status = (
                        "Disabled"
                        if k["disabled"]
                        else (
                            "RateLimited"
                            if is_cooling
                            else ("Exhausted" if remaining == 0 else "Active")
                        )
                    )
                    m_map[m] = {
                        "limit_rpd": limit,
                        "used_today": used,
                        "remaining": remaining,
                        "status": status,
                        "cooldown_remaining_sec": max(0, int(q["cooldown_until"] - now)),
                    }
                keys_summary.append({
                    "name": k["name"],
                    "key_prefix": self._mask_key(k["key"]),
                    "disabled": k["disabled"],
                    "models": m_map,
                })

            model_totals = {}
            for m in sorted(DEFAULT_MODEL_RPD.keys()):
                total_limit = 0
                total_used = 0
                total_remaining = 0
                active_keys = 0
                for k in keys_summary:
                    m_stat = k["models"].get(m, {})
                    total_limit += m_stat.get("limit_rpd", 0)
                    total_used += m_stat.get("used_today", 0)
                    total_remaining += m_stat.get("remaining", 0)
                    if m_stat.get("status") == "Active":
                        active_keys += 1
                model_totals[m] = {
                    "total_limit": total_limit,
                    "total_used": total_used,
                    "total_remaining": total_remaining,
                    "active_keys": active_keys,
                }

            return {
                "current_pacific_date": today_pt,
                "seconds_to_next_reset": seconds_to_reset,
                "next_reset_in_hours": round(seconds_to_reset / 3600, 2),
                "summary_by_model": model_totals,
                "keys": keys_summary,
                "history": self.quota_history,
            }

    def _load_state(self):
        if not self.state_file or not self.state_file.is_file():
            return
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.last_reset_date = payload.get("last_reset_date", "")
            self.quota_history = payload.get("quota_history", {})
            self.verification_batches = payload.get("verification_batches", [])
            persisted_keys = payload.get("keys", {})
            if not isinstance(persisted_keys, dict):
                return
            for key_info in self.keys:
                persisted = persisted_keys.get(self._fingerprint(key_info["key"]), {})
                if not isinstance(persisted, dict):
                    continue
                for field in PERSISTED_FIELDS:
                    if field in persisted:
                        key_info[field] = persisted[field]
                key_info["in_flight"] = 0
                if "recent_tokens" not in key_info or not isinstance(key_info["recent_tokens"], deque):
                    key_info["recent_tokens"] = deque()
                now = self._clock()
                for m_name, m_quota in key_info.get("model_quotas", {}).items():
                    # 旧状态只有 success_today（成功数），used_today（发出数）为 0。
                    # 迁移时取两者较大值：宁可高估消耗，不可超卖。
                    if int(m_quota.get("used_today", 0)) < int(m_quota.get("success_today", 0)):
                        m_quota["used_today"] = int(m_quota.get("success_today", 0))
                    if m_quota.get("cooldown_until", 0.0) > now + 300:
                        remaining_cd = int(m_quota.get("cooldown_until", 0.0) - now)
                        logger.warning(
                            "Persistent cooldown detected: %s on %s has %ds remaining (used %d/%d). Preserved; use dashboard to unfreeze manually.",
                            m_name, key_info["name"], remaining_cd, m_quota.get("used_today", 0), m_quota.get("limit_rpd", 20)
                        )
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Unable to load Key pool state: %s", exc)

    def _persist_state(self):
        if not self.state_file:
            return
        payload = {
            "version": 1,
            "updated_at": self._clock(),
            "last_reset_date": self.last_reset_date,
            "quota_history": self.quota_history,
            "verification_batches": self.verification_batches[:50],
            "keys": {
                self._fingerprint(item["key"]): {
                    "name": item["name"],
                    **{field: item[field] for field in PERSISTED_FIELDS if field in item},
                }
                for item in self.keys
            },
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.state_file)
        except OSError as exc:
            logger.warning("Unable to persist Key pool state: %s", exc)

    def add_verification_batch(self, batch: dict):
        with self._lock:
            self.verification_batches.insert(0, batch)
            if len(self.verification_batches) > 50:
                self.verification_batches = self.verification_batches[:50]
            self._persist_state()

    def get_verification_batches(self) -> list[dict]:
        with self._lock:
            return list(self.verification_batches)

    def delete_verification_batches(self, batch_ids: list[str]):
        with self._lock:
            ids_set = set(batch_ids)
            self.verification_batches = [
                b for b in self.verification_batches if b.get("batch_id") not in ids_set
            ]
            self._persist_state()

    @staticmethod
    def _fingerprint(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 12:
            return "***"
        return f"{key[:6]}...{key[-4:]}"
