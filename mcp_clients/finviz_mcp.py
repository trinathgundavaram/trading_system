"""finviz data - technical/analyst ratings, short float, earnings dates.

2026-07-15b (finviz-mcp-server replacement): the previous implementation
shelled out over stdio to a local `finviz-mcp-server` binary
(FINVIZ_MCP_PATH) that Trinath could no longer set up/build on this machine -
every call failed at the "server binary not found" check, so finviz had been
dark in production regardless of the circuit breaker/cache work done the
same day. Replaced with the `finviz` PyPI package
(https://github.com/mariostoev/finviz, `pip install finviz`) which scrapes
finviz.com's quote page directly in-process - no server to build/spawn, no
API key, no separate binary to keep in sync with this machine's Python.

Verified against live finviz.com (2026-07-15): `finviz.get_stock(ticker)`
returns the full snapshot table (P/E, Recom, Target Price, Short Float,
Insider/Inst Own, Earnings date, SMA20/50/200, RSI, etc). Two things it does
NOT reliably give us, confirmed by hitting real tickers before wiring this
in:
  1. Sector/Industry/Country - this package version's CSS selectors for
     finviz's "quote-links" block don't match the current page (came back
     empty on AAPL/TSLA/NVDA). Harmless here: ticker_analyzer.py's
     _parse_finviz already only overrides td.sector when finviz's value is
     truthy and not "N/A", so this silently falls through to the existing
     yfinance-sourced sector.
  2. A categorical "technical rating" (Buy/Strong Buy/etc) - finviz.com
     doesn't expose one via server-rendered HTML at all (that's a
     TradingView gauge widget, JS-rendered client-side); no scraper can pull
     it from the raw page. We synthesize an equivalent rating from the SMA20/
     50/200-vs-price and RSI(14) fields finviz DOES give us - see
     _derive_technical_rating(). This is a computed heuristic, not a
     finviz-native field; documented here so it isn't mistaken for one.

Hardening carried over unchanged from the no-buys-round-2 audit (still
correct for a scraper-backed source hit by up to 70 screener candidates
every 15-min cycle):
  1. 6-hour per-ticker cache - ratings/analyst consensus/short float move
     daily at most; re-scraping every 15-min cycle is pure ban-bait.
  2. Concurrency semaphore (2) - same treatment maverick.py already had.
  3. Circuit breaker - after 3 consecutive failures, skip finviz entirely
     for 15 min instead of burning a timeout on every ticker of every cycle.
  4. Hard per-call timeout (30s) via a worker thread - the finviz package's
     http_request_get() passes no `timeout=` to requests.get() at all, so a
     stalled connection would otherwise hang forever instead of failing
     fast into the breaker.

This module only covers PER-TICKER fundamentals (`finviz.get_stock()`).
Market-wide screening (`finviz.Screener`) is a separate concern with its own
row-parsing bug that had to be fixed first - see mcp_clients/finviz_screen.py
(wired into engine/screener.py's `finviz_screen` source, 2026-07-15c).
"""
import logging
import re
import threading

from engine.cache import cache
from mcp_clients.base import SourceCircuitBreaker

logger = logging.getLogger(__name__)

TTL_FINVIZ = 6 * 3600  # 6h - ratings/short-float/earnings-date churn is daily at most
CALL_TIMEOUT = 30  # seconds - see module docstring point 4

_FINVIZ_CONCURRENCY = threading.Semaphore(2)


def _call_with_timeout(fn, *args, timeout_seconds=CALL_TIMEOUT):
    """2026-07-17 hang forensics: this used to submit to a permanent,
    module-level `ThreadPoolExecutor(max_workers=2)`. Since finviz's
    http_request_get() passes no timeout to requests.get() at all, a single
    stalled scrape ties up one of those 2 workers FOREVER - future.result(
    timeout=...) only stops the caller from waiting, it doesn't kill the
    worker thread. A live py-spy dump caught exactly this: finviz-scrape
    worker threads stuck mid-call. With only 2 workers, two stalls in the
    process's lifetime (very plausible over a trading day) permanently
    wedge this source - every call after that queues behind dead workers
    and always times out, silently going dark until a full restart, no
    different from a hang except it doesn't show up as one.

    Fix: no shared pool. Every call gets its own fresh, throwaway daemon
    thread (same escape-valve pattern as storage/database.py's
    _open_with_timeout and mcp_clients/base.py's run_async) - a stalled
    call leaks at most one daemon thread (harmless, GC'd at process exit,
    doesn't block anything) instead of permanently consuming shared
    capacity every future call depends on."""
    result = {}
    done = threading.Event()

    def _target():
        try:
            result["value"] = fn(*args)
        except Exception as e:
            result["error"] = e
        finally:
            done.set()

    threading.Thread(target=_target, daemon=True, name="finviz-scrape").start()
    if not done.wait(timeout=timeout_seconds):
        raise TimeoutError(f"scrape timed out after {timeout_seconds}s")
    if "error" in result:
        raise result["error"]
    return result["value"]


