"""In-memory cache with per-key TTL. Thread-safe enough for our ThreadPoolExecutor usage."""
import threading
import time
from typing import Any, Callable, Optional


class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: float):
        with self._lock:
            self._store[key] = (time.time() + ttl_seconds, value)

    def get_or_fetch(self, key: str, ttl_seconds: float, fetch_fn: Callable[[], Any]) -> Any:
        """Return cached value if fresh, otherwise call fetch_fn(), cache it, and return it.
        On fetch_fn exception, re-raise (caller decides fallback / partial-data handling)."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = fetch_fn()
        self.set(key, value, ttl_seconds)
        return value

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()


# Module-level singleton shared across the whole process
cache = TTLCache()
