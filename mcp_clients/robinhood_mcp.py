"""Robinhood READ-ONLY MCP client (2026-07-15) - wraps the `robinhood-mcp`
PyPI server (https://github.com/verygoodplugins/robinhood-mcp), which itself
wraps robin_stocks. Spawned locally via `uvx robinhood-mcp` (stdio), same
pattern as every other client in this package.

DESIGN GUARANTEE - this does NOT change the "no local order execution" rule:
the robinhood-mcp server exposes ZERO trading tools (read-only by design;
see its README), so nothing this class can call is capable of placing,
modifying, or cancelling an order. Execution stays Claude-Desktop-only via
output/trade_prompt.md + confirm_fill.py, exactly as before. What this adds
is local visibility into REAL account state - actual positions with cost
basis, portfolio value, buying power - so reconciliation (robinhood_sync.py)
and the ALREADY_OPEN veto can be grounded in what the account actually holds
instead of only what confirm_fill.py was told.

Credentials: ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD (+ ROBINHOOD_TOTP_SECRET
only if you use an authenticator app) from .env / the environment. With no
credentials set, every method returns its empty fallback instantly and logs
once - the rest of the platform is unaffected.

Operational notes (why the knobs below look the way they do):
- robin_stocks caches its session token in ~/.tokens/robinhood.pickle, so
  only the FIRST spawn does a real login; later spawns reuse the pickle.
  A full login (especially device-approval flows) can exceed the 30s MCP
  hard timeout - if the first-ever call times out, that's likely why; see
  README "Robinhood (read-only)" for the one-time warm-up step.
- Concurrency semaphore (1): this talks to YOUR authenticated account
  against an unofficial API - politeness here is account-safety, not just
  rate-limit hygiene. Nothing in this platform needs concurrent account
  reads anyway.
- Circuit breaker: same treatment as every other source, with a longer
  cooldown (30 min) because repeated failed logins are how accounts get
  flagged.
- Cache TTLs are short (60s positions/portfolio) - account state is the
  one thing we genuinely want fresh, but "fresh" for a 15-min scan cycle
  does not mean "re-login for every caller in the same cycle".
"""
import logging
import os
import threading

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker, StdioMCPClient, run_async

logger = logging.getLogger(__name__)

# Reuse market_data's minimal .env loader (import side effect already loads
# .env on first import anywhere in the process; calling again is a no-op for
# keys already set).
from mcp_clients.market_data import _load_dotenv  # noqa: E402

_load_dotenv()

TTL_ACCOUNT = 60          # positions/portfolio - fresh per cycle, shared within it
TTL_DIVIDENDS = 6 * 3600  # dividend history churns ~quarterly

_RH_CONCURRENCY = threading.Semaphore(1)

_warned_no_creds = False


def _credentials() -> dict | None:
    """Returns the env dict for the server spawn, or None if not configured."""
    user = os.getenv("ROBINHOOD_USERNAME", "").strip()
    pw = os.getenv("ROBINHOOD_PASSWORD", "").strip()
    if not user or not pw:
        return None
    env = {"ROBINHOOD_USERNAME": user, "ROBINHOOD_PASSWORD": pw}
    totp = os.getenv("ROBINHOOD_TOTP_SECRET", "").strip()
    if totp:
        env["ROBINHOOD_TOTP_SECRET"] = totp
    return env


