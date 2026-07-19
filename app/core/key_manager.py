import os
import threading
import time
from collections.abc import Callable


class KeyManager:
    def __init__(
        self,
        keys_value: str | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.keys: list[dict] = []
        self.current_index = 0
        self._clock = clock or time.time
        self._lock = threading.RLock()
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

    def _add_key(self, name: str, key: str):
        self.keys.append(
            {
                "name": name,
                "key": key,
                "req_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "rate_limit_count": 0,
                "in_flight": 0,
                "cooldown_until": 0.0,
                "disabled": False,
                "last_error": "",
                "last_used_at": 0.0,
            }
        )

    def acquire_key(self) -> dict | None:
        with self._lock:
            if not self.keys:
                return None

            now = self._clock()
            for _ in range(len(self.keys)):
                idx = self.current_index
                self.current_index = (self.current_index + 1) % len(self.keys)
                key_info = self.keys[idx]

                if key_info["disabled"] or key_info["cooldown_until"] > now:
                    continue

                key_info["req_count"] += 1
                key_info["in_flight"] += 1
                key_info["last_used_at"] = now
                return {"name": key_info["name"], "key": key_info["key"]}

            return None

    def mark_success(self, key_str: str):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            key_info["in_flight"] = max(0, key_info["in_flight"] - 1)
            key_info["success_count"] += 1
            key_info["last_error"] = ""

    def mark_failure(
        self,
        key_str: str,
        *,
        reason: str,
        cooldown_seconds: int = 0,
        disable: bool = False,
        rate_limited: bool = False,
    ):
        with self._lock:
            key_info = self._find_key(key_str)
            if not key_info:
                return
            key_info["in_flight"] = max(0, key_info["in_flight"] - 1)
            key_info["failure_count"] += 1
            key_info["last_error"] = reason[:240]
            key_info["disabled"] = disable
            if rate_limited:
                key_info["rate_limit_count"] += 1
            if cooldown_seconds > 0:
                key_info["cooldown_until"] = max(
                    key_info["cooldown_until"], self._clock() + cooldown_seconds
                )

    def release(self, key_str: str):
        with self._lock:
            key_info = self._find_key(key_str)
            if key_info:
                key_info["in_flight"] = max(0, key_info["in_flight"] - 1)

    def _find_key(self, key_str: str) -> dict | None:
        return next((item for item in self.keys if item["key"] == key_str), None)

    def get_status(self) -> dict:
        with self._lock:
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
                        "in_flight": key_info["in_flight"],
                        "cooldown_remaining_sec": cooldown,
                        "last_error": key_info["last_error"],
                        "last_used_at": key_info["last_used_at"],
                        "key_prefix": self._mask_key(key_info["key"]),
                    }
                )

            total_requests = sum(item["req_count"] for item in self.keys)
            total_successes = sum(item["success_count"] for item in self.keys)
            return {
                "total_keys": len(self.keys),
                "available_keys": sum(item["status"] == "Active" for item in pool),
                "cooling_keys": sum(item["status"] == "Cooling" for item in pool),
                "disabled_keys": sum(item["status"] == "Disabled" for item in pool),
                "in_flight": sum(item["in_flight"] for item in self.keys),
                "total_requests": total_requests,
                "total_successes": total_successes,
                "success_rate": (
                    round(total_successes / total_requests * 100, 1)
                    if total_requests
                    else 0.0
                ),
                "pool": pool,
            }

    @staticmethod
    def _mask_key(key: str) -> str:
        if len(key) <= 12:
            return "***"
        return f"{key[:6]}...{key[-4:]}"
