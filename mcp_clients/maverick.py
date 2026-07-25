"""maverick-mcp - RSI/MACD/BB/technical analysis/news sentiment. Via the MCP
Python SDK's streamable-HTTP transport (MaverickMCP runs as a local HTTP service
at localhost:8003 - this project never starts it). OPTIONAL: if it's not up,
`available` is False and every call short-circuits to {} so ticker_analyzer.py
falls back to its own pandas-ta calculations."""
import asyncio
import logging
import threading
import time

from engine.cache import cache
from mcp_clients.base import HttpMCPClient, SourceCircuitBreaker, run_async

logger = logging.getLogger(__name__)

TTL_MAVERICK = 600  # 10 min - technicals/news sentiment, fresher than finviz fundamentals

# 2026-07-14: real production evidence showed scheduler.py's 4-way parallel
# ticker loop, each firing MaverickMCP.get_all()'s own 5-way asyncio.gather,
# bursting up to ~20 concurrent local HTTP MCP sessions at localhost:8003 -
# which this machine's Maverick server handled badly (repeated "GET stream
# disconnected, reconnecting" churn), eventually wedging a worker thread hard
# enough to defeat call_tool()'s own 45s timeout (see base.py's run_async()
# docstring for the full mechanism) and leave server.py's process silently
# hung for 35+ minutes. Capping how many TICKERS may be inside a Maverick
# call at once - independent of scheduler.py's own ticker-level concurrency
# knob - keeps the burst against this one fragile local dependency gentle
# without reducing per-ticker parallelism for everything else.
_MAVERICK_CONCURRENCY = threading.Semaphore(2)


