"""Multi-provider market-data layer (2026-07-15) - direct REST, no MCP.

WHY: yfinance (unofficial, scraper-grade) has been the single biggest source
of production failures in this platform: mass OHLCV rate-limiting (337
signals killed in one day), stale/one-sided bid-ask producing false
SPREAD_WIDE vetoes (205 signals), and silently lost news headlines. Per the
provider assessment: yfinance should be a non-critical fallback, never a
hard-veto source.

PROVIDERS (all key-gated - a provider activates only when its key is present
in the environment or the repo-root .env; with no keys at all this module is
completely inert and the yfinance path behaves exactly as before):

  Alpaca   (ALPACA_API_KEY + ALPACA_API_SECRET) - PRIMARY for quotes + bars.
           Free real-time IEX feed, generous rate limits, one snapshot call
           returns trade/quote/daily bar together. IEX-only (not consolidated
           NBBO) - fine for this system's spread sanity checks and scoring;
           it's dramatically better than Yahoo's often-stale fields.
  Finnhub  (FINNHUB_API_KEY) - quotes backup + COMPANY NEWS (fills the dead
           news-feed gap with real, dated headlines). Candles are premium on
           free tier, so not used for bars. ~60 calls/min free - rate-limited
           here to stay under it.
  Tiingo   (TIINGO_API_KEY) - bars fallback (EOD) + IEX last-quote backup.
           Modest free limits - used only when Alpaca is absent/down.
  TwelveData (TWELVEDATA_API_KEY) - last-resort bars/quote. Free tier is 8
           credits/min, unusable for continuous scanning - lowest priority,
           aggressive rate limit.
  Marketstack - assessed and REJECTED: 100 requests/month free is not
           usable for a scanner that touches dozens of tickers per cycle.

Priority: Alpaca -> Finnhub -> Tiingo -> TwelveData -> (caller falls back to
yfinance MCP). Every provider gets a SourceCircuitBreaker so a dead/limited
provider is skipped instantly rather than burning per-ticker timeouts.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from mcp_clients.base import SourceCircuitBreaker
from mcp_clients.defeatbeta_data import DefeatBetaProvider

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 8  # seconds - REST is fast or it isn't coming


def _load_dotenv():
    """Minimal .env loader (repo root) - nothing in this codebase loaded
    .env into the environment before this module; keys are set without
    overriding anything already exported."""
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"market_data: .env load failed: {e}")


_load_dotenv()


class _RateLimiter:
    """Simple min-interval limiter - enough to stay under free-tier
    per-minute caps without a full token bucket."""

    def __init__(self, min_interval_seconds: float):
        self.min_interval = min_interval_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.time()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.time()


class _HTTPError(RuntimeError):
    """RuntimeError with the HTTP status code attached (2026-07-21) so
    callers can tell a permanent, subscription-tier failure (402/403 - will
    never succeed on retry) apart from a transient one (429/5xx - will
    recover on its own), instead of treating every non-200 the same."""
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _get(url: str, headers: dict = None, params: dict = None):
    import requests
    r = requests.get(url, headers=headers or {}, params=params or {}, timeout=_REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise _HTTPError(r.status_code, f"HTTP {r.status_code}: {r.text[:120]}")
    return r.json()


class AlpacaProvider:
    name = "alpaca"

    def __init__(self):
        self.key = os.getenv("ALPACA_API_KEY", "")
        self.secret = os.getenv("ALPACA_API_SECRET", "")
        self.breaker = SourceCircuitBreaker("alpaca", 3, 300)  # quotes
        # 2026-07-21: bars and assets get their OWN breakers, same rationale
        # as the FMP split above - quotes/bars/assets are independently
        # reliable (a quote endpoint 400 on one bad symbol shouldn't stop
        # bars from serving every other ticker, and vice versa), but all
        # three shared one breaker so a run of failures in any one of them
        # tripped the router's is-alpaca-available() check for all three.
        self.bars_breaker = SourceCircuitBreaker("alpaca-bars", 3, 300)
        self.assets_breaker = SourceCircuitBreaker("alpaca-assets", 3, 300)
        # 0.30s = the 200/min free cap exactly (2026-07-16: was 0.35s/~170min;
        # this limiter is GLOBAL across the parallel ticker threads, so with
        # ~44 tickers x 3 calls each it alone put a ~46s serial floor under
        # every cycle - reclaiming the full cap shaves ~7s per cycle free).
        self.limiter = _RateLimiter(0.30)
        self._base = "https://data.alpaca.markets/v2"

    def available(self) -> bool:
        return bool(self.key and self.secret) and self.breaker.available()

    def bars_available(self) -> bool:
        return bool(self.key and self.secret) and self.bars_breaker.available()

    def assets_available(self) -> bool:
        return bool(self.key and self.secret) and self.assets_breaker.available()

    def _headers(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    @staticmethod
    def _to_alpaca_symbol(ticker: str) -> str:
        """Alpaca's REST API expects class-share tickers in DOT notation
        ('BRK.B', 'PBR.A') - this app's canonical ticker format is the
        hyphen convention other providers/the screener use ('BRK-B',
        'PBR-A'). Passing the hyphen form straight through 400s with
        'invalid symbol', and at volume that alone is enough to trip the
        breaker for every OTHER (valid) ticker in the same cycle too
        (2026-07-20 log: PBR-A 400'd 3x in a row and opened the alpaca
        breaker for 5min, right as other tickers needed it). Translate only
        for the Alpaca call - hyphen form stays canonical everywhere else."""
        return ticker.replace("-", ".") if "-" in ticker else ticker

    def get_quote(self, ticker: str) -> dict | None:
        """One snapshot call: latest trade + latest quote + today's bar.

        2026-07-22 (volume_ratio bug fix - Trinath: "not even one ticker
        crossed 45%"): day_volume is deliberately NOT populated from this
        snapshot. `feed: iex` is the free-tier feed - it only reflects
        trades that printed on the IEX exchange specifically, which is
        routinely ~1-3% of a stock's TOTAL consolidated-tape volume, not the
        real day volume. Confirmed in production: EVERY sampled ticker
        (including NFLX, ORCL - among the most liquid names on the market)
        showed 0.0x-0.1x of its average volume, all day, every cycle -
        impossible as a real reading, exactly consistent with comparing
        IEX-only same-day volume against yfinance's averageVolume (which IS
        a consolidated, all-exchanges figure). Comparing the two was an
        apples-to-oranges unit mismatch, not a reflection of real thin
        trading, and it was silently capping VOLUME_PA's rvol sub-score
        (up to 10 pts) near zero for every ticker, every cycle.
        ticker_analyzer.py's _parse_yfinance() already computes a correct,
        consolidated-tape volume_ratio from yfinance's own
        regularMarketVolume/averageVolume pair BEFORE this quote is applied;
        leaving day_volume out of this dict means that correct figure is
        never overwritten by the IEX-only one. Price/bid/ask are NOT
        affected by this fix - IEX's last trade price and NBBO-derived
        quote still track the real market closely enough for those fields
        (that's a precision question, not a unit-mismatch one)."""
        try:
            self.limiter.wait()
            d = _get(f"{self._base}/stocks/{self._to_alpaca_symbol(ticker)}/snapshot",
                     headers=self._headers(), params={"feed": "iex"})
            trade, quote = d.get("latestTrade") or {}, d.get("latestQuote") or {}
            daily = d.get("dailyBar") or {}
            out = {
                "price": trade.get("p") or daily.get("c"),
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
                # day_volume intentionally omitted - see docstring above.
                "prev_close": (d.get("prevDailyBar") or {}).get("c"),
                # quote_time preserved (2026-07-21, external review -
                # "validate staleness from the provider's market timestamp,
                # not from the time your code performed the request") -
                # Alpaca's own "t" on latestTrade/latestQuote is an RFC3339
                # timestamp of when that trade/quote actually happened,
                # independent of when this HTTP call ran. Prefers the trade
                # timestamp (matches the price this dict reports); falls
                # back to the quote timestamp if the trade one is missing.
                "quote_time": trade.get("t") or quote.get("t"),
            }
            ok = bool(out["price"])
            self.breaker.record(ok)
            return out if ok else None
        except Exception as e:
            logger.warning(f"alpaca quote {ticker}: {e}")
            self.breaker.record(False)
            return None

    def _bars(self, ticker: str, timeframe: str, start_days: int, limit: int) -> list | None:
        try:
            self.limiter.wait()
            start = (datetime.utcnow() - timedelta(days=start_days)).strftime("%Y-%m-%dT00:00:00Z")
            d = _get(f"{self._base}/stocks/{self._to_alpaca_symbol(ticker)}/bars", headers=self._headers(),
                     params={"timeframe": timeframe, "start": start, "limit": limit,
                             "adjustment": "split", "feed": "iex"})
            bars = d.get("bars") or []
            # "date" preserved (2026-07-21, external review - "prioritize
            # preserving the daily OHLCV date index all the way through
            # ticker_analyzer.py"): Alpaca's "t" is an RFC3339 timestamp
            # (e.g. "2026-07-15T04:00:00Z"); the first 10 chars are the
            # session date, which is all _calc_earnings_avwap() needs to
            # locate a real anchor bar instead of the old calendar-day
            # approximation.
            rows = [{"open": b["o"], "high": b["h"], "low": b["l"],
                     "close": b["c"], "volume": b["v"],
                     "date": (b.get("t") or "")[:10]} for b in bars]
            ok = len(rows) >= 5
            self.bars_breaker.record(ok)
            return rows if ok else None
        except Exception as e:
            logger.warning(f"alpaca bars {ticker}: {e}")
            self.bars_breaker.record(False)
            return None

    def get_daily_bars(self, ticker: str) -> list | None:
        return self._bars(ticker, "1Day", start_days=380, limit=400)

    def get_intraday_bars(self, ticker: str) -> list | None:
        return self._bars(ticker, "5Min", start_days=5, limit=500)

    def get_all_assets(self) -> list | None:
        """Every active, tradable US equity symbol (2026-07-15g, universe
        sweep) - Alpaca's assets endpoint is free with any key and returns
        the full ~10k-name active US-equity list in one call. Tries the
        paper-trading host first (paper keys are the recommended setup),
        then the live host. Filters to major exchanges and plain symbols
        (skips units/warrants/preferreds with ./- suffixes)."""
        for host in ("https://paper-api.alpaca.markets", "https://api.alpaca.markets"):
            try:
                self.limiter.wait()
                d = _get(f"{host}/v2/assets",
                         headers=self._headers(),
                         params={"status": "active", "asset_class": "us_equity"})
                assets = d if isinstance(d, list) else []
                symbols = [
                    a["symbol"] for a in assets
                    if a.get("tradable")
                    and a.get("exchange") in ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS")
                    and a.get("symbol", "").isalpha() and len(a.get("symbol", "")) <= 5
                ]
                if symbols:
                    self.assets_breaker.record(True)
                    return symbols
            except Exception as e:
                logger.warning(f"alpaca assets via {host}: {e}")
        self.assets_breaker.record(False)
        return None


class FinnhubProvider:
    name = "finnhub"

    def __init__(self):
        self.key = os.getenv("FINNHUB_API_KEY", "")
        self.breaker = SourceCircuitBreaker("finnhub", 3, 300)
        self.limiter = _RateLimiter(1.1)  # ~55/min, under the 60/min free cap

    def available(self) -> bool:
        return bool(self.key) and self.breaker.available()

    def get_quote(self, ticker: str) -> dict | None:
        """Last price only - Finnhub's free /quote has no bid/ask."""
        try:
            self.limiter.wait()
            d = _get("https://finnhub.io/api/v1/quote",
                     params={"symbol": ticker, "token": self.key})
            ok = bool(d.get("c"))
            self.breaker.record(ok)
            # quote_time preserved (2026-07-21, same review as Alpaca above) -
            # Finnhub's "t" is the unix-epoch-seconds timestamp of the quote
            # itself, not of this HTTP call.
            return {"price": d.get("c"), "prev_close": d.get("pc"),
                    "quote_time": d.get("t")} if ok else None
        except Exception as e:
            logger.warning(f"finnhub quote {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_recommendation(self, ticker: str) -> str | None:
        """Analyst consensus label from Finnhub's FREE /stock/recommendation
        endpoint (monthly Buy/Hold/Sell counts) - part of the no-Finviz-Elite
        fallback chain (2026-07-15d). Returns 'Strong Buy'/'Buy'/'Hold'/
        'Sell' or None. Cached 6h per ticker (analyst counts move monthly)."""
        from engine.cache import cache as _cache
        cached = _cache.get(f"finnhub_reco_{ticker}")
        if cached is not None:
            return cached or None  # "" caches a known-empty answer
        try:
            self.limiter.wait()
            d = _get("https://finnhub.io/api/v1/stock/recommendation",
                     params={"symbol": ticker, "token": self.key})
            rows = d if isinstance(d, list) else []
            if not rows:
                self.breaker.record(True)
                _cache.set(f"finnhub_reco_{ticker}", "", 6 * 3600)
                return None
            r = rows[0]  # newest month first
            sb, b = r.get("strongBuy", 0), r.get("buy", 0)
            h, s, ss = r.get("hold", 0), r.get("sell", 0), r.get("strongSell", 0)
            total = sb + b + h + s + ss
            if not total:
                label = None
            elif sb > (b + h + s + ss):
                label = "Strong Buy"
            elif (sb + b) > (h + s + ss):
                label = "Buy"
            elif (s + ss) > (sb + b + h):
                label = "Sell"
            else:
                label = "Hold"
            self.breaker.record(True)
            _cache.set(f"finnhub_reco_{ticker}", label or "", 6 * 3600)
            return label
        except Exception as e:
            logger.warning(f"finnhub recommendation {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_news(self, ticker: str, days: int = 4) -> list | None:
        """Real dated company headlines - fills the news feed the yfinance
        path kept losing. Returns newest-first titles."""
        try:
            self.limiter.wait()
            to = datetime.utcnow().strftime("%Y-%m-%d")
            frm = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
            d = _get("https://finnhub.io/api/v1/company-news",
                     params={"symbol": ticker, "from": frm, "to": to, "token": self.key})
            items = d if isinstance(d, list) else []
            items.sort(key=lambda x: x.get("datetime", 0), reverse=True)
            titles = [i.get("headline", "") for i in items if i.get("headline")][:10]
            self.breaker.record(True)  # empty news is a valid answer, not a failure
            return titles
        except Exception as e:
            logger.warning(f"finnhub news {ticker}: {e}")
            self.breaker.record(False)
            return None


class TiingoProvider:
    name = "tiingo"

    def __init__(self):
        self.key = os.getenv("TIINGO_API_KEY", "")
        self.breaker = SourceCircuitBreaker("tiingo", 3, 600)
        self.limiter = _RateLimiter(1.5)

    def available(self) -> bool:
        return bool(self.key) and self.breaker.available()

    def get_quote(self, ticker: str) -> dict | None:
        try:
            self.limiter.wait()
            d = _get(f"https://api.tiingo.com/iex/{ticker}", params={"token": self.key})
            row = d[0] if isinstance(d, list) and d else {}
            ok = bool(row.get("last") or row.get("tngoLast"))
            self.breaker.record(ok)
            if not ok:
                return None
            # quote_time preserved (2026-07-21, same review as Alpaca/Finnhub
            # above) - schema not verified against live output (Tiingo's IEX
            # top-of-book has carried "quoteTimestamp"/"timestamp" fields
            # historically), so this tries the common key names defensively;
            # worst case it's None and the caller falls back to the old
            # 0-age approximation exactly as before this pass.
            return {"price": row.get("last") or row.get("tngoLast"),
                    "bid": row.get("bidPrice"), "ask": row.get("askPrice"),
                    "prev_close": row.get("prevClose"),
                    "quote_time": row.get("quoteTimestamp") or row.get("timestamp")
                                  or row.get("lastSaleTimestamp")}
        except Exception as e:
            logger.warning(f"tiingo quote {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_daily_bars(self, ticker: str) -> list | None:
        try:
            self.limiter.wait()
            start = (datetime.utcnow() - timedelta(days=380)).strftime("%Y-%m-%d")
            d = _get(f"https://api.tiingo.com/tiingo/daily/{ticker}/prices",
                     params={"startDate": start, "token": self.key})
            # "date" preserved (2026-07-21, same review as Alpaca above) -
            # Tiingo's own "date" field (e.g. "2026-07-15T00:00:00.000Z")
            # was already in the raw row and simply wasn't carried into the
            # slimmed-down dict.
            rows = [{"open": r["adjOpen"], "high": r["adjHigh"], "low": r["adjLow"],
                     "close": r["adjClose"], "volume": r["adjVolume"],
                     "date": (r.get("date") or "")[:10]}
                    for r in (d if isinstance(d, list) else [])
                    if r.get("adjClose") is not None]
            ok = len(rows) >= 5
            self.breaker.record(ok)
            return rows if ok else None
        except Exception as e:
            logger.warning(f"tiingo bars {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_intraday_bars(self, ticker: str) -> list | None:
        return None  # intraday is a paid Tiingo product - don't pretend


class TwelveDataProvider:
    name = "twelvedata"

    def __init__(self):
        self.key = os.getenv("TWELVEDATA_API_KEY", "")
        self.breaker = SourceCircuitBreaker("twelvedata", 2, 900)
        # Free tier is 8 credits/min - this provider is a LAST resort, not a
        # scanning source. 9s interval keeps a slow trickle inside the cap.
        self.limiter = _RateLimiter(9.0)

    def available(self) -> bool:
        # Disabled (2026-07-17): source_health shows last_success_at=None -
        # this provider has never once returned usable data since being
        # configured, only ever recorded failures. It's the last item in
        # every fallback chain, so every call to it was pure wasted latency
        # (HTTP round trip + retry) with zero payoff. Flip back to
        # `bool(self.key) and self.breaker.available()` if a working
        # TWELVEDATA_API_KEY is ever confirmed live.
        return False

    def get_quote(self, ticker: str) -> dict | None:
        try:
            self.limiter.wait()
            d = _get("https://api.twelvedata.com/quote",
                     params={"symbol": ticker, "apikey": self.key})
            ok = "close" in d and d.get("close") not in (None, "")
            self.breaker.record(ok)
            # quote_time preserved (2026-07-21, same review as the other
            # providers above) - TwelveData's "timestamp" is unix-epoch
            # seconds. This provider is currently disabled (see available()
            # above) so this is dormant until re-enabled.
            return {"price": float(d["close"]),
                    "prev_close": float(d.get("previous_close") or 0) or None,
                    "quote_time": d.get("timestamp")} if ok else None
        except Exception as e:
            logger.warning(f"twelvedata quote {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_daily_bars(self, ticker: str) -> list | None:
        try:
            self.limiter.wait()
            d = _get("https://api.twelvedata.com/time_series",
                     params={"symbol": ticker, "interval": "1day",
                             "outputsize": 300, "apikey": self.key})
            vals = d.get("values") or []
            # "date" preserved (2026-07-21, same review as Alpaca/Tiingo
            # above) - TwelveData's "datetime" is already "YYYY-MM-DD" for
            # the 1day interval used here.
            rows = [{"open": float(v["open"]), "high": float(v["high"]),
                     "low": float(v["low"]), "close": float(v["close"]),
                     "volume": float(v.get("volume") or 0),
                     "date": v.get("datetime") or ""} for v in reversed(vals)]
            ok = len(rows) >= 5
            self.breaker.record(ok)
            return rows if ok else None
        except Exception as e:
            logger.warning(f"twelvedata bars {ticker}: {e}")
            self.breaker.record(False)
            return None

    def get_intraday_bars(self, ticker: str) -> list | None:
        return None


class AlphaVantageProvider:
    """Alpha Vantage (2026-07-15h) - free tier is ~25 requests/DAY, so this
    provider is deliberately restricted to two low-frequency, high-value
    calls and hard-caps itself at _DAILY_BUDGET calls/day:

      - TOP_GAINERS_LOSERS: one call returns ~60 tickers (top 20 gainers /
        losers / most active) - a whole discovery-source's worth of
        candidates for one request. Cached 4h (max ~2 calls/market day).
      - LISTING_STATUS: the full active US listing (CSV) - a universe-sweep
        seed that doesn't require Alpaca keys. Called at most weekly.

    Per-ticker AV endpoints (OVERVIEW, NEWS_SENTIMENT, ...) are deliberately
    NOT wired: at 25/day they'd exhaust the quota inside one scan cycle.

    2026-07-16 dedupe: this file accidentally contained TWO
    AlphaVantageProvider classes; the earlier one (get_listed_symbols +
    a "raw" movers key) was silently shadowed by this one and has been
    removed, along with engine/screener.py's dead references to it."""

    name = "alphavantage"
    _DAILY_BUDGET = 20

    def __init__(self):
        self.key = os.getenv("ALPHAVANTAGE_API_KEY", "")
        self.breaker = SourceCircuitBreaker("alphavantage", 2, 3600)
        self.limiter = _RateLimiter(15.0)
        self._calls_today = 0
        self._budget_day = ""

    def _budget_ok(self) -> bool:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._budget_day:
            self._budget_day, self._calls_today = today, 0
        if self._calls_today >= self._DAILY_BUDGET:
            logger.info("alphavantage: daily call budget reached - skipping until tomorrow")
            return False
        return True

    def available(self) -> bool:
        return bool(self.key) and self.breaker.available() and self._budget_ok()

    def get_top_movers(self) -> dict | None:
        """{'gainers': [...], 'most_active': [...]} - each entry
        {ticker, price, change_pct, volume}. Cached 4h."""
        from engine.cache import cache as _cache
        cached = _cache.get("av_top_movers")
        if cached is not None:
            return cached
        if not self.available():
            return None
        try:
            self.limiter.wait()
            self._calls_today += 1
            d = _get("https://www.alphavantage.co/query",
                     params={"function": "TOP_GAINERS_LOSERS", "apikey": self.key})
            if not isinstance(d, dict) or "top_gainers" not in d:
                # AV returns {"Note"/"Information": ...} when throttled
                self.breaker.record(False, error=str(d)[:150])
                return None

            def _rows(key):
                out = []
                for r in d.get(key, []) or []:
                    try:
                        out.append({
                            "ticker": r["ticker"],
                            "price": float(r.get("price", 0)),
                            "change_pct": float(str(r.get("change_percentage", "0")).rstrip("%")),
                            "volume": float(r.get("volume", 0)),
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
                return out
            result = {"gainers": _rows("top_gainers"),
                      "most_active": _rows("most_actively_traded")}
            self.breaker.record(True)
            _cache.set("av_top_movers", result, 4 * 3600)
            return result
        except Exception as e:
            logger.warning(f"alphavantage top movers: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None

    def get_listing_symbols(self) -> list | None:
        """Full active US listing via LISTING_STATUS (CSV). Caller throttles
        to at most weekly (see _screen_universe_sweep's refresh logic)."""
        if not self.available():
            return None
        try:
            import requests
            self.limiter.wait()
            self._calls_today += 1
            r = requests.get("https://www.alphavantage.co/query",
                             params={"function": "LISTING_STATUS", "apikey": self.key},
                             timeout=30)
            lines = r.text.strip().splitlines()
            if len(lines) < 2 or not lines[0].lower().startswith("symbol"):
                self.breaker.record(False, error=r.text[:150])
                return None
            symbols = []
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) >= 4:
                    sym, exch, atype = parts[0].strip(), parts[2].strip(), parts[3].strip()
                    if (atype == "Stock" and exch in ("NYSE", "NASDAQ", "AMEX", "NYSE ARCA", "BATS")
                            and sym.isalpha() and len(sym) <= 5):
                        symbols.append(sym)
            ok = len(symbols) > 100
            self.breaker.record(ok)
            return symbols if ok else None
        except Exception as e:
            logger.warning(f"alphavantage listing: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None


class FMPProvider:
    """Financial Modeling Prep (2026-07-15h) - free tier is ~250 req/day
    with a hard self-cap at _DAILY_BUDGET here. Wired for:

      - /stable/biggest-gainers + /stable/most-actives: a second independent
        movers lens for discovery (2 calls, cached 2h).
      - /stable/stock-list: the full tradable-symbol directory - a universe
        sweep seed that needs neither Alpaca nor Alpha Vantage (1 call,
        refreshed weekly at most via the universe_refresh throttle).
      - /stable/earnings, /stable/grades, /stable/analyst-estimates
        (2026-07-16, placeholder-fill pass): three per-ticker endpoints that
        turned out to be free-tier-accessible (verified live) after most of
        FMP's older v3/v4 per-ticker endpoints were retired as "legacy" in
        their Aug-2025 API migration. Each is 24h/12h-cached (see the method
        docstrings) so a small watchlist stays a tiny fraction of the daily
        budget even with the movers/stock-list calls above sharing it.
        Fills three of engine/ticker_data_adapter.py's PLACEHOLDER fields
        with real, dated, per-event data - see that file and
        engine/rules_catalog.py for what changed."""

    name = "fmp"
    _DAILY_BUDGET = 200
    _BASE = "https://financialmodelingprep.com/stable"

    def __init__(self):
        self.key = os.getenv("FMP_API_KEY", "")
        self.breaker = SourceCircuitBreaker("fmp", 3, 900)
        # 2026-07-21: grades/analyst-estimates get their OWN breaker,
        # separate from movers/stock-list/earnings above. Those two started
        # 402'ing ("not available under your current subscription") for
        # most symbols - a PERMANENT, plan-tier failure, not a transient
        # blip - and because every FMP method previously shared one breaker,
        # every 402 on grades/analyst-estimates was also tripping the
        # breaker for the still-healthy movers/stock-list/earnings calls,
        # taking down working features to punish a broken one.
        self.ratings_breaker = SourceCircuitBreaker("fmp-ratings", 3, 900)
        self.limiter = _RateLimiter(0.6)
        self._calls_today = 0
        self._budget_day = ""

    def _budget_ok(self) -> bool:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._budget_day:
            self._budget_day, self._calls_today = today, 0
        return self._calls_today < self._DAILY_BUDGET

    def available(self) -> bool:
        return bool(self.key) and self.breaker.available() and self._budget_ok()

    def _ratings_available(self) -> bool:
        return bool(self.key) and self.ratings_breaker.available() and self._budget_ok()

    def _call(self, endpoint: str, params: dict = None):
        self.limiter.wait()
        self._calls_today += 1
        p = dict(params or {})
        p["apikey"] = self.key
        return _get(f"{self._BASE}/{endpoint}", params=p)

    def get_movers(self) -> dict | None:
        """{'gainers': [...], 'most_active': [...]} - cached 2h."""
        from engine.cache import cache as _cache
        cached = _cache.get("fmp_movers")
        if cached is not None:
            return cached
        if not self.available():
            return None
        try:
            def _rows(endpoint):
                d = self._call(endpoint)
                out = []
                for r in (d if isinstance(d, list) else []):
                    try:
                        out.append({"ticker": r.get("symbol"),
                                    "price": float(r.get("price") or 0),
                                    "change_pct": float(r.get("changesPercentage") or r.get("changePercentage") or 0),
                                    "volume": float(r.get("volume") or 0)})
                    except (ValueError, TypeError):
                        continue
                return [r for r in out if r["ticker"]]
            result = {"gainers": _rows("biggest-gainers"),
                      "most_active": _rows("most-actives")}
            ok = bool(result["gainers"] or result["most_active"])
            self.breaker.record(ok, error="" if ok else "empty movers response")
            if ok:
                _cache.set("fmp_movers", result, 2 * 3600)
                return result
            return None
        except Exception as e:
            logger.warning(f"fmp movers: {e}")
            if getattr(e, "status_code", None) == 402:
                self.breaker.force_open(24 * 3600, reason="HTTP 402 - not entitled under current FMP plan")
            else:
                self.breaker.record(False, error=str(e)[:150])
            return None

    def get_stock_list(self) -> list | None:
        """Full symbol directory for the universe sweep. Caller throttles."""
        if not self.available():
            return None
        try:
            d = self._call("stock-list")
            symbols = [
                r["symbol"] for r in (d if isinstance(d, list) else [])
                if r.get("symbol", "").isalpha() and len(r.get("symbol", "")) <= 5
                and (r.get("exchangeShortName") or r.get("exchange") or "").upper()
                    in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
            ]
            ok = len(symbols) > 100
            self.breaker.record(ok, error="" if ok else "stock-list empty/unrecognized")
            return symbols if ok else None
        except Exception as e:
            logger.warning(f"fmp stock-list: {e}")
            if getattr(e, "status_code", None) == 402:
                self.breaker.force_open(24 * 3600, reason="HTTP 402 - not entitled under current FMP plan")
            else:
                self.breaker.record(False, error=str(e)[:150])
            return None

    def get_last_earnings_date(self, ticker: str) -> str | None:
        """Most recent PAST earnings report date (YYYY-MM-DD), verified
        against live AAPL data to be the real reported date, not a fiscal
        period-end - /stable/earnings returns past+future rows sorted
        newest-first; the first row with a non-null epsActual is the last
        real report. This is what engine/ticker_analyzer.py's
        _calc_swing_low_avwap() docstring flagged as missing ("no reliable
        source for the last (past) earnings date" - td.earnings_date is a
        forward-looking NEXT-earnings guess, unusable as a backward AVWAP
        anchor). 24h-cached (report dates don't move intraday). Returns None
        (not a cached "") on both "no data yet" and "call failed" - caller
        treats both the same (keep the placeholder) since there's no way to
        tell them apart from here."""
        from engine.cache import cache as _cache
        cache_key = f"fmp_last_earnings_{ticker}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached or None  # "" caches a known-empty answer
        if not self.available():
            return None
        try:
            rows = self._call("earnings", {"symbol": ticker})
            rows = rows if isinstance(rows, list) else []
            last_row = next((r for r in rows if r.get("epsActual") is not None), None)
            last_date = last_row.get("date") if last_row else None
            self.breaker.record(bool(rows), error="" if rows else "empty earnings response")
            _cache.set(cache_key, last_date or "", 24 * 3600)
            # Companion time-of-day hint (2026-07-21, external review -
            # "before-market-open earnings: anchor on that same session;
            # after-market-close: anchor on the next session"). FMP's
            # /stable/earnings sometimes carries a "time" field (bmo/amc);
            # cached alongside the date under its own key so
            # get_last_earnings_time_hint() below can read it without a
            # second API call. Not every plan/ticker returns this field -
            # absent means the anchor stays "approximate" (see
            # ticker_analyzer.py's _calc_earnings_avwap).
            time_hint = (last_row.get("time") or "").lower() if last_row else ""
            _cache.set(f"fmp_last_earnings_time_{ticker}", time_hint, 24 * 3600)
            return last_date
        except Exception as e:
            logger.warning(f"fmp earnings {ticker}: {e}")
            if getattr(e, "status_code", None) == 402:
                self.breaker.force_open(24 * 3600, reason="HTTP 402 - not entitled under current FMP plan")
            else:
                self.breaker.record(False, error=str(e)[:150])
            return None

    def get_last_earnings_time_hint(self, ticker: str) -> str:
        """Reads the "bmo"/"amc" hint cached by get_last_earnings_date()
        above - always call that first each cycle (ticker_analyzer.py does).
        Returns "" (not None) when nothing is cached yet or FMP didn't supply
        a time field - callers treat "" as "unknown", which
        _calc_earnings_avwap() uses to pick a conservative anchor and mark
        it approximate rather than guessing a session."""
        from engine.cache import cache as _cache
        return _cache.get(f"fmp_last_earnings_time_{ticker}") or ""

    def get_recent_downgrade(self, ticker: str, lookback_days: int = 30) -> bool | None:
        """True if any analyst rating action was 'downgrade' within the last
        lookback_days - real dated per-event data from /stable/grades
        (company, previous/new grade, action), verified live against AAPL
        (caught a real KeyBanc downgrade dated two days before this was
        written). Fills rules/swing_buy_rules.py's no_recent_downgrade,
        which used to default-True unconditionally because no
        downgrade-history source existed - see engine/ticker_data_adapter.py.

        Returns None (NOT False) when the call itself failed/unavailable, so
        the caller can fall back to the old neutral default instead of
        quietly treating 'no data' as 'confirmed no downgrade'. 12h-cached -
        shorter than earnings/estimates since a fresh downgrade is more
        time-sensitive than a report date or consensus number."""
        from datetime import datetime, timedelta
        from engine.cache import cache as _cache
        cache_key = f"fmp_downgrade_{ticker}_{lookback_days}"
        cached = _cache.get(cache_key)
        if isinstance(cached, dict):
            return cached["value"]
        if not self._ratings_available():
            return None
        try:
            rows = self._call("grades", {"symbol": ticker})
            rows = rows if isinstance(rows, list) else []
            cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            recent_downgrade = any(
                (r.get("action") or "").lower() == "downgrade" and (r.get("date") or "") >= cutoff
                for r in rows
            )
            self.ratings_breaker.record(bool(rows), error="" if rows else "empty grades response")
            _cache.set(cache_key, {"value": recent_downgrade}, 12 * 3600)
            return recent_downgrade
        except Exception as e:
            logger.warning(f"fmp grades {ticker}: {e}")
            if getattr(e, "status_code", None) == 402:
                # Not a transient failure - this plan/key isn't entitled to
                # this endpoint for this symbol. Retrying every 15 min
                # forever won't fix that; stand down for a day instead.
                self.ratings_breaker.force_open(
                    24 * 3600, reason="HTTP 402 - not entitled under current FMP plan")
            else:
                self.ratings_breaker.record(False, error=str(e)[:150])
            return None

    def get_consensus_eps(self, ticker: str) -> float | None:
        """Current consensus forward-FY EPS estimate (nearest future fiscal
        year's epsAvg) from /stable/analyst-estimates?period=annual - the
        only period value this key can access free (quarterly/period-less
        calls both 402'd in testing). This is a SNAPSHOT of today's
        consensus, not a revision history by itself - storage/database.py's
        estimate_snapshots table (added alongside this) records one reading
        per ticker per day so a later cycle can diff today's value against
        an older one to detect a genuine raise. 24h-cached (one real network
        call per ticker per day is also all the snapshot table needs)."""
        from engine.cache import cache as _cache
        cache_key = f"fmp_consensus_eps_{ticker}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached if cached else None  # 0.0 caches a known-empty answer
        if not self._ratings_available():
            return None
        try:
            rows = self._call("analyst-estimates", {"symbol": ticker, "period": "annual"})
            rows = rows if isinstance(rows, list) else []
            # Rows are future fiscal years ascending by date; nearest one is
            # the FY consensus this ticker's next earnings will be judged
            # against.
            eps = None
            for r in sorted(rows, key=lambda r: r.get("date") or ""):
                if r.get("epsAvg") is not None:
                    eps = float(r["epsAvg"])
                    break
            self.ratings_breaker.record(bool(rows), error="" if rows else "empty analyst-estimates response")
            _cache.set(cache_key, eps or 0.0, 24 * 3600)
            return eps
        except Exception as e:
            logger.warning(f"fmp analyst-estimates {ticker}: {e}")
            if getattr(e, "status_code", None) == 402:
                self.ratings_breaker.force_open(
                    24 * 3600, reason="HTTP 402 - not entitled under current FMP plan")
            else:
                self.ratings_breaker.record(False, error=str(e)[:150])
            return None


class FinanceQueryProvider:
    """FinanceQuery (2026-07-16, Akhil's ask) - open-source, KEYLESS Yahoo-
    backed HTTP API (github.com/Verdenroz/finance-query), hosted free at
    finance-query.com with a self-host option (Docker) if the shared host
    degrades. Every endpoint below was verified live before wiring.

    Why it's here: it replaces/relieves the QUOTA-limited sources -
      - movers (/v2/screeners/day_gainers + most_actives): a keyless,
        quota-free discovery lens that runs BEFORE FMP/AV movers in the
        screener, freeing FMP's 200/day budget for the per-ticker
        fundamentals (earnings/grades/estimates) only FMP can serve, and
        making discovery work with zero keys configured.
      - quotes/bars: last-resort fallback slotted ahead of TwelveData
        (whose 8-credits/min free tier is effectively unusable).
      - news (/v2/news/{symbol}): fallback after Finnhub.

    Why it's NOT primary for quotes/bars: it's the same Yahoo scraper-grade
    data that caused the original yfinance incidents (see module docstring)
    - server-side and cached, but the same reliability class. Alpaca stays
    primary; this never enters bars_capable(), so it only serves bars when
    a configured bars provider is down mid-cycle.

    Keyless = active by default. Set FINANCEQUERY_DISABLED=1 in .env to
    turn it off, or FINANCEQUERY_BASE_URL to point at a self-hosted
    instance. Circuit breaker + rate limiter same as every other provider."""

    name = "financequery"

    def __init__(self):
        self.disabled = os.getenv("FINANCEQUERY_DISABLED", "") in ("1", "true", "yes")
        self.base = (os.getenv("FINANCEQUERY_BASE_URL", "https://finance-query.com")).rstrip("/")
        # `key` kept truthy-when-active for symmetry with the other
        # providers' `p.key` checks in the router's active-list logging.
        self.key = "" if self.disabled else "keyless"
        self.breaker = SourceCircuitBreaker("financequery", 3, 600)
        self.limiter = _RateLimiter(0.5)

    def available(self) -> bool:
        return not self.disabled and self.breaker.available()

    def _call(self, path: str, params: dict = None):
        self.limiter.wait()
        return _get(f"{self.base}/v2/{path}", params=params or {})

    def get_quote(self, ticker: str) -> dict | None:
        """Same shape as the other providers' get_quote. Verified live:
        /v2/quote returns regularMarketPrice/bid/ask/regularMarketVolume/
        regularMarketPreviousClose (Yahoo field names)."""
        try:
            d = self._call(f"quote/{ticker}")
            out = {
                "price": d.get("regularMarketPrice"),
                "bid": d.get("bid"),
                "ask": d.get("ask"),
                "day_volume": d.get("regularMarketVolume"),
                "prev_close": d.get("regularMarketPreviousClose"),
                # quote_time preserved (2026-07-21, same review as the other
                # providers above) - "regularMarketTime" is the standard
                # Yahoo-shaped unix-epoch-seconds field this provider's own
                # docstring says it mirrors.
                "quote_time": d.get("regularMarketTime"),
            }
            ok = bool(out["price"])
            self.breaker.record(ok)
            return out if ok else None
        except Exception as e:
            logger.warning(f"financequery quote {ticker}: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None

    def get_daily_bars(self, ticker: str) -> list | None:
        """/v2/chart/{symbol}?range=1y&interval=1d -> {"candles": [...]}."""
        try:
            d = self._call(f"chart/{ticker}", {"range": "1y", "interval": "1d"})
            candles = (d or {}).get("candles") or []
            # "date" preserved (2026-07-21, same review as Alpaca/Tiingo/
            # TwelveData above) - schema for this field isn't verified
            # against live output (see this provider's own note on
            # yfmcp screener shapes elsewhere in this file), so this tries
            # the common key names defensively rather than assuming one;
            # worst case it's "" and _calc_earnings_avwap() falls back to
            # its calendar-day approximation exactly as before.
            rows = [{"open": c["open"], "high": c["high"], "low": c["low"],
                     "close": c["close"], "volume": c["volume"],
                     "date": str(c.get("date") or c.get("timestamp") or c.get("time") or "")[:10]}
                    for c in candles if c.get("close") is not None]
            ok = len(rows) >= 5
            self.breaker.record(ok)
            return rows if ok else None
        except Exception as e:
            logger.warning(f"financequery bars {ticker}: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None

    def get_intraday_bars(self, ticker: str) -> list | None:
        return None  # Alpaca-only capability, same as Tiingo/TwelveData

    def get_movers(self) -> dict | None:
        """Same shape as FMPProvider.get_movers() so the screener source
        functions stay interchangeable: {'gainers': [{ticker, price,
        change_pct, volume}], 'most_active': [...]}. Cached 2h. Verified
        live: /v2/screeners/{day_gainers,most_actives} return Yahoo quote
        rows under a 'quotes' key."""
        from engine.cache import cache as _cache
        cached = _cache.get("fq_movers")
        if cached is not None:
            return cached
        if not self.available():
            return None
        try:
            def _rows(screener_id):
                d = self._call(f"screeners/{screener_id}", {"count": 25})
                out = []
                for q in (d or {}).get("quotes") or []:
                    try:
                        out.append({
                            "ticker": q.get("symbol"),
                            "price": float(q.get("regularMarketPrice") or 0),
                            "change_pct": float(q.get("regularMarketChangePercent") or 0),
                            "volume": float(q.get("regularMarketVolume") or 0),
                        })
                    except (TypeError, ValueError):
                        continue
                return [r for r in out if r["ticker"]]
            result = {"gainers": _rows("day_gainers"),
                      "most_active": _rows("most_actives")}
            ok = bool(result["gainers"] or result["most_active"])
            self.breaker.record(ok, error="" if ok else "empty screener response")
            if ok:
                _cache.set("fq_movers", result, 2 * 3600)
                return result
            return None
        except Exception as e:
            logger.warning(f"financequery movers: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None

    def get_news(self, ticker: str) -> list | None:
        """Newest-first headline titles, same contract as Finnhub's
        get_news. Verified live: /v2/news/{symbol} returns a list of
        {title, source, time, sentiment, link}."""
        try:
            d = self._call(f"news/{ticker}")
            items = d if isinstance(d, list) else []
            titles = [i.get("title", "") for i in items if i.get("title")][:10]
            self.breaker.record(True)  # empty news is a valid answer
            return titles
        except Exception as e:
            logger.warning(f"financequery news {ticker}: {e}")
            self.breaker.record(False, error=str(e)[:150])
            return None


class MarketDataRouter:
    """Tries providers in priority order per capability; returns None when no
    configured provider can answer, in which case the caller uses the
    existing yfinance MCP path unchanged (fallback-only, per the provider
    assessment). Also reports which provider served what, for the
    data_coverage/provenance trail."""

    def __init__(self):
        self.alpaca = AlpacaProvider()
        self.finnhub = FinnhubProvider()
        self.tiingo = TiingoProvider()
        self.twelvedata = TwelveDataProvider()
        self.alphavantage = AlphaVantageProvider()
        self.fmp = FMPProvider()
        self.financequery = FinanceQueryProvider()
        # 2026-07-22 (Trinath: unlimited free data source research): dataset-
        # backed, keyless bars fallback - see mcp_clients/defeatbeta_data.py's
        # module docstring. Inert (available() False) if the optional
        # `defeatbeta-api` package isn't installed.
        self.defeatbeta = DefeatBetaProvider()
        active = [p.name for p in (self.alpaca, self.finnhub, self.tiingo,
                                    self.twelvedata, self.alphavantage, self.fmp,
                                    self.financequery)
                  if (p.key and getattr(p, "secret", True))]
        if self.defeatbeta.available():
            active.append(self.defeatbeta.name)
        if active:
            logger.info(f"market_data: active providers: {active} (yfinance demoted to fallback)")
        else:
            logger.info("market_data: no provider API keys configured - yfinance remains primary "
                        "(add ALPACA_API_KEY/ALPACA_API_SECRET or FINNHUB_API_KEY etc. to .env, "
                        "or `pip install defeatbeta-api` for a keyless bars fallback)")

    def any_configured(self) -> bool:
        # financequery counts: it's keyless, so quotes/news now work with
        # zero API keys configured (it slots LAST in each chain, so keyed
        # providers still win whenever they're present).
        return bool((self.alpaca.key and self.alpaca.secret) or self.finnhub.key
                    or self.tiingo.key or self.twelvedata.key
                    or self.financequery.available())

    def bars_capable(self) -> bool:
        """True when a bars-serving provider is configured AND currently
        healthy - used by ticker_analyzer to skip the yfinance price-history
        calls entirely. defeatbeta counts here (2026-07-22) even with ZERO
        API keys configured, since it's keyless - closes the gap where a
        fresh install with no provider keys at all fell straight through to
        yfinance's rate-limited scraper for every single ticker."""
        return (self.alpaca.bars_available() or self.tiingo.available()
                or self.twelvedata.available() or self.defeatbeta.available())

    def get_quote(self, ticker: str) -> tuple[dict, str] | None:
        # financequery ahead of twelvedata (2026-07-16): keyless and
        # quota-free vs TD's 8-credits/min free tier; TD stays as the very
        # last resort for anyone with a paid TD key.
        # Tiingo de-prioritized (2026-07-17): its free-tier hourly quota was
        # getting exhausted mid-cycle (dozens of HTTP 429s/day), and each 429
        # is a real network round-trip paid BEFORE its breaker opens - across
        # ~50 tickers/cycle that was adding up to real wall-clock time and
        # contributing to cycles overrunning the scan interval (see
        # scheduler.py's max_instances=1 cron skip warnings). Tried after the
        # cheaper/keyless sources now; still ahead of twelvedata since it's at
        # least free-tier-quota-based rather than 8-credits/min throttled.
        for p in (self.alpaca, self.finnhub, self.financequery, self.tiingo,
                  self.twelvedata):
            if p.available():
                q = p.get_quote(ticker)
                if q:
                    return q, p.name
        return None

    def get_daily_bars(self, ticker: str) -> tuple[list, str] | None:
        # Tiingo de-prioritized (2026-07-17) - see get_quote()'s comment above.
        # 2026-07-21: alpaca checks its dedicated bars_available() (not the
        # quote breaker) - see AlpacaProvider.__init__ for why they're split.
        # Other providers here don't have a bars/quote split, so they still
        # use the plain available().
        # defeatbeta LAST (2026-07-22): dataset-backed, no rate limit, but
        # refreshed periodically (can lag a live close by up to ~a week) -
        # every keyed/near-real-time provider above it wins whenever
        # configured and healthy. Still strictly better than this method
        # returning None (today's "give up, fall through to yfinance's
        # scraper" outcome for anyone with zero provider keys configured).
        for p in (self.alpaca, self.financequery, self.tiingo, self.twelvedata, self.defeatbeta):
            check = p.bars_available if hasattr(p, "bars_available") else p.available
            if check():
                b = p.get_daily_bars(ticker)
                if b:
                    return b, p.name
        return None

    def get_intraday_bars(self, ticker: str) -> tuple[list, str] | None:
        if self.alpaca.bars_available():
            b = self.alpaca.get_intraday_bars(ticker)
            if b:
                return b, self.alpaca.name
        return None

    def get_news(self, ticker: str) -> tuple[list, str] | None:
        if self.finnhub.available():
            n = self.finnhub.get_news(ticker)
            if n:  # empty-but-successful falls through to financequery
                return n, self.finnhub.name
        if self.financequery.available():
            n = self.financequery.get_news(ticker)
            if n is not None:
                return n, self.financequery.name
        return None

    def get_analyst_consensus(self, ticker: str) -> tuple[str, str] | None:
        """Free-tier analyst consensus (no-Finviz-Elite fallback chain)."""
        if self.finnhub.available():
            label = self.finnhub.get_recommendation(ticker)
            if label:
                return label, self.finnhub.name
        return None

    def get_last_earnings_date(self, ticker: str) -> str | None:
        """FMP-only capability (2026-07-16) - see FMPProvider.get_last_earnings_date."""
        if self.fmp.available():
            return self.fmp.get_last_earnings_date(ticker)
        return None

    def get_last_earnings_time_hint(self, ticker: str) -> str:
        """FMP-only capability (2026-07-21) - see FMPProvider.get_last_earnings_time_hint.
        Must be called AFTER get_last_earnings_date() in the same cycle (it
        reads that call's cache entry); returns "" otherwise, same as
        "unknown"."""
        if self.fmp.available():
            return self.fmp.get_last_earnings_time_hint(ticker)
        return ""

    def get_recent_downgrade(self, ticker: str, lookback_days: int = 30) -> bool | None:
        """FMP-only capability (2026-07-16) - see FMPProvider.get_recent_downgrade."""
        if self.fmp.available():
            return self.fmp.get_recent_downgrade(ticker, lookback_days)
        return None

    def get_consensus_eps(self, ticker: str) -> float | None:
        """FMP-only capability (2026-07-16) - see FMPProvider.get_consensus_eps."""
        if self.fmp.available():
            return self.fmp.get_consensus_eps(ticker)
        return None


router = MarketDataRouter()
