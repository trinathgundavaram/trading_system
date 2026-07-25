"""Layer 1 - global market data + the market-wide trading gate, fetched ONCE
per cycle via direct MCP protocol calls (fear-greed, yfinance, stock-scanner,
fred), all through the Python `mcp` SDK - no Claude, no API key."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from engine.cache import cache, TTL_FEAR_GREED, TTL_FRED, TTL_VIX, TTL_SECTOR
from mcp_clients.fear_greed import FearGreedMCP
from mcp_clients.yfinance_mcp import YFinanceMCP
from mcp_clients.stock_scanner import StockScannerMCP
from mcp_clients.fred_mcp import FredMCP


@dataclass
class MarketContextData:
    fear_greed_score: int = 50
    fear_greed_rating: str = "neutral"
    vix_score: float = 50
    put_call_score: float = 50
    breadth_score: float = 50
    momentum_score: float = 50
    junk_bond_score: float = 50
    safe_haven_score: float = 50
    vix_level: float = 20.0
    vix_is_elevated: bool = False
    vix_is_high: bool = False
    yield_spread: float = 0.0
    yield_curve_inverted: bool = False
    cpi_trend: str = "stable"
    fed_funds_rate: float = 5.0
    unemployment: float = 4.0
    hours_to_next_major_macro: float = 999
    blackout_active: bool = False
    blackout_reason: str = ""
    sector_leaders: list = None
    sector_laggards: list = None
    put_call_ratio: float = 1.0
    market_breadth: float = 50.0
    can_trade: bool = True
    no_trade_reason: str = ""

    def __post_init__(self):
        if self.sector_leaders is None:
            self.sector_leaders = []
        if self.sector_laggards is None:
            self.sector_laggards = []


class MarketContext:
    def __init__(self):
        self.fg = FearGreedMCP()
        self.yf = YFinanceMCP()
        self.scanner = StockScannerMCP()
        self.fred = FredMCP()

    def fetch(self) -> MarketContextData:
        """Fetch all market context in parallel, with caching.

        2026-07-17 (hang forensics): this method's four .result() calls had
        NO timeout, and `with ThreadPoolExecutor(...) as ex:` calls
        shutdown(wait=True) on exit - the exact same trap documented in
        mcp_clients/base.py's run_async() docstring and already fixed in
        scheduler.py's ticker-analysis loop, but never applied here. Because
        this is called synchronously at the very START of every cycle -
        before ticker-analysis's own 20-min budget/cancel-flag protections
        even exist yet - a genuine wedge in any ONE of fear-greed/VIX/macro/
        market-data (beyond what run_async()'s own 40s escape valve can
        catch) froze the WHOLE cycle with no way out except killing the
        process. Confirmed in production: cycle_status showed stage=
        'market_context' for 70+ minutes straight. Bounded per-future
        timeouts + shutdown(wait=False) let this degrade to neutral defaults
        for whichever single piece is stuck, same as if that source were
        simply unavailable, instead of hanging forever."""
        ex = ThreadPoolExecutor(max_workers=4)
        fg_future = ex.submit(self._get_fear_greed)
        vix_future = ex.submit(self._get_vix)
        macro_future = ex.submit(self._get_macro)
        market_future = ex.submit(self._get_market_data)

        _TIMEOUT = 45  # seconds - just above run_async()'s own 40s hard ceiling

        def _safe_result(future, name, default):
            try:
                return future.result(timeout=_TIMEOUT)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"MarketContext.fetch(): {name} didn't resolve within "
                    f"{_TIMEOUT}s ({e}) - using neutral defaults for this "
                    f"cycle instead of blocking on it.")
                return default

        fg = _safe_result(fg_future, "fear_greed", {})
        vix = _safe_result(vix_future, "vix", 20.0)
        macro = _safe_result(macro_future, "macro", {})
        market = _safe_result(market_future, "market_data", {})
        ex.shutdown(wait=False)  # don't block cleanup on a future we already gave up on

        mkt = MarketContextData(
            fear_greed_score=fg.get("score", 50),
            fear_greed_rating=fg.get("rating", "neutral"),
            vix_score=fg.get("vix_score", 50),
            put_call_score=fg.get("put_call_score", 50),
            breadth_score=fg.get("breadth_score", 50),
            momentum_score=fg.get("momentum_score", 50),
            junk_bond_score=fg.get("junk_bond_score", 50),
            safe_haven_score=fg.get("safe_haven_score", 50),
            vix_level=vix,
            vix_is_elevated=vix > 20,
            vix_is_high=vix > 28,
            yield_spread=macro.get("yield_spread_2s10s") or 0.0,
            yield_curve_inverted=macro.get("yield_curve_inverted", False),
            cpi_trend=macro.get("cpi_trend", "stable"),
            fed_funds_rate=macro.get("fed_funds_rate") or 5.0,
            unemployment=macro.get("unemployment") or 4.0,
            sector_leaders=market.get("sector_leaders", []),
            sector_laggards=market.get("sector_laggards", []),
            hours_to_next_major_macro=market.get("hours_to_next_macro", 999),
            blackout_active=market.get("blackout_active", False),
            blackout_reason=market.get("blackout_reason", ""),
        )
        return mkt

    def _get_fear_greed(self) -> dict:
        cached = cache.get("fear_greed")
        if cached:
            return cached
        result = self.fg.get_index()
        cache.set("fear_greed", result, TTL_FEAR_GREED)
        return result

    def _get_vix(self) -> float:
        cached = cache.get("vix")
        if cached:
            return cached
        result = self.yf.get_vix()
        cache.set("vix", result, TTL_VIX)
        return result

    def _get_macro(self) -> dict:
        cached = cache.get("fred_macro")
        if cached:
            return cached
        result = self.fred.get_macro()
        cache.set("fred_macro", result, TTL_FRED)
        return result

    def _get_market_data(self) -> dict:
        cached = cache.get("market_data")
        if cached:
            return cached
        result = self._parse_market_data(self.scanner.get_market_data())
        cache.set("market_data", result, TTL_SECTOR)
        return result

    def _parse_market_data(self, raw: dict) -> dict:
        sectors = raw.get("sectors") or {}
        calendar = raw.get("calendar") or {}

        if isinstance(sectors, dict) and sectors:
            sorted_sectors = sorted(sectors.items(), key=lambda x: x[1], reverse=True)
            leaders = [s[0] for s in sorted_sectors[:3]]
            laggards = [s[0] for s in sorted_sectors[-3:]]
        else:
            leaders, laggards = [], []

        blackout, blackout_reason, hours_to_macro = self._check_blackout(calendar)

        return {
            "sector_leaders": leaders,
            "sector_laggards": laggards,
            "blackout_active": blackout,
            "blackout_reason": blackout_reason,
            "hours_to_next_macro": hours_to_macro,
        }

    def _check_blackout(self, calendar) -> tuple[bool, str, float]:
        from datetime import datetime
        import pytz
        et = pytz.timezone("US/Eastern")
        now = datetime.now(et)

        high_impact = ["CPI", "FOMC", "Federal Funds", "NFP", "Non-Farm", "Unemployment", "GDP", "PCE"]
        buffers = {"CPI": 2, "FOMC": 4, "Federal Funds": 4, "NFP": 2, "Non-Farm": 2, "GDP": 2, "PCE": 2}

        events = calendar if isinstance(calendar, list) else []
        for event in events:
            name = event.get("event", "")
            event_time_str = event.get("datetime") or event.get("date")
            if not event_time_str:
                continue
            for keyword in high_impact:
                if keyword.lower() in name.lower():
                    try:
                        from dateutil import parser as dp
                        event_time = dp.parse(event_time_str)
                        if event_time.tzinfo is None:
                            event_time = et.localize(event_time)
                        hours_away = (event_time - now).total_seconds() / 3600
                        buffer = buffers.get(keyword, 2)
                        if 0 <= hours_away <= buffer:
                            return True, f"{name} in {hours_away:.1f}h", hours_away
                    except Exception:
                        pass
        return False, "", 999


def evaluate_market_gate(mkt: MarketContextData, cfg: dict) -> tuple[bool, str]:
    """Returns (can_trade, reason). Missing/None values are treated as passing
    (a briefly unreachable free/MCP source never itself halts trading)."""
    checks = [
        (mkt.fear_greed_score >= cfg["market_filters"]["fear_greed"]["no_buy_below"],
         f"F&G too low: {mkt.fear_greed_score}/100 (min: {cfg['market_filters']['fear_greed']['no_buy_below']})"),
        (mkt.fear_greed_score <= cfg["market_filters"]["fear_greed"]["no_buy_above"],
         f"F&G too high: {mkt.fear_greed_score}/100 (max: {cfg['market_filters']['fear_greed']['no_buy_above']})"),
        (mkt.vix_level < cfg["market_filters"]["vix"]["no_trade_above"],
         f"VIX too high: {mkt.vix_level:.1f} (max: {cfg['market_filters']['vix']['no_trade_above']})"),
        (not mkt.blackout_active, f"Macro blackout: {mkt.blackout_reason}"),
        (not cfg["risk"]["kill_switch_triggered"], "Kill switch is ON"),
    ]
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "OK"
