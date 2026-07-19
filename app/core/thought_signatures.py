import json
import threading
import time
from collections import OrderedDict


class ThoughtSignatureStore:
    def __init__(self, ttl_seconds: float = 3600, max_entries: int = 2048):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._by_call_id: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._by_fingerprint: OrderedDict[str, tuple[float, str]] = OrderedDict()

    @staticmethod
    def _fingerprint(name: str, arguments) -> str:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return f"{name}:{canonical}"

    def remember(self, call_id: str, name: str, arguments, signature: str):
        if not signature:
            return
        expires_at = time.monotonic() + self.ttl_seconds
        fingerprint = self._fingerprint(name, arguments)
        with self._lock:
            self._prune()
            self._by_call_id[call_id] = (expires_at, signature)
            self._by_fingerprint[fingerprint] = (expires_at, signature)
            while len(self._by_call_id) > self.max_entries:
                self._by_call_id.popitem(last=False)
            while len(self._by_fingerprint) > self.max_entries:
                self._by_fingerprint.popitem(last=False)

    def resolve(self, call_id: str, name: str, arguments) -> str | None:
        fingerprint = self._fingerprint(name, arguments)
        with self._lock:
            self._prune()
            entry = self._by_call_id.get(call_id) or self._by_fingerprint.get(
                fingerprint
            )
            return entry[1] if entry else None

    def clear(self):
        with self._lock:
            self._by_call_id.clear()
            self._by_fingerprint.clear()

    def _prune(self):
        now = time.monotonic()
        for mapping in (self._by_call_id, self._by_fingerprint):
            expired = [key for key, (expires_at, _) in mapping.items() if expires_at <= now]
            for key in expired:
                mapping.pop(key, None)


thought_signature_store = ThoughtSignatureStore()
