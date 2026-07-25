"""stock-scanner MCP - SEC filings, insider trades, technical ratings, sector
performance, options flow. Via the MCP Python SDK (stdio).

2026-07-16 (missing-tool warning fix, Akhil's report): production logs were
full of 'MCP error -32602: Tool get_earnings_calendar/get_short_interest/
get_analyst_ratings/get_insider_trades not found' - the unpinned
`npx -y stock-scanner-mcp` resolved to a release that no longer exposes
those four per-ticker tools, but this module kept calling them blind:
4 doomed calls x 2 retries each x per ticker x per cycle, every one
spawning a fresh npx subprocess just to be told the tool doesn't exist.

Fix: discover the server's ACTUAL tool set once via list_tools() (cached
24h when discovery succeeds, 15min when it fails so a transient failure
doesn't blind us for a day), then only ever call tools that exist. Missing
tools yield None fields - the exact shape callers already handle - plus a
single 'tools missing' log line per discovery instead of a warning storm.

2026-07-22 (Trinath: "finviz and 2 FMP endpoints cannot be used, see if this
data can be sourced elsewhere"): re-ran discovery against the CURRENTLY
installed stock-scanner-mcp@1.18.0 and confirmed the tool map above was
completely stale - not one of get_insider_trades/get_short_interest/
get_analyst_ratings/get_earnings_calendar/get_sector_performance/
get_economic_calendar/get_market_overview exists anymore (production logs:
"stock-scanner: server does not expose [...] (available: [...])"). This
MCP has been silently contributing ZERO signal this whole time - not because
it's down, but because every tool this file asked for was renamed/removed
upstream. The version installed NOW exposes a materially different, and
genuinely useful, surface: SEC EDGAR filings (edgar_insider_trades,
edgar_institutional_holdings, edgar_ownership_filings), TradingView-sourced
technicals/sector data (tradingview_technicals, tradingview_sector_performance),
and real options-flow tools (options_unusual_activity, options_put_call_ratio).
Remapped below to the tools that actually exist, closing three real gaps at
once:
  - insider_trades: now edgar_insider_trades (real SEC filings) instead of
    the dead get_insider_trades - fixes rules/swing_buy_rules.py's
    insider_net_buying (EXTERNAL... actually SENTIMENT_MACRO bucket), which
    had been permanently neutral/false.
  - technical_rating: NEW - tradingview_technicals, a genuine third-party
    technical gauge (Buy/Sell/Neutral) to sit alongside (not silently
    replace) finviz's own SMA/RSI-derived heuristic rating - see
    ticker_analyzer.py's _parse_scanner for how the two are reconciled.
  - unusual_options: NEW - options_unusual_activity upgrades
    unusual_options_bullish from a permanent 0-point placeholder (the
    documented paid-API-only signal, github.com/erikmaday/unusual-whales-mcp,
    never configured) to a real, already-connected source.
HONESTY NOTE: the exact param name these new tools expect (assumed "symbol",
matching every other per-ticker tool this file has ever called) is NOT
verified against a live call - this codebase has been burned by exactly this
before (mcp_clients/maverick.py's 2026-07-15 "wrong argument name" bug, which
silently zeroed maverick_bullish for weeks). If these new fields stay empty
in the Data Sources panel / source_health after this deploys, check the
argument name first, same lesson maverick.py already learned the hard way.
"""
import asyncio

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker, StdioMCPClient, run_async
import logging

logger = logging.getLogger(__name__)

# 2026-07-15 (no-buys-round-2 audit): insider trades / short interest /
# analyst ratings churn daily at most, but this was being re-fetched (4 MCP
# calls, each spawning a fresh `npx` subprocess) per ticker per 15-min cycle
# across up to 70 screener candidates. 6h cache + circuit breaker, same
# rationale as finviz_mcp.py.
TTL_SCANNER = 6 * 3600
TTL_TOOLS_OK = 24 * 3600     # discovered tool set (server version changes rarely)
TTL_TOOLS_FAILED = 15 * 60   # failed discovery - retry soon, don't blind for a day

# result-dict key -> MCP tool name (2026-07-22 remap - see module docstring)
_TICKER_TOOLS = {
    "insider_trades": "edgar_insider_trades",
    "technical_rating": "tradingview_technicals",
    "unusual_options": "options_unusual_activity",
}
_MARKET_TOOLS = {
    "sectors": "tradingview_sector_performance",
}