class RobinhoodMCP:
    """Read-only account data. Every method degrades to an empty dict/list on
    any failure - callers must treat missing data as 'unknown', never as
    'flat/no position'."""

    def __init__(self):
        self._env = _credentials()
        self.client = (
            StdioMCPClient("uvx", ["robinhood-mcp"], env=self._env)
            if self._env else None
        )
        self.breaker = SourceCircuitBreaker("robinhood", fail_threshold=3,
                                            cooldown_seconds=1800)

    def configured(self) -> bool:
        return self.client is not None

    def _call(self, tool: str, params: dict = None, cache_key: str = None,
              ttl: int = TTL_ACCOUNT):
        global _warned_no_creds
        if not self.configured():
            if not _warned_no_creds:
                logger.info("robinhood: no ROBINHOOD_USERNAME/PASSWORD in .env - "
                            "read-only account data disabled (platform runs "
                            "normally without it)")
                _warned_no_creds = True
            return None
        if cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        if not self.breaker.available():
            return None
        with _RH_CONCURRENCY:
            result = run_async(self.client.call_tool(tool, params or {}))
        ok = result is not None and not (
            isinstance(result, dict) and set(result.keys()) == {"raw"}
        )
        if ok:
            self.breaker.record(True)
            if cache_key:
                cache.set(cache_key, result, ttl)
            return result
        err = ("no response - spawn failure or 30s timeout (first-ever call "
               "does a full Robinhood login, which can exceed the timeout; "
               "run `uvx robinhood-mcp` once manually to warm the session "
               "cache - see README)"
               if result is None else str(result.get("raw", ""))[:150])
        self.breaker.record(False, error=err)
        return None

    # -- account state ------------------------------------------------------

    def get_portfolio(self) -> dict:
        """Total value, equity, buying power, day change. {} on failure."""
        result = self._call("robinhood_get_portfolio", cache_key="rh_portfolio")
        return result if isinstance(result, dict) else {}

    def get_positions(self) -> list:
        """All real holdings with cost basis / current value / P&L.
        [] means UNKNOWN-or-empty - callers needing the distinction should
        check configured() and portfolio() first."""
        result = self._call("robinhood_get_positions", cache_key="rh_positions")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("positions", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_position(self, ticker: str) -> dict:
        """One holding by ticker - much cheaper than get_positions() for
        single-symbol questions. {} = unknown or not held."""
        result = self._call("robinhood_get_position", {"symbol": ticker.upper()},
                            cache_key=f"rh_position_{ticker.upper()}")
        return result if isinstance(result, dict) else {}

    def get_options_positions(self) -> list:
        result = self._call("robinhood_get_options_positions",
                            cache_key="rh_options_positions")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("positions", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_dividends(self) -> list:
        result = self._call("robinhood_get_dividends", cache_key="rh_dividends",
                            ttl=TTL_DIVIDENDS)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ("dividends", "results", "data"):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    # -- 2026-07-16 additions (full read coverage of the server's tool set) --

    @staticmethod
    def _as_list(result, *keys) -> list:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in keys:
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_accounts(self) -> list:
        """Account numbers for multi-account logins (robinhood_get_accounts)."""
        return self._as_list(
            self._call("robinhood_get_accounts", cache_key="rh_accounts",
                       ttl=TTL_DIVIDENDS),
            "accounts", "results", "data")

    def get_order_history(self, ticker: str = None) -> list:
        """Executed order history with per-fill detail
        (robinhood_get_order_history) - real cost-basis ground truth for
        reconciliation and the Journal view. Optionally filtered by symbol."""
        params = {"symbol": ticker.upper()} if ticker else {}
        key = f"rh_orders_{ticker.upper()}" if ticker else "rh_orders_all"
        return self._as_list(
            self._call("robinhood_get_order_history", params, cache_key=key,
                       ttl=TTL_ACCOUNT * 5),
            "orders", "results", "data")

    def get_watchlist(self) -> list:
        """Symbols in the account's Robinhood watchlists
        (robinhood_get_watchlist) - can be cross-referenced with the
        platform's own watchlist/screener."""
        return self._as_list(
            self._call("robinhood_get_watchlist", cache_key="rh_watchlist",
                       ttl=TTL_DIVIDENDS),
            "watchlist", "symbols", "results", "data")


# ── Process-wide singleton (2026-07-17) ─────────────────────────────────────
# Every RobinhoodMCP() gets its OWN circuit breaker, so per-call construction
# (the old pattern in robinhood_sync.py / server.py's status endpoint) reset
# the failure count each time - a repeatedly-failing login never actually
# tripped the breaker. One shared instance per process fixes that, and gives
# scheduler.py's per-cycle health probe a stable breaker/cache to lean on.
_client = None
_client_lock = threading.Lock()


def get_client() -> RobinhoodMCP:
    global _client
    with _client_lock:
        if _client is None:
            _client = RobinhoodMCP()
        return _client
