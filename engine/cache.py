"""TTL cache for MCP results, so a 30-min scan interval across an 8-ticker
watchlist doesn't hammer any of the 6 MCP servers more than once per TTL window."""
import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe: scheduler.py's per-ticker cycle loop now runs concurrently
    (ThreadPoolExecutor, see _run_cycle_impl()'s 2026-07-14 performance pass),
    so multiple threads call get()/set() on this shared instance at once. Each
    ticker uses its own cache key (f"ticker_{ticker}") so collisions are rare,
    but the lock makes read-check-delete in get() atomic regardless - cheap
    (in-memory dict, no I/O) so the lock is not a bottleneck."""
    def __init__(self):
        self._store = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.time() < expires_at:
                    return value
                del self._store[key]
            return None

    def set(self, key: str, value: Any, ttl_seconds: int):
        with self._lock:
            self._store[key] = (value, time.time() + ttl_seconds)

    def clear(self):
        with self._lock:
            self._store.clear()


# Global cache instance
cache = TTLCache()

# TTL constants (seconds)
TTL_FEAR_GREED = 900   # 15 min
TTL_FRED = 3600        # 60 min
TTL_VIX = 300          # 5 min
TTL_SECTOR = 900       # 15 min
TTL_CALENDAR = 3600    # 60 min
TTL_TICKER = 300       # 5 min per ticker
# 2026-07-16 (cycle-overrun fix): lite screener-candidate analyses get a
# 10-min TTL. At exactly 300s, the cache expired precisely at HYBRID's 5-min
# cycle cadence, so every one of ~38 screener candidates was re-fetched cold
# every single cycle. 10 min means alternate cycles hit warm cache; the
# phase-2 promotion path still does a FULL fresh fetch for anything near the
# buy bar, so no trade decision rides on a stale lite quote.
TTL_TICKER_LITE = 600  # 10 min for lite (bars/quote/info-only) results
TTL_FINVIZ = 900       # 15 min
TTL_MAVERICK = 300     # 5 min
