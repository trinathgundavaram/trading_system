"""defeatbeta-api client (2026-07-22, Trinath: "unlimited free data source
research - what can be implemented for better data availability").

See mcp_clients/market_data.py's module docstring for why yfinance's
scraper-grade price-history endpoint has been the platform's #1 documented
failure source (mass OHLCV rate-limiting - 337 signals killed in one day,
per that docstring). defeatbeta-api (github.com/defeat-beta/defeatbeta-api)
is NOT a live scraper - it reads a public, anonymous Hugging Face dataset
(pre-built Parquet snapshots of Yahoo Finance data) via DuckDB, so there is
no per-request rate limit and no ban risk. The tradeoff: the dataset is
refreshed periodically (weekly, per the project's own docs), so the most
recent 1-7 daily bars can lag a live feed by that long. That's a real cost
for short-window indicators (RSI, recent MACD crosses) but a non-issue for
SMA200/long-trend calcs, and it's strictly better than the all-defaults
"stale_indicators" fallback this platform hits today when yfinance is
rate-limited and no paid provider key (Alpaca/Tiingo/TwelveData) is
configured. Positioned LAST in market_data.py's bars provider chain for
exactly this reason - keyed, closer-to-real-time providers always win when
they're configured and healthy.

Fully optional: `defeatbeta-api` is NOT in requirements.txt's hard installs
(it pulls in duckdb/openpyxl/huggingface-hub - a heavy chain for a fallback
of a fallback). Import is lazy and guarded; with the package not installed
this module is completely inert, same convention as every other optional
provider in mcp_clients/market_data.py ("no keys = inert" -> here, "no
package = inert").

No API key needed - it's a public dataset read. Requires outbound HTTPS to
huggingface.co; if that's blocked, available() reports unhealthy after the
first failure and the circuit breaker keeps this out of the hot path for
cooldown_seconds rather than retrying every ticker.

NOT verified against a live network call from where this was written (the
build sandbox's egress is allowlisted and does not include huggingface.co) -
the price()/ttm_eps()/ttm_pe()/shares() column names below were read
directly from the installed package's source
(defeatbeta_api/data/ticker.py's _query_data()/SQL templates), not from a
live response. Same honesty-note convention as mcp_clients/stock_scanner.py's
2026-07-22 remap: if get_daily_bars() comes back empty in production, check
the DataFrame's actual column names first via `Ticker('AAPL').price().columns`."""
import logging
import threading

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker

logger = logging.getLogger(__name__)

TTL_BARS = 12 * 3600           # dataset refreshes ~weekly - no value re-querying more than 2x/day
TTL_BARS_NEGATIVE = 900        # short negative-cache for unknown/delisted symbols
TTL_FUNDAMENTALS = 24 * 3600

_IMPORT_LOCK = threading.Lock()
_ticker_cls = None
_import_failed = False


def _get_ticker_cls():
    """Lazy, one-time import of defeatbeta_api's Ticker class - keeps the
    package fully optional (see module docstring)."""
    global _ticker_cls, _import_failed
    if _ticker_cls is not None or _import_failed:
        return _ticker_cls
    with _IMPORT_LOCK:
        if _ticker_cls is not None or _import_failed:
            return _ticker_cls
        try:
            from defeatbeta_api.data.ticker import Ticker  # noqa: local import, optional dep
            _ticker_cls = Ticker
        except Exception as e:
            _import_failed = True
            logger.info(
                f"defeatbeta-api not installed/importable ({e}) - this fallback "
                f"stays inert (pip install defeatbeta-api to enable it)")
    return _ticker_cls