class MaverickMCP:
    def __init__(self):
        # No trailing slash (2026-07-15): Maverick's own logs showed every
        # request to /mcp/ bouncing through a 307 redirect to /mcp first -
        # pure wasted latency on every one of the 5 calls per ticker.
        self.client = HttpMCPClient("http://127.0.0.1:8003/mcp")
        # 2026-07-15 (no-buys-round-2 audit): availability used to be checked
        # exactly ONCE, at process start. If Maverick was started (or
        # restarted) after scheduler.py, or blipped once at the wrong moment,
        # self.available stayed False for the entire life of the process and
        # every maverick_bullish/news-sentiment signal silently read as
        # missing - matching production data where maverick fired in 0 of
        # 149 signals while the server was verifiably running. Now re-checked
        # every _AVAILABILITY_RECHECK_SECONDS, plus a circuit breaker for
        # mid-run failures (down 5 min after 3 consecutive failures rather
        # than burning timeouts per ticker).
        self._available = self._check_available()
        self._last_check = time.time()
        self.breaker = SourceCircuitBreaker("maverick", fail_threshold=3, cooldown_seconds=300)

    _AVAILABILITY_RECHECK_SECONDS = 300

    @property
    def available(self) -> bool:
        if time.time() - self._last_check > self._AVAILABILITY_RECHECK_SECONDS:
            self._available = self._check_available()
            self._last_check = time.time()
        return self._available

    def _check_available(self) -> bool:
        try:
            import requests
            requests.get("http://127.0.0.1:8003/mcp", timeout=3)
            ok = True
        except Exception:
            ok = False
        if not ok:
            # Surface "server unreachable" distinctly in the Data Sources
            # panel - different action item than "reachable but erroring".
            try:
                from storage.database import Database
                Database().upsert_source_health(
                    "maverick", False,
                    error="localhost:8003 unreachable - is the Maverick server running?")
            except Exception:
                pass
        return ok

    async def _get_all(self, ticker: str) -> dict:
        # Param renamed symbol -> ticker (2026-07-15): Maverick's server logs
        # showed every call REJECTED with "Missing required argument:
        # 'ticker' / Unexpected keyword argument: 'symbol'" - the server was
        # up and reachable the whole time, but this client was speaking the
        # wrong argument name, so maverick_bullish/news sentiment silently
        # never contributed to a single signal.
        #
        # get_news_sentiment REMOVED from this gather (2026-07-17, hang
        # forensics round 2): launchd_maverick.log shows this tool is backed
        # by a full "DeepResearchAgent" doing PARALLEL EXTERNAL WEB SEARCH
        # (an Exa search provider) with its own internal 120s timeout
        # budget - not a simple indicator lookup like the other 4 calls.
        # Live server logs show it currently resolving 0 of 4 research
        # sub-tasks successfully ("Parallel research completed: 0
        # successful, 4 failed") - i.e. zero signal value right now - while
        # cross-referencing scheduler.log showed get_news_sentiment tied for
        # the most hard-30/40s-ceiling timeouts of any single Maverick tool.
        # Because all 5 calls previously shared one asyncio.gather against
        # the SAME local server process, a slow/stuck sentiment request could
        # also starve the other 4 fast technical calls riding along with it.
        # Dropping just this one call keeps the genuinely fast, valuable
        # RSI/MACD/support-resistance/technical-analysis overlay while
        # removing the single most expensive, currently-valueless, and
        # most latency-unpredictable part of this integration.
        # td.maverick_sentiment simply stays at its neutral 0.5 default now
        # (identical to when Maverick is fully unavailable) - news sentiment
        # is still covered by ticker_analyzer.py's own local
        # _score_sentiment() over yfinance headlines.
        results = await asyncio.gather(
            self.client.call_tool("get_full_technical_analysis", {"ticker": ticker}),
            self.client.call_tool("get_rsi_analysis", {"ticker": ticker}),
            self.client.call_tool("get_macd_analysis", {"ticker": ticker}),
            self.client.call_tool("get_support_resistance", {"ticker": ticker}),
            return_exceptions=True,
        )
        def _clean(v):
            # Exceptions and unparseable {"raw": ...} error payloads (e.g.
            # the invalid-argument rejections that hid the symbol->ticker
            # bug) both count as "no data", so they can't poison the cache
            # or fool the circuit breaker's success check.
            if isinstance(v, Exception) or v is None:
                return None
            if isinstance(v, dict) and set(v.keys()) == {"raw"}:
                return None
            return v

        return {
            "technical": _clean(results[0]),
            "rsi": _clean(results[1]),
            "macd": _clean(results[2]),
            "support_resistance": _clean(results[3]),
            "sentiment": None,  # see get_news_sentiment removal note above
        }

    # 2026-07-16 (hang forensics): `with _MAVERICK_CONCURRENCY:` blocked
    # FOREVER, silently, if the 2 permits were held by threads that
    # run_async()'s escape valve had abandoned (a stuck teardown holds its
    # permit for the life of the process). Two leaked permits meant every
    # subsequent Maverick call - and therefore every per-ticker worker, and
    # therefore the whole cycle - wedged in Semaphore.acquire() with zero
    # log output: exactly the 25-35min silent hangs in production logs.
    # A bounded acquire converts that death spiral into a logged skip.
    _SEMAPHORE_ACQUIRE_TIMEOUT = 60  # seconds

    def get_all(self, ticker: str) -> dict:
        cached = cache.get(f"maverick_{ticker}")
        if cached is not None:
            return cached
        if not self.available or not self.breaker.available():
            return {}
        if not _MAVERICK_CONCURRENCY.acquire(timeout=self._SEMAPHORE_ACQUIRE_TIMEOUT):
            logger.warning(
                f"maverick {ticker}: couldn't acquire a concurrency slot within "
                f"{self._SEMAPHORE_ACQUIRE_TIMEOUT}s - permits likely leaked by "
                f"abandoned stuck calls. Skipping maverick for this ticker "
                f"(degrades to ta_fallback, same as maverick being down).")
            self.breaker.record(False, error="semaphore acquire timeout")
            return {}
        try:
            result = run_async(self._get_all(ticker)) or {}
        finally:
            _MAVERICK_CONCURRENCY.release()
        ok = any(v is not None for v in result.values()) if result else False
        self.breaker.record(ok)
        if ok:
            cache.set(f"maverick_{ticker}", result, TTL_MAVERICK)
        return result