def _to_float(raw) -> float:
    """finviz values are strings like '39.62', '0.96%', '318.76', or '-' for
    N/A. Strips %/commas and returns 0.0 for anything unparseable, matching
    the old MCP client's fallback-to-0 behavior for numeric fields."""
    if raw is None:
        return 0.0
    s = str(raw).strip().replace(",", "").replace("%", "")
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _derive_technical_rating(row: dict) -> str:
    """Buy/Hold/Sell string synthesized from SMA20/50/200 (% price is above/
    below each average) and RSI(14) - see module docstring point 2 on why
    this is computed rather than scraped. Trend score = how many of the 3
    SMAs price is currently above; RSI keeps a strongly-trending-but-
    overbought/oversold name out of the Buy/Sell buckets rather than
    doubling down on it."""
    sma20 = _to_float(row.get("SMA20"))
    sma50 = _to_float(row.get("SMA50"))
    sma200 = _to_float(row.get("SMA200"))
    rsi_raw = row.get("RSI (14)")
    rsi = _to_float(rsi_raw) if rsi_raw not in (None, "-") else None

    trend_score = sum(1 for v in (sma20, sma50, sma200) if v > 0)

    if rsi is not None and rsi >= 80:
        return "Sell"  # extended/overbought regardless of trend
    if rsi is not None and rsi <= 20:
        return "Buy"  # oversold bounce candidate regardless of trend

    if trend_score == 3 and (rsi is None or 45 <= rsi <= 75):
        return "Strong Buy"
    if trend_score >= 2:
        return "Buy"
    if trend_score == 0 and (rsi is None or rsi <= 40):
        return "Strong Sell"
    if trend_score <= 1:
        return "Sell"
    return "Hold"


def _derive_analyst_rating(row: dict) -> str:
    """finviz's 'Recom' field is analysts' average recommendation on the
    standard 1.0 (Strong Buy) - 5.0 (Strong Sell) scale. '-' means finviz has
    no analyst coverage for this ticker."""
    raw = row.get("Recom")
    if raw in (None, "-", ""):
        return "N/A"
    recom = _to_float(raw)
    if recom <= 0:
        return "N/A"
    if recom <= 1.5:
        return "Strong Buy"
    if recom <= 2.5:
        return "Buy"
    if recom <= 3.5:
        return "Hold"
    if recom <= 4.5:
        return "Sell"
    return "Strong Sell"


def _clean_earnings_date(raw: str) -> str:
    """finviz gives 'Jul 30 AMC' / 'Jul 30 BMO' (after-close/before-open
    suffix). ticker_analyzer.py's _parse_finviz parses this with
    strptime(..., '%b %d'), so strip the suffix down to 'Jul 30' rather than
    letting it hard-fail into the 999-days-to-earnings fallback."""
    if not raw or raw == "-":
        return "N/A"
    m = re.match(r"([A-Za-z]{3}\s+\d{1,2})", raw.strip())
    return m.group(1) if m else raw.strip()


class FinvizMCP:
    def __init__(self):
        self.breaker = SourceCircuitBreaker("finviz", fail_threshold=3, cooldown_seconds=900)

    def _scrape(self, ticker: str) -> dict:
        import finviz  # deferred import - keeps this optional dep out of the hot path if unused
        return finviz.get_stock(ticker)

    def get_fundamentals(self, ticker: str) -> dict:
        cached = cache.get(f"finviz_{ticker}")
        if cached is not None:
            return cached
        if not self.breaker.available():
            return {}

        row = None
        error = ""
        with _FINVIZ_CONCURRENCY:
            try:
                row = _call_with_timeout(self._scrape, ticker)
            except TimeoutError as e:
                error = str(e)
            except ImportError:
                error = ("`finviz` package not installed - run "
                         "`pip install finviz` (see requirements.txt)")
            except Exception as exc:
                msg = str(exc)[:160]
                lowered = msg.lower()
                hint = (" <- finviz.com is rate-limiting/blocking this IP; wait it out (6h cache "
                        "+ 2-call semaphore already minimize load)"
                        if any(m in lowered for m in ("429", "too many requests", "403", "forbidden"))
                        else "")
                error = f"{type(exc).__name__}: {msg}{hint}"

        ok = bool(row) and isinstance(row, dict) and error == ""
        self.breaker.record(ok, error=error)
        if not ok:
            return {}

        data = {
            "technical_rating": _derive_technical_rating(row),
            "analyst_rating": _derive_analyst_rating(row),
            "target_price": _to_float(row.get("Target Price")),
            "short_float": _to_float(row.get("Short Float")),
            "earnings_date": _clean_earnings_date(row.get("Earnings", "")),
            "pe_ratio": _to_float(row.get("P/E")),
            "eps_next_q": _to_float(row.get("EPS next Q")),
            "insider_own": _to_float(row.get("Insider Own")),
            "inst_own": _to_float(row.get("Inst Own")),
            "sector": row.get("Sector") or "N/A",
            "industry": row.get("Industry") or "N/A",
        }
        cache.set(f"finviz_{ticker}", data, TTL_FINVIZ)
        return data