class StockScannerMCP:
    def __init__(self):
        # 2026-07-20: pinned after an unpinned `@latest` resolve produced a
        # non-JSON-RPC line on stdout during list_tools() (logged as
        # "Failed to parse JSONRPC message from server" in mcp.client.stdio).
        # 1.18.0 is the version that was resolving at the time; bump
        # deliberately, not silently via @latest, so a future stdout-framing
        # regression doesn't recur unnoticed.
        self.client = StdioMCPClient("npx", ["-y", "stock-scanner-mcp@1.18.0"])
        self.breaker = SourceCircuitBreaker("stock-scanner", fail_threshold=3, cooldown_seconds=900)

    async def _available_tools(self) -> set:
        """The server's real tool set - one list_tools() per process per TTL
        window instead of discovering 'tool not found' the expensive way on
        every call."""
        cached = cache.get("scanner_tool_names")
        if cached is not None:
            return set(cached)
        tools = await self.client.list_tools()
        names = {t.name for t in tools}
        if names:
            cache.set("scanner_tool_names", sorted(names), TTL_TOOLS_OK)
            wanted = set(_TICKER_TOOLS.values()) | set(_MARKET_TOOLS.values())
            missing = sorted(wanted - names)
            if missing:
                logger.info(
                    f"stock-scanner: server does not expose {missing} "
                    f"(available: {sorted(names)}) - those fields will stay None; "
                    f"no calls will be attempted for them")
        else:
            # Discovery itself failed (server not installed / npx hung) -
            # cache the empty set briefly so we skip the per-tool calls this
            # window rather than firing them all just to fail one by one.
            cache.set("scanner_tool_names", [], TTL_TOOLS_FAILED)
            logger.warning("stock-scanner: tool discovery failed - skipping "
                           f"scanner calls for {TTL_TOOLS_FAILED // 60}min")
        return names

    async def _gather_existing(self, tool_map: dict, params_for: dict) -> dict:
        """Calls only the tools the server actually has; absent tools map to
        None fields (the shape every caller already handles)."""
        available = await self._available_tools()
        keys = [k for k, tool in tool_map.items() if tool in available]
        if not keys:
            return {k: None for k in tool_map}
        results = await asyncio.gather(
            *(self.client.call_tool(tool_map[k], params_for.get(k, {})) for k in keys),
            return_exceptions=True,
        )
        out = {k: None for k in tool_map}
        for k, r in zip(keys, results):
            out[k] = r if not isinstance(r, Exception) else None
        return out

    async def _get_market_data(self) -> dict:
        return await self._gather_existing(_MARKET_TOOLS, {})

    def get_market_data(self) -> dict:
        return run_async(self._get_market_data()) or {}

    async def _get_ticker_data(self, ticker: str) -> dict:
        # 2026-07-22 param-name fix (Trinath: "most tickers not scored, what
        # degraded it" - confirmed via live production logs): every call to
        # edgar_insider_trades and tradingview_technicals was failing with
        # "MCP error -32602: Invalid arguments" on EVERY ticker, EVERY cycle,
        # since this module's 2026-07-22 remap (see class docstring) assumed
        # every tool took {"symbol": ticker} like every other per-ticker tool
        # this file has ever called - exactly the risk flagged in that same
        # remap's own honesty note. The real, confirmed (from the error
        # payloads' own "path" field) shapes are per-tool, not uniform:
        #   - edgar_insider_trades wants {"ticker": "..."} (singular, "ticker"
        #     not "symbol")
        #   - tradingview_technicals wants {"tickers": [...]} - an ARRAY
        #     field, a batch-oriented shape, not a single-symbol call at all
        #   - options_unusual_activity: no error observed in production logs
        #     for this one - left as {"symbol": ticker} since there's no
        #     evidence yet that it's wrong (unlike the two above, which
        #     failed on literally every call).
        params = {
            "insider_trades": {"ticker": ticker},
            "technical_rating": {"tickers": [ticker]},
            "unusual_options": {"symbol": ticker},
        }
        return await self._gather_existing(_TICKER_TOOLS, params)

    def get_ticker_data(self, ticker: str) -> dict:
        cached = cache.get(f"scanner_{ticker}")
        if cached is not None:
            return cached
        if not self.breaker.available():
            return {}
        result = run_async(self._get_ticker_data(ticker)) or {}
        ok = any(v is not None for v in result.values()) if result else False
        # Don't punish the breaker when the server simply doesn't HAVE the
        # per-ticker tools - that's a known capability gap, not an outage.
        known_tools = set(cache.get("scanner_tool_names") or [])
        ticker_capable = bool(known_tools & set(_TICKER_TOOLS.values()))
        if ticker_capable or ok:
            self.breaker.record(ok)
        if ok:
            cache.set(f"scanner_{ticker}", result, TTL_SCANNER)
        return result
