import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default Google Gemini Free Tier RPD (Requests Per Day) per project/key
DEFAULT_MODEL_RPD = {
    "gemini-2.5-flash": 20,
    "gemini-3-flash-preview": 20,
    "gemini-3-flash": 20,
    "gemini-3.5-flash": 20,
    "gemini-3.6-flash": 20,
    "gemini-3.7-flash": 20,
    "gemini-3.8-flash": 20,
    "gemini-2.5-flash-lite": 20,
    "gemini-3.1-flash-lite": 500,
    "gemini-3.5-flash-lite": 500,
    "gemini-flash-latest": 20,
    "gemini-flash-lite-latest": 500,
}
DEFAULT_FALLBACK_RPD = 20

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
            os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "60")
        )
        self.transient_cooldown = int(os.getenv("TRANSIENT_COOLDOWN_SECONDS", "10"))

        keys_str = os.getenv("GEMINI_KEYS", "") if keys_value is None else keys_value
        if keys_str:
            for item in keys_str.split(","):
                if "|" not in item:
                    continue
                name, key = item.split("|", 1)
                if key.strip():
                    self._add_key(name.strip() or "Unnamed", key.strip())
        elif keys_value is None:
            single_key = os.getenv("GEMINI_API_KEY", "").strip()
            if single_key:
                self._add_key("Default", single_key)
        self._load_state()
        self._check_and_reset_daily()

    def _add_key(self, name: str, key: str):
        self.keys.append(
            {
                "name": name,
                "key": key,
                "req_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "rate_limit_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "in_flight": 0,
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
                for m_stats in k.get("model_quotas", {}).values():
                    m_stats["used_today"] = 0
                    m_stats["success_today"] = 0
                    m_stats["failure_today"] = 0
                    m_stats["rate_limited_today"] = 0
                    m_stats["cooldown_until"] = 0.0

            self.last_reset_date = today_pt
            self._persist_state()

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
            limit = DEFAULT_MODEL_RPD.get(m, DEFAULT_FALLBACK_RPD)
            key_info["model_quotas"][m] = {
                "used_today": 0,
                "success_today": 0,
                "failure_today": 0,
                "rate_limited_today": 0,
                "limit_rpd": limit,
                "cooldown_until": 0.0,
            }
        return key_info["model_quotas"][m]

    def acquire_key(self, model: str | None = None) -> dict | None:
        with self._lock:
            if not self.keys:
                return None

            self._check_and_reset_daily()
            now = self._clock()
            normalized_model = self._normalize_model(model)

            for _ in range(len(self.keys)):
                idx = self.current_index
                self.current_index = (self.current_index + 1) % len(self.keys)
                key_info = self.keys[idx]

                if key_info["disabled"] or key_info["cooldown_until"] > now:
                    continue

                if normalized_model:
                    m_quota = self._get_model_quota(key_info, normalized_model)
                    if m_quota["success_today"] >= m_quota["limit_rpd"] or m_quota.get("cooldown_until", 0.0) > now:
                        continue

                key_info["req_count"] += 1
                key_info["in_flight"] += 1
                key_info["last_used_at"] = now
                if normalized_model:
                    m_quota = self._get_model_quota(key_info, normalized_model)
                    m_quota["used_today"] += 1
                self._persist_state()
                return {"name": key_info["name"], "key": key_info["key"]}

            return None

    def mark_success(
        self,
        key_str: str,
        usage: dict | None = None,
        model: str | None = None,
    ):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            key_info["in_flight"] = max(0, key_info["in_flight"] - 1)
            key_info["success_count"] += 1
            key_info["last_error"] = ""
            if model:
                m_quota = self._get_model_quota(key_info, model)
                m_quota["success_today"] += 1

            if usage:
                input_tokens = int(usage.get("promptTokenCount") or 0)
                output_tokens = int(usage.get("candidatesTokenCount") or 0)
                total_tokens = int(
                    usage.get("totalTokenCount")
                    or input_tokens + output_tokens
                )
                key_info["input_tokens"] += input_tokens
                key_info["output_tokens"] += output_tokens
                key_info["thought_tokens"] += int(
                    usage.get("thoughtsTokenCount") or 0
                )
                key_info["total_tokens"] += total_tokens
                key_info["cached_tokens"] += int(
                    usage.get("cachedContentTokenCount") or 0
                )
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
    ):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            key_info["in_flight"] = max(0, key_info["in_flight"] - 1)
            key_info["failure_count"] += 1
            key_info["last_error"] = reason[:240]
            key_info["disabled"] = disable
            now = self._clock()

            if model:
                m_quota = self._get_model_quota(key_info, model)
                m_quota["failure_today"] += 1
                if rate_limited:
                    m_quota["rate_limited_today"] += 1
                    is_daily_exhausted = any(
                        sub in reason.lower()
                        for sub in ("resource_exhausted", "quota", "limit: 20", "limit: 500", "per day")
                    )
                    cd = (
                        max(cooldown_seconds, int(self._get_next_reset_epoch() - now))
                        if is_daily_exhausted
                        else max(cooldown_seconds, 60)
                    )
                    m_quota["cooldown_until"] = max(m_quota["cooldown_until"], now + cd)

            if rate_limited:
                key_info["rate_limit_count"] += 1
            if cooldown_seconds > 0:
                key_info["cooldown_until"] = max(
                    key_info["cooldown_until"], now + cooldown_seconds
                )
            self._persist_state()

    def release(self, key_str: str):
        with self._lock:
            key_info = self._find_key(key_str)
            if key_info:
                key_info["in_flight"] = max(0, key_info["in_flight"] - 1)
                self._persist_state()

    def _find_key(self, key_str: str) -> dict | None:
        return next((item for item in self.keys if item["key"] == key_str), None)

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
                pool.append(
                    {
                        "name": key_info["name"],
                        "status": status,
                        "req_count": key_info["req_count"],
                        "success_count": key_info["success_count"],
                        "failure_count": key_info["failure_count"],
                        "rate_limit_count": key_info["rate_limit_count"],
                        "input_tokens": key_info["input_tokens"],
                        "output_tokens": key_info["output_tokens"],
                        "thought_tokens": key_info["thought_tokens"],
                        "total_tokens": key_info["total_tokens"],
                        "cached_tokens": key_info["cached_tokens"],
                        "in_flight": key_info["in_flight"],
                        "cooldown_remaining_sec": cooldown,
                        "last_error": key_info["last_error"],
                        "last_used_at": key_info["last_used_at"],
                        "key_prefix": self._mask_key(key_info["key"]),
                    }
                )

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
                    set(list(DEFAULT_MODEL_RPD.keys()) + list(k.get("model_quotas", {}).keys()))
                )
                for m in all_models:
                    q = self._get_model_quota(k, m)
                    limit = q["limit_rpd"]
                    used = q["success_today"]
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

    @staticmethod
    def _fingerprint(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 12:
            return "***"
        return f"{key[:6]}...{key[-4:]}"
