"""TTL cache for MCP results, so a 30-min scan interval across an 8-ticker
watchlist doesn't hammer any of the 6 MCP servers more than once per TTL window."""
import hashlib
import logging
import os
import pickle
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TTLCache:
    """Thread-safe: scheduler.py's per-ticker cycle loop now runs concurrently
    (ThreadPoolExecutor, see _run_cycle_impl()'s 2026-07-14 performance pass),
    so multiple threads call get()/set() on this shared instance at once. Each
    ticker uses its own cache key (f"ticker_{ticker}") so collisions are rare,
    but the lock makes read-check-delete in get() atomic regardless - cheap
    (in-memory dict, no I/O) so the lock is not a bottleneck."""
    def __init__(self, disk_dir=None):
        self._store = {}
        self._lock = threading.Lock()
        # 2026-10-28: since 2026-07-22 every scan cycle runs in its OWN short-lived child
        # process (engine/cycle_supervisor.py), so a memory-only cache was empty at the start
        # of every cycle: every "cached 2h/4h/15min" assumption in this codebase was silently
        # void and each cycle re-fetched everything (market context ~28s, the screener,
        # quota-limited APIs). Entries are now also written to disk so a later cycle's process
        # sees them, honouring the same TTLs. Best-effort throughout: any disk problem (unpicklable
        # value, corrupt file, read-only dir) falls back to plain memory behaviour.
        self._disk = Path(disk_dir) if disk_dir else _default_disk_dir()
        self._purged = False

    # ---- disk helpers ----
    def _path(self, key: str) -> Path:
        return self._disk / (hashlib.sha1(key.encode()).hexdigest() + ".pkl")

    def _disk_get(self, key: str):
        if self._disk is None:
            return None
        try:
            with open(self._path(key), "rb") as f:
                k, expires_at, value = pickle.load(f)
            if k == key and time.time() < expires_at:
                return value, expires_at
        except Exception:
            pass
        return None

    def _disk_set(self, key: str, value: Any, expires_at: float):
        if self._disk is None:
            return
        try:
            self._disk.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._disk, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    pickle.dump((key, expires_at, value), f, protocol=4)
                os.replace(tmp, self._path(key))
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            if not self._purged:
                self._purged = True
                self._purge_expired()
        except Exception as e:                      # e.g. unpicklable value: memory-only for this key
            logger.debug(f"cache: not persisting {key!r}: {e}")

    def _purge_expired(self):
        """Drop files older than a day (expired entries are also ignored on read)."""
        cutoff = time.time() - 86400
        try:
            for p in self._disk.glob("*.pkl"):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except Exception:
            pass

    # ---- public API (unchanged) ----
    def get(self, key: str) -> Any | None:
        with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.time() < expires_at:
                    return value
                del self._store[key]
        hit = self._disk_get(key)
        if hit is not None:
            value, expires_at = hit
            with self._lock:
                self._store[key] = (value, expires_at)
            return value
        return None

    def set(self, key: str, value: Any, ttl_seconds: int):
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._store[key] = (value, expires_at)
        self._disk_set(key, value, expires_at)

    def clear(self):
        with self._lock:
            self._store.clear()
        if self._disk is not None:
            try:
                for p in self._disk.glob("*.pkl"):
                    p.unlink()
            except Exception:
                pass


def _default_disk_dir():
    # Tests must never read a previous run's cache or write into the real output dir.
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in os.environ.get("_", ""):
        return None
    try:
        from storage.paths import output_dir
        return Path(str(output_dir())) / "cache"
    except Exception:
        return None


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
