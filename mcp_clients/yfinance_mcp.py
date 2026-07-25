"""yfinance MCP - OHLCV, financials, news, options, holders. Via the MCP Python
SDK (stdio transport, `uvx yfmcp@latest`). No Claude, no API key."""
import asyncio

from mcp_clients.base import StdioMCPClient, run_async


class YFinanceMCP:
    def __init__(self):
        self.client = StdioMCPClient("uvx", ["yfmcp@latest"])

    async def _get_all(self, ticker: str, skip_holders_financials: bool = False,
                       skip_options: bool = False, skip_price_history: bool = False) -> dict:
        """Fetch all ticker data in parallel.

        skip_holders_financials: True for tickers already known to be ETFs
        (config.yaml's asset_profiles.etf_tickers, checked by the caller in
        engine/ticker_analyzer.py before we get here) - Yahoo's
        holders/financials fundamentals endpoints don't have data for ETFs
        (they're built for individual equities: institutional holders,
        quarterly financial statements), so those two calls are a guaranteed
        404 for e.g. SPY. Skipping them avoids that guaranteed-to-fail round
        trip (and its noisy ERROR log line) instead of firing it and
        discarding the exception every single cycle. Neither field is read
        anywhere downstream even when present (verified - nothing in this
        codebase does data.get("holders") or data.get("financials")), so
        returning None for them here changes nothing functionally."""
        calls = []
        keys = []
        # skip_price_history (2026-07-15): when a real market-data provider
        # (Alpaca/Tiingo/TwelveData via mcp_clients/market_data.py) is
        # configured and healthy, OHLCV comes from there - yfinance's
        # scraper-grade history endpoint (the source of the mass
        # STALE_DATA_CIRCUIT_BREAKER incidents) isn't called at all.
        if not skip_price_history:
            calls += [
                # period 3mo -> 1y (2026-07-15, zero-trades audit): ~63 daily
                # bars can never legitimately compute SMA200 (needs 200+), so
                # TREND's single biggest rule (above_sma200, 15 pts) and the
                # 200-day regime context were built on NaN/fallback values.
                self.client.call_tool("yfinance_get_price_history",
                                       {"symbol": ticker, "period": "1y", "interval": "1d"}),
                self.client.call_tool("yfinance_get_price_history",
                                       {"symbol": ticker, "period": "5d", "interval": "5m"}),
            ]
            keys += ["daily_ohlcv", "intraday_ohlcv"]
        calls += [
            self.client.call_tool("yfinance_get_ticker_info", {"symbol": ticker}),
            self.client.call_tool("yfinance_get_ticker_news", {"symbol": ticker}),
        ]
        keys += ["info", "news"]
        # skip_options (2026-07-15, no-buys-round-2 audit): the option chain
        # feeds ONLY the informational put/call ratio in the analysis prompt -
        # it's not a scoring input anywhere. Fetching it for every screener
        # candidate (up to ~70/cycle) was one full MCP subprocess spawn per
        # ticker of pure display data. The caller now skips it for
        # non-watchlist tickers.
        if not skip_options:
            calls.append(self.client.call_tool("yfinance_get_option_chain", {"symbol": ticker}))
            keys.append("options")
        if not skip_holders_financials:
            calls += [
                self.client.call_tool("yfinance_get_financials",
                                       {"symbol": ticker, "frequency": "quarterly"}),
                self.client.call_tool("yfinance_get_holders", {"symbol": ticker}),
            ]
            keys += ["financials", "holders"]
        results = await asyncio.gather(*calls, return_exceptions=True)
        data = {k: (v if not isinstance(v, Exception) else None) for k, v in zip(keys, results)}
        if skip_holders_financials:
            data["financials"] = None
            data["holders"] = None
        if skip_options:
            data["options"] = None
        if skip_price_history:
            data["daily_ohlcv"] = None
            data["intraday_ohlcv"] = None
        return data

    def get_all(self, ticker: str, skip_holders_financials: bool = False,
                skip_options: bool = False, skip_price_history: bool = False) -> dict:
        return run_async(self._get_all(ticker, skip_holders_financials=skip_holders_financials,
                                       skip_options=skip_options,
                                       skip_price_history=skip_price_history)) or {}

    def get_vix(self) -> float:
        result = run_async(self.client.call_tool("yfinance_get_ticker_info", {"symbol": "^VIX"}))
        if result:
            return result.get("regularMarketPrice", result.get("currentPrice", 20.0))
        return 20.0

    def get_price_history(self, ticker: str, period: str = "3mo", interval: str = "1d") -> dict:
        """Single lightweight call (just OHLCV, not the full get_all() bundle of
        news/financials/holders/options) - used by engine/market_breadth.py to
        pull closes for the 11 sector ETFs without hammering the MCP server
        with 7x the necessary calls per instrument."""
        result = run_async(self.client.call_tool(
            "yfinance_get_price_history", {"symbol": ticker, "period": period, "interval": interval}
        ))
        return result or {}

    def get_ticker_info(self, ticker: str) -> dict:
        """Single lightweight call (just yfinance_get_ticker_info, not the
        full get_all() bundle) - used to validate a ticker symbol and look up
        its company name when adding it to the watchlist (server.py's
        /api/ticker/validate), without waiting on news/financials/options
        that aren't needed for that check."""
        result = run_async(self.client.call_tool("yfinance_get_ticker_info", {"symbol": ticker}))
        return result or {}

    # ------------------------------------------------------------------
    # Screener tools - used by engine/screener.py. Tool names and parameter
    # shapes below are REAL/VERIFIED (transcribed from the yfmcp PyPI page,
    # https://pypi.org/project/yfmcp/, not guessed) - unlike a ticker lookup,
    # nothing else in this codebase has called these three before, so the
    # exact shape of each row in the response (which key holds the ticker
    # symbol, percent change, etc.) has NOT been verified against live output.
    # engine/screener.py defends against that with the same multi-key-fallback
    # pattern used throughout this codebase (e.g. ticker_analyzer.py's
    # stock.get("symbol") or stock.get("ticker") or stock.get("Symbol")).
    # ------------------------------------------------------------------

    def screen_gappers(self, min_percent_change: float = 3.0, min_price: float = 5.0,
                        min_volume: int = 500_000, min_market_cap: int = 2_000_000_000,
                        size: int = 50) -> dict:
        """yfinance_screen_gappers - purpose-built Yahoo screener for
        opening-session bullish gappers. Real tool, real params (all of the
        above are the tool's own documented defaults)."""
        result = run_async(self.client.call_tool("yfinance_screen_gappers", {
            "min_percent_change": min_percent_change, "min_price": min_price,
            "min_volume": min_volume, "min_market_cap": min_market_cap,
            "region": "us", "size": size,
        }))
        return result or {}

    def screen_equity(self, min_percent_change: float = 3.0, min_price: float = 5.0,
                       min_volume: int = 500_000, sort_field: str = "percentchange",
                       size: int = 50) -> dict:
        """yfinance_screen with query_type="equity" - a custom screener tree,
        using the exact operator/operand shape from yfmcp's own documented
        example (gt percentchange / eq region / gte intradayprice / gt
        dayvolume). Real tool. sort_field lets callers reuse this for both a
        "gainers" view (sort_field="percentchange") and a "volume" view
        (sort_field="dayvolume") without two near-identical query builders."""
        query = {
            "operator": "and",
            "operands": [
                {"operator": "gt", "operands": ["percentchange", min_percent_change]},
                {"operator": "eq", "operands": ["region", "us"]},
                {"operator": "gte", "operands": ["intradayprice", min_price]},
                {"operator": "gt", "operands": ["dayvolume", min_volume]},
            ],
        }
        result = run_async(self.client.call_tool("yfinance_screen", {
            "query_type": "equity", "query": query,
            "sort_field": sort_field, "sort_asc": False, "size": size,
        }))
        return result or {}

    def get_top_in_sector(self, sector: str, top_type: str = "top_performing_companies",
                           top_n: int = 10) -> dict:
        """yfinance_get_top - top-ranked entities within a Yahoo market
        sector. Real tool. `sector` must be one of yfmcp's 11 documented
        sector names (Technology, Financial Services, Healthcare, Energy,
        etc. - see engine/screener.py's YFINANCE_SECTOR_NAMES for the exact
        list and how it maps from this codebase's SPDR sector ETF tickers)."""
        result = run_async(self.client.call_tool("yfinance_get_top", {
            "sector": sector, "top_type": top_type, "top_n": top_n,
        }))
        return result or {}