class DefeatBetaProvider:
    """market_data.py MarketDataRouter-shaped provider (name/available()/
    bars_available()/get_daily_bars()) so it slots into the existing
    provider-chain pattern (see MarketDataRouter.get_daily_bars()) with no
    special-casing needed there."""

    name = "defeatbeta"

    def __init__(self):
        self.breaker = SourceCircuitBreaker("defeatbeta", fail_threshold=3, cooldown_seconds=1800)
        # `key` kept truthy-when-active for symmetry with FinanceQueryProvider's
        # keyless convention (MarketDataRouter.__init__'s active-provider log
        # line, and server.py's /api/sources "is this provider configured at
        # all" check, both key off `p.key`) - truthy only when the optional
        # package actually imported successfully, distinct from available()
        # which also folds in current breaker/cooldown state.
        self.key = "keyless" if _get_ticker_cls() is not None else ""

    def available(self) -> bool:
        return _get_ticker_cls() is not None and self.breaker.available()

    def bars_available(self) -> bool:
        """Router checks bars_available() when present (see AlpacaProvider's
        split quote/bars breakers) - defeatbeta has no such split, so this
        just mirrors available()."""
        return self.available()

    def get_daily_bars(self, ticker: str) -> list | None:
        """Same return shape as every other provider's get_daily_bars(): a
        list of {open, high, low, close, volume, date} dicts, oldest-first.
        Cached for TTL_BARS since the underlying dataset barely moves
        between scan cycles - no reason to re-hit DuckDB/Hugging Face more
        than a couple times a day per ticker."""
        cache_key = f"defeatbeta_bars_{ticker}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        TickerCls = _get_ticker_cls()
        if TickerCls is None:
            return None
        if not self.breaker.available():
            return None
        try:
            df = TickerCls(ticker).price()
            if df is None or df.empty:
                self.breaker.record(False, error="empty price() dataframe")
                cache.set(cache_key, [], TTL_BARS_NEGATIVE)
                return None
            df = df.sort_values("report_date").tail(400)
            rows = []
            for _, r in df.iterrows():
                close = r.get("close")
                if close is None:
                    continue
                rows.append({
                    "open": float(r.get("open") or close),
                    "high": float(r.get("high") or close),
                    "low": float(r.get("low") or close),
                    "close": float(close),
                    "volume": float(r.get("volume") or 0),
                    "date": str(r.get("report_date"))[:10],
                })
            ok = len(rows) >= 5
            self.breaker.record(ok, error="" if ok else "fewer than 5 usable rows")
            if ok:
                cache.set(cache_key, rows, TTL_BARS)
                return rows
            cache.set(cache_key, [], TTL_BARS_NEGATIVE)
            return None
        except Exception as e:
            logger.warning(f"defeatbeta bars {ticker}: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None

    def get_fundamentals(self, ticker: str) -> dict | None:
        """Bonus capability, not yet wired into engine/ticker_analyzer.py:
        TTM EPS/PE, shares outstanding - all free/unlimited via the same
        dataset. Exposed here for future use (e.g. a second opinion on
        yfinance's get_ticker_info fields when THAT'S rate-limited too)."""
        cache_key = f"defeatbeta_fund_{ticker}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached or None
        TickerCls = _get_ticker_cls()
        if TickerCls is None or not self.breaker.available():
            return None
        try:
            t = TickerCls(ticker)
            out = {}
            try:
                eps_df = t.ttm_eps()
                if eps_df is not None and not eps_df.empty:
                    out["ttm_eps"] = float(eps_df.iloc[-1].get("tailing_eps"))
            except Exception:
                pass
            try:
                pe_df = t.ttm_pe()
                if pe_df is not None and not pe_df.empty:
                    out["ttm_pe"] = float(pe_df.iloc[-1].get("ttm_pe"))
            except Exception:
                pass
            try:
                shares_df = t.shares()
                if shares_df is not None and not shares_df.empty:
                    out["shares_outstanding"] = float(shares_df.iloc[-1].get("shares_outstanding"))
            except Exception:
                pass
            ok = bool(out)
            self.breaker.record(ok, error="" if ok else "no fundamentals fields resolved")
            if ok:
                cache.set(cache_key, out, TTL_FUNDAMENTALS)
                return out
            return None
        except Exception as e:
            logger.warning(f"defeatbeta fundamentals {ticker}: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None


_default_provider = DefeatBetaProvider()


def get_price_history(ticker: str, period: str = "1y", interval: str = "1d") -> dict:
    """Drop-in-shaped replacement for mcp_clients/yfinance_mcp.py's
    YFinanceMCP.get_price_history() (returns {"data": [...]})  - lets the
    other direct yf.get_price_history() call sites (engine/market_breadth.py,
    engine/portfolio_risk.py, engine/screener.py) switch to this unlimited
    source with a one-line import change, without touching their downstream
    parsing. `period`/`interval` are accepted for signature compatibility but
    otherwise ignored - defeatbeta-api only has daily bars (no intraday
    granularity), and get_daily_bars() already returns the fullest history
    it has (up to 400 sessions); callers needing 5m/1h bars still need
    Alpaca or yfinance. NOT yet wired into any call site - see this module's
    docstring and market_data.py's DefeatBetaProvider for the one path
    that IS wired (the per-ticker bars provider chain)."""
    bars = _default_provider.get_daily_bars(ticker)
    if not bars:
        return {}
    return {"data": bars}
