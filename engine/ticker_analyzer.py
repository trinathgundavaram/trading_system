"""Layer 2 - per-ticker data via MCPs (all parallel), technicals via pandas-ta.
Fully sync (the MCP client classes internally bridge into asyncio via
mcp.base.run_async), so this plugs straight into a ThreadPoolExecutor-based
scheduler with no special async handling anywhere above this layer."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import logging

import numpy as np

# pandas-ta (as of 0.3.14b) still imports `from numpy import NaN as npNaN`, which
# numpy>=1.24 removed (only lowercase `nan` remains). Patch it back before the
# `import pandas_ta` below - this is the standard, widely-used workaround.
if not hasattr(np, "NaN"):
    np.NaN = np.nan

import pandas as pd

# ── Which TA implementation is actually computing the indicators (§13) ──────
#
# engine/ta_fallback.py exposes the same method and column names as pandas_ta
# but is NOT bit-identical to it: the two produce different numbers from the
# same bars. pandas_ta has no wheel for Python < 3.12, so two machines on
# different Python versions silently compute different scores - and until now
# nothing recorded which one was active. Backtest summaries wrote
# `pandas_ta_used: true`; live runs wrote nothing at all.
#
# So: name the backend, log it, stamp it onto every signal row (see
# storage/version.py:ta_backend), and FAIL CLOSED by default. "Silently produce
# different numbers" is never the right default for a scoring engine. The
# opt-out exists for the case where you accept the divergence deliberately -
# and having to type TP_REQUIRE_REFERENCE_TA=0 is the point.
import os as _os

try:
    import pandas_ta as ta  # noqa: F401 - registers the real .ta accessor on DataFrames
    TA_BACKEND = f"pandas_ta {getattr(ta, '__version__', 'unknown')}"
    TA_IS_REFERENCE = True
except Exception as _e:  # pandas-ta's PyPI releases are currently broken for many
    # Python versions (old pin removed from PyPI; newest prerelease needs 3.12+
    # f-strings). See engine/ta_fallback.py.
    import engine.ta_fallback  # noqa: F401 - registers the fallback .ta accessor
    TA_BACKEND = "ta_fallback (hand-rolled)"
    TA_IS_REFERENCE = False
    logging.getLogger(__name__).warning(
        f"TA backend: {TA_BACKEND} - pandas_ta unavailable ({_e})"
    )
    if _os.getenv("TP_REQUIRE_REFERENCE_TA", "1") == "1":
        raise RuntimeError(
            "pandas_ta unavailable - refusing to start.\n"
            "  The fallback engine (engine/ta_fallback.py) is not bit-identical to\n"
            "  pandas_ta, so scores computed here are not comparable with the\n"
            "  backtest or with any prior live session. Every threshold in\n"
            "  config.yaml was derived on the reference backend.\n"
            "  Fix: run on Python 3.12+ so the pandas-ta wheel installs, or set\n"
            "  TP_REQUIRE_REFERENCE_TA=0 to accept the divergence deliberately.\n"
            f"  Underlying import error: {_e}"
        ) from _e
else:
    logging.getLogger(__name__).info(f"TA backend: {TA_BACKEND}")

from engine.cache import cache, TTL_TICKER, TTL_TICKER_LITE
from mcp_clients.yfinance_mcp import YFinanceMCP
from mcp_clients.maverick import MaverickMCP
from mcp_clients.finviz_mcp import FinvizMCP
from mcp_clients.stock_scanner import StockScannerMCP
from mcp_clients.edgar_data import EdgarClient


@dataclass
class TickerData:
    ticker: str
    company_name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    volume: int = 0
    volume_ratio: float = 1.0
    bid: float = 0.0
    ask: float = 0.0
    # Real quote age (2026-07-21, external review - "STALE_QUOTE never fires
    # today since quotes are always freshly fetched. Freshly fetched does
    # not always mean fresh market data... validate staleness from the
    # provider's market timestamp"). 0.0 by default - same value the old
    # hardcoded-0 behavior always produced - but now genuinely measured
    # whenever the winning quote provider supplied a market timestamp (see
    # mcp_clients/market_data.py's per-provider "quote_time" field and
    # _parse_quote_time() below). quote_age_is_measured distinguishes a real
    # "checked and it's fresh" 0.0 from the old "never checked" 0.0 - see
    # rules/hard_vetoes.py's STALE_QUOTE veto.
    quote_age_minutes: float = 0.0
    quote_age_is_measured: bool = False
    # Technical indicators
    rsi: float = 50.0
    stoch_k: float = 50.0
    stoch_d: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    macd_crossover: bool = False
    macd_crossover_direction: str = "none"
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_pct: float = 0.5
    sma_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    ema_9: float = 0.0
    ema_21: float = 0.0
    vwap: float = 0.0
    atr: float = 0.0
    obv_trend: str = "flat"
    support_levels: list = field(default_factory=list)
    resistance_levels: list = field(default_factory=list)
    # ADX/+DI/-DI, CMF, Donchian, swing-low AVWAP - REAL as of this session,
    # computed from the same daily OHLCV bars as everything else (see
    # _calc_indicators / _calc_swing_low_avwap below). These used to be
    # PLACEHOLDER(0.0) in engine/ticker_data_adapter.py.
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0
    cmf: float = 0.0
    donchian_20d_high: float = 0.0
    recent_swing_low: float = 0.0
    avwap_swing_low: float = 0.0
    # Momentum persistence - REAL, consecutive trailing days MACD histogram
    # has stayed positive (0 if it's negative today). "MACD positive for 12
    # days" vs "1 day" is meaningfully different conviction even though both
    # currently satisfy the plain macd_hist>0 check - see
    # rules/swing_buy_rules.py's MOMENTUM bucket.
    macd_positive_days: int = 0
    # Volatility compression/expansion (TTM Squeeze + NR7/NR4 + inside day) -
    # see _calc_indicators for the exact definitions. All four are REAL,
    # computed from the same daily OHLCV bars already fetched for every other
    # indicator - no new data source needed.
    squeeze_active: bool = False   # squeeze fired: compressed in recent bars, released now
    is_nr7: bool = False
    is_nr4: bool = False
    is_inside_day: bool = False
    # Accumulation signals (2026-07-15, zero-trades audit) - all REAL,
    # computed from the same daily OHLCV bars in _calc_indicators. Detect
    # institutional accumulation building BEFORE a breakout:
    obv_new_high_20d: bool = False      # OBV at/above its 20-bar high today
    obv_divergence: bool = False        # OBV 20-bar high while price is NOT at its own 20-bar high (quiet accumulation)
    dollar_vol_ratio_20_50: float = 1.0 # 20d avg dollar volume / 50d avg dollar volume (>1.15 = liquidity building)
    accumulation_days_10: int = 0       # of last 10 bars: closed up AND volume > 20d avg
    return_1m_pct: float = 0.0          # ticker's own ~21-bar % return (for RS-vs-SPY in the adapter)
    # Data provenance (2026-07-15): which provider served each capability
    # this cycle, e.g. {"quote": "alpaca", "daily_bars": "alpaca",
    # "intraday_bars": "alpaca", "news": "finnhub"} - anything absent came
    # from the yfinance MCP fallback. Surfaced in data_coverage.
    data_sources: dict = field(default_factory=dict)
    # True when the Maverick MCP actually returned any payload this cycle -
    # distinguishes "maverick says not bullish" (FALSE evidence) from
    # "maverick unreachable" (UNKNOWN - feeds bucket-availability logic).
    maverick_data_present: bool = False
    # True weekly-resample trend flags (2026-07-15, external review) - None
    # means "not enough weekly history"; the adapter then falls back to the
    # old daily-proxy approximation and labels it as such.
    weekly_above_sma20: bool = None
    weekly_above_sma50: bool = None
    # Fundamentals
    pe_ratio: float = 0.0
    eps: float = 0.0
    beta: float = 1.0
    market_cap: float = 0.0
    w52_high: float = 0.0
    w52_low: float = 0.0
    avg_volume: int = 0
    # External data
    technical_rating: str = "N/A"
    # tradingview_rating (2026-07-22, Trinath: "source finviz's data
    # elsewhere"): a REAL third-party technical gauge from stock-scanner
    # MCP's tradingview_technicals tool (Buy/Strong Buy/Hold/Sell/Strong
    # Sell), independent of finviz's own SMA/RSI-derived heuristic above -
    # see ticker_analyzer.py's _parse_scanner() and
    # mcp_clients/stock_scanner.py's module docstring for exactly how this
    # is wired and why it's unverified-shape/best-effort rather than a
    # confirmed-live source. Preferred over technical_rating when present
    # (rules/swing_buy_rules.py's EXTERNAL bucket) since it's a genuine
    # external opinion rather than a rating synthesized from indicators this
    # engine already scores elsewhere.
    tradingview_rating: str = "N/A"
    analyst_rating: str = "N/A"
    analyst_target: float = 0.0
    short_float: float = 0.0
    earnings_date: str = "N/A"
    days_to_earnings: int = 999
    sector: str = "N/A"
    # §18: finer-grained than sector, and the difference matters for
    # concentration - "Technology" covers both a semiconductor foundry and a
    # payments processor, which do not move together, while
    # "Semiconductors" is close to a theme on its own. Populated
    # opportunistically from whichever of yfinance/finviz answered; stays
    # "N/A" when neither did, which portfolio_risk treats as unclassified
    # rather than as a match.
    industry: str = "N/A"
    # REAL - yfinance's own asset-class classification ("EQUITY", "ETF",
    # "MUTUALFUND", "INDEX", ...). Used by rules/swing_buy_rules.py to pick
    # between the stock and ETF bucket-weight profiles - see that module's
    # _detect_asset_class(). Not inferred/guessed - straight from yfinance's
    # info dict, same trust level as td.sector/td.beta above.
    quote_type: str = "EQUITY"
    # Sentiment
    news_sentiment_score: float = 0.5
    news_headlines: list = field(default_factory=list)
    maverick_sentiment: float = 0.5
    # Options
    options_put_call_ratio: float = 1.0
    implied_volatility: float = 0.0
    max_pain: float = 0.0
    # Insider
    insider_net_direction: str = "neutral"
    insider_buys_30d: int = 0
    insider_sells_30d: int = 0
    # unusual_options_bullish (2026-07-22, Trinath: "remove any capped API if
    # possible and see if it can be sourced elsewhere"): stock-scanner MCP's
    # options_unusual_activity tool - a real, already-connected source,
    # replacing the previous permanent placeholder (the literal intended
    # source, github.com/erikmaday/unusual-whales-mcp, needs a paid API key
    # never configured here). None means "not available this cycle" (breaker
    # open / tool didn't respond / shape didn't match - see _parse_scanner),
    # distinct from a confirmed False, same None-means-unknown convention as
    # recent_downgrade/estimate_raised below.
    unusual_options_bullish: bool = None
    # PLACEHOLDER-FILL PASS (2026-07-16) - real per-ticker data from FMP's
    # free-tier /stable endpoints (mcp_clients/market_data.py's
    # FMPProvider), replacing three of engine/ticker_data_adapter.py's
    # PLACEHOLDER defaults - see that file and engine/rules_catalog.py.
    # recent_downgrade/estimate_raised are True/False/None - None means
    # "unavailable this cycle" and the adapter treats it as False (no
    # credit), never as a silent True, so a data outage can't manufacture
    # bullish evidence the way the old default-True no_recent_downgrade did.
    last_earnings_date: str = ""    # real PAST report date (YYYY-MM-DD), FMP /stable/earnings
    last_earnings_time_hint: str = ""  # "bmo"/"amc"/"" from FMP - see _calc_earnings_avwap
    avwap_earnings: float = 0.0     # anchored VWAP from last_earnings_date - see _calc_earnings_avwap
    # True unless a real trading-session date index was available AND either
    # a confirmed "bmo" time hint or a confirmed "amc"-convention next-session
    # match was used to place the anchor bar (2026-07-21, external review -
    # "mark anchor timing as approximate, use a documented default, and lower
    # data confidence" when the report time is unknown). Surfaced in
    # ticker_data_adapter.py so downstream confidence/logging can see it.
    earnings_avwap_anchor_approximate: bool = True
    # Full anchor telemetry (2026-07-21, external review round 2 - "log
    # earnings_avwap_anchor_date, anchor_mode, and anchor_confidence"), on
    # top of the plain approximate/not-approximate flag above:
    #   anchor_mode: "bmo_exact" / "amc_exact" / "unknown_hint_approx"
    #     (real date index, no confirmed bmo/amc hint) /
    #     "calendar_fallback_approx" (old 5/7-ratio path, no date index at
    #     all this cycle) / "unset" (no earnings date this cycle).
    #   anchor_confidence: "high" (bmo_exact/amc_exact) / "low" (either
    #     approx mode) / "none" (unset).
    #   anchor_date: the actual anchor bar's calendar date (YYYY-MM-DD) when
    #     a real date index was used; "" for the calendar-fallback/unset
    #     paths, where there's no per-bar date to report.
    earnings_avwap_anchor_mode: str = "unset"
    earnings_avwap_anchor_confidence: str = "none"
    earnings_avwap_anchor_date: str = ""
    recent_downgrade: bool = None   # FMP /stable/grades, any downgrade action in the last 30d
    consensus_eps: float = 0.0      # forward-FY consensus EPS snapshot, FMP /stable/analyst-estimates
    estimate_raised: bool = None    # consensus_eps vs. a stored prior snapshot - see storage/database.py
    # Warm-up/measurement metadata for estimate_raised above (2026-07-21,
    # external review) - see storage/database.py's
    # check_and_record_estimate_snapshot() docstring for the exact shape
    # (status/score_effect/data_availability/observed_eps/prior_eps/
    # pct_change/source/snapshot_age_days/analyst_count_change). Empty dict
    # when estimate_raised was never computed this cycle (e.g. lite calls,
    # or consensus_eps <= 0 - see the call site below).
    estimate_raised_detail: dict = field(default_factory=dict)
    # Misc
    data_quality: str = "complete"
    missing_sources: list = field(default_factory=list)
    # Data Provenance Circuit Breaker: which of the CORE indicators (RSI,
    # MACD, TREND, VWAP) silently fell back to a default value this cycle
    # because the real calculation failed or its input data was missing -
    # unlike data_quality/missing_sources above (which only tracks whole
    # MCP-SOURCE availability), this tracks per-INDICATOR staleness even
    # when the source call itself succeeded but the specific calc didn't
    # (e.g. yfinance returned data, but too few daily bars for a real SMA,
    # or intraday data was missing so VWAP stayed at its 0.0 default).
    # Populated in _calc_indicators(). See rules/hard_vetoes.py's veto #16
    # ("STALE_DATA_CIRCUIT_BREAKER") which counts len(stale_indicators) (+
    # breadth staleness from market_data) against a configurable threshold.
    stale_indicators: list = field(default_factory=list)


class TickerAnalyzer:
    def __init__(self):
        self.yf = YFinanceMCP()
        self.maverick = MaverickMCP()
        self.finviz = FinvizMCP()
        self.scanner = StockScannerMCP()
        # 2026-07-22 (Trinath: unlimited free data source research) - direct
        # SEC EDGAR client, no npx subprocess middleman. See
        # mcp_clients/edgar_data.py's module docstring for why: stock-scanner's
        # own edgar_insider_trades tool has a documented history of drifting/
        # unverified shapes. Preferred ahead of it below when it resolves.
        self.edgar = EdgarClient()

    def analyze(self, ticker: str, market_ctx, cfg: dict = None, lite: bool = False) -> TickerData:
        """cfg is optional (defaults to None, same as before this param was
        added) so every existing caller keeps working unchanged - when a
        caller DOES pass cfg (scheduler.py, engine/position_management.py),
        tickers on config.yaml's asset_profiles.etf_tickers override list
        skip the yfinance holders/financials calls, which Yahoo doesn't have
        data for on ETFs anyway (see mcp_clients/yfinance_mcp.py's
        get_all() docstring - this was producing a guaranteed 404/ERROR log
        line every cycle for SPY specifically, since SPY is always analyzed
        for the regime engine regardless of watchlist)."""
        td = TickerData(ticker=ticker)

        # lite (2026-07-15, cycle-runtime fix): bars/quote/info only - no
        # maverick (5 calls) / finviz (1) / scanner (4) / news / options.
        # Used for screener candidates' first-pass score; a candidate near
        # the buy bar is re-analyzed with lite=False (see scheduler.py's
        # phase-2 promotion). Separate cache keys so a lite result can never
        # masquerade as a full one.
        cache_key = f"ticker_{ticker}_lite" if lite else f"ticker_{ticker}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        if not lite:
            # a fresh FULL result also satisfies future lite requests
            pass

        # 2026-07-17 (cycle-overrun fix): yfinance_get_financials/get_holders
        # were previously only skipped for explicit ETF overrides - but
        # mcp_clients/yfinance_mcp.py's own _get_all() docstring confirms
        # neither field is read anywhere downstream (verified: nothing in
        # this codebase does data.get("financials") or data.get("holders")).
        # get_financials was also the single biggest source of the hard
        # 30-40s MCP subprocess hangs that were overrunning the 5-min
        # day-trade scan interval (9-14 hits/day in scheduler.log). Skipping
        # it for EVERY ticker, not just ETFs, removes a guaranteed-wasted,
        # frequently-hanging round trip with zero functional loss.
        skip_hf = True
        # Option chain is display-only (put/call in the analysis prompt) -
        # skip it for non-watchlist screener candidates to save one MCP
        # subprocess spawn per candidate per cycle (2026-07-15).
        watchlist = {t.upper() for t in ((cfg or {}).get("watchlist", []) or [])}
        skip_options = bool(watchlist) and ticker.upper() not in watchlist

        # Provider-first data (2026-07-15, see mcp_clients/market_data.py):
        # when Alpaca/Tiingo/TwelveData keys are configured and healthy,
        # OHLCV comes from a real market-data API and the yfinance
        # price-history calls (the platform's #1 failure source) are skipped
        # entirely. yfinance keeps supplying info/fundamentals/news-fallback.
        from mcp_clients.market_data import router as md_router
        use_provider_bars = md_router.bars_capable()

        # 2026-07-16 (placeholder-fill pass): three more per-ticker FMP calls
        # (earnings date / downgrade check / consensus EPS), gated `if not
        # lite` same as maverick/finviz/scanner - enrichment signals, not
        # needed for the fast screener first-pass. bumped max_workers 6->9
        # since up to 9 futures can now be in flight for a full (non-lite)
        # analysis with every provider configured.
        # 2026-07-17 (hang-forensics audit, defense-in-depth): every branch
        # submitted here already routes through mcp_clients/base.py's
        # run_async() (hard 40s ceiling) or has its own timeout (maverick's
        # semaphore, market_data.py's requests timeouts, finviz's
        # _call_with_timeout), so none of these SHOULD be able to hang
        # forever today. But `with ThreadPoolExecutor() as ex:` + bare
        # `.result()` (no timeout=) is exactly the pattern that turned out
        # to be an unbounded hang in engine/market_context.py and
        # mcp_clients/base.py's OWN prior version (see those files'
        # docstrings) - `Executor.__exit__` calls shutdown(wait=True)
        # unconditionally, so if any future ever DOES fail to respect its
        # inner timeout (a new source added later, a library update, a
        # nested cancellation swallow), this would silently re-hang the
        # whole ticker-analysis worker with no ceiling at all. Bounding
        # every result() here and using shutdown(wait=False) means a future
        # bug in one data source degrades to "this field is missing for
        # this ticker" instead of "the whole cycle is stuck again."
        _FUT_TIMEOUT = 45  # seconds - just above run_async's own 40s ceiling
        ex = ThreadPoolExecutor(max_workers=10)
        yf_future = ex.submit(self.yf.get_all, ticker, skip_hf,
                              skip_options or lite, use_provider_bars)
        maverick_future = ex.submit(self.maverick.get_all, ticker) if not lite else None
        finviz_future = ex.submit(self.finviz.get_fundamentals, ticker) if not lite else None
        scanner_future = ex.submit(self.scanner.get_ticker_data, ticker) if not lite else None
        # 2026-07-22: direct SEC EDGAR Form 4 fetch - see self.edgar's
        # instantiation comment above. Full (non-lite) analysis only, same
        # gating as the other enrichment sources on this list.
        edgar_future = ex.submit(self.edgar.get_insider_transactions, ticker) if not lite else None
        bars_future = ex.submit(md_router.get_daily_bars, ticker) if use_provider_bars else None
        ibars_future = ex.submit(md_router.get_intraday_bars, ticker) if use_provider_bars else None
        quote_future = ex.submit(md_router.get_quote, ticker) if md_router.any_configured() else None
        news_future = (ex.submit(md_router.get_news, ticker)
                       if md_router.any_configured() and not lite else None)
        earnings_date_future = ex.submit(md_router.get_last_earnings_date, ticker) if not lite else None
        downgrade_future = ex.submit(md_router.get_recent_downgrade, ticker) if not lite else None
        consensus_eps_future = ex.submit(md_router.get_consensus_eps, ticker) if not lite else None

        def _safe(future, name, default=None):
            if future is None:
                return default
            try:
                return future.result(timeout=_FUT_TIMEOUT)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"{ticker}: {name} didn't resolve within {_FUT_TIMEOUT}s ({e}) - skipping it for this cycle")
                return default

        yf_data = _safe(yf_future, "yfinance", {}) or {}
        maverick_data = _safe(maverick_future, "maverick", {}) or {}
        finviz_data = _safe(finviz_future, "finviz", {}) or {}
        scanner_data = _safe(scanner_future, "stock-scanner", {}) or {}
        edgar_insider = _safe(edgar_future, "edgar_insider", None)
        provider_bars = _safe(bars_future, "provider_bars")
        provider_ibars = _safe(ibars_future, "provider_intraday_bars")
        provider_quote = _safe(quote_future, "provider_quote")
        provider_news = _safe(news_future, "provider_news")
        last_earnings_date = _safe(earnings_date_future, "last_earnings_date")
        # Read synchronously (not its own future): get_last_earnings_time_hint
        # just reads the cache entry get_last_earnings_date() above already
        # populated in the same call, so there's nothing to gain from another
        # thread/round trip - see FMPProvider.get_last_earnings_time_hint.
        last_earnings_time_hint = (
            md_router.get_last_earnings_time_hint(ticker) if last_earnings_date else ""
        )
        recent_downgrade = _safe(downgrade_future, "recent_downgrade")
        consensus_eps = _safe(consensus_eps_future, "consensus_eps")
        ex.shutdown(wait=False)

        td.last_earnings_date = last_earnings_date or ""
        td.last_earnings_time_hint = last_earnings_time_hint or ""
        td.recent_downgrade = recent_downgrade
        td.consensus_eps = consensus_eps or 0.0
        # estimate_raised needs a stored PRIOR reading to diff against (see
        # storage/database.py's check_and_record_estimate_snapshot) - only
        # call it when this cycle actually got a real consensus_eps (never
        # for lite calls, where consensus_eps_future is None and
        # td.consensus_eps stays 0.0), so a lite/no-key cycle never writes a
        # bogus 0.0 snapshot that would poison the next real comparison.
        if td.consensus_eps > 0:
            try:
                from storage.database import Database
                td.estimate_raised, td.estimate_raised_detail = (
                    Database().check_and_record_estimate_snapshot(ticker, td.consensus_eps))
            except Exception as e:
                logging.getLogger(__name__).warning(f"{ticker}: estimate snapshot error: {e}")

        # yfinance health for the Monitor tab's Data Sources panel (it has
        # no circuit breaker of its own - it's the core fallback). Success =
        # we got an info payload or bars this call. Throttled to one write
        # per 60s to keep DB churn negligible.
        try:
            import time as _time
            now_t = _time.time()
            if now_t - getattr(TickerAnalyzer, "_yf_health_last_write", 0) > 60:
                TickerAnalyzer._yf_health_last_write = now_t
                from storage.database import Database
                yf_ok = bool(yf_data.get("info") or yf_data.get("daily_ohlcv"))
                Database().upsert_source_health(
                    "yfinance", yf_ok,
                    error="" if yf_ok else "empty response (rate-limited or unreachable)")
        except Exception:
            pass

        self._parse_yfinance(td, yf_data)

        # Provider quote overrides Yahoo's often-stale price/bid/ask (real
        # bid/ask also stops false SPREAD_WIDE vetoes from one-sided quotes).
        if provider_quote:
            q, src = provider_quote
            td.data_sources["quote"] = src
            if q.get("price"):
                td.price = float(q["price"])
            if q.get("prev_close"):
                td.prev_close = float(q["prev_close"])
                td.change_pct = ((td.price - td.prev_close) / td.prev_close) * 100 if td.prev_close else td.change_pct
            if q.get("bid") and q.get("ask"):
                td.bid, td.ask = float(q["bid"]), float(q["ask"])
            if q.get("day_volume"):
                td.volume = int(q["day_volume"])
                if td.avg_volume > 0:
                    td.volume_ratio = td.volume / td.avg_volume
            # Real quote age (2026-07-21, external review) - see
            # TickerData.quote_age_minutes' field comment. Left at the 0.0/
            # not-measured default when the provider didn't supply a
            # timestamp this cycle (Tiingo's field is unverified, TwelveData
            # is currently disabled) - that's the same behavior this
            # codebase always had, just now honestly labeled as unmeasured
            # instead of silently assumed fresh.
            qt = self._parse_quote_time(q.get("quote_time"))
            if qt is not None:
                from datetime import datetime, timezone
                age_min = max(0.0, (datetime.now(timezone.utc) - qt).total_seconds() / 60.0)
                td.quote_age_minutes = age_min
                td.quote_age_is_measured = True

        # Provider news (Finnhub, real dated headlines) takes precedence;
        # yfinance headlines remain the fallback.
        if provider_news:
            titles, src = provider_news
            if titles:
                td.data_sources["news"] = src
                td.news_headlines = titles[:10]

        daily_src = intraday_src = None
        if provider_bars:
            daily_bars, daily_src = provider_bars
            td.data_sources["daily_bars"] = daily_src
            yf_data = dict(yf_data)
            yf_data["daily_ohlcv"] = {"data": daily_bars}
        if provider_ibars:
            ibars, intraday_src = provider_ibars
            td.data_sources["intraday_bars"] = intraday_src
            yf_data = dict(yf_data) if not provider_bars else yf_data
            yf_data["intraday_ohlcv"] = {"data": ibars}

        if yf_data.get("daily_ohlcv"):
            self._calc_indicators(td, yf_data["daily_ohlcv"], yf_data.get("intraday_ohlcv"))
        else:
            # No daily bars at all this cycle - every core indicator that
            # depends on them (RSI/MACD/TREND) is on its dataclass default,
            # not a real reading. VWAP separately needs intraday_ohlcv - flag
            # it too since _calc_indicators (which normally sets/clears it)
            # never even ran.
            td.stale_indicators = ["RSI", "MACD", "TREND", "VWAP"]

        self._parse_maverick(td, maverick_data)
        self._parse_finviz(td, finviz_data)
        # Direct EDGAR Form 4 data takes precedence over stock-scanner's
        # edgar_insider_trades MCP tool (unverified shape, documented
        # reliability history - see mcp_clients/edgar_data.py's module
        # docstring). Only overrides when the direct fetch actually returned
        # transactions this cycle; an empty list here (no recent Form 4s) or
        # a failed fetch (None) both fall through to whatever stock-scanner
        # already put in scanner_data["insider_trades"] unchanged.
        # _parse_scanner()'s existing generic list-parsing logic needs no
        # changes - edgar_data.py's rows already match its expected shape.
        if edgar_insider:
            scanner_data = dict(scanner_data)
            scanner_data["insider_trades"] = edgar_insider
            td.data_sources["insider_trades"] = "edgar_direct"
        self._parse_scanner(td, scanner_data)

        # Finnhub analyst-consensus fallback (2026-07-15d, no-Finviz-Elite
        # chain): finviz -> yfinance recommendationKey -> Finnhub monthly
        # Buy/Hold/Sell counts. Only fetched on FULL (non-lite) analysis and
        # only when the cheaper sources came up empty; 6h-cached inside the
        # provider, so this is at most a handful of calls per cycle.
        if not lite and td.analyst_rating in ("", "N/A"):
            try:
                reco = md_router.get_analyst_consensus(ticker)
                if reco:
                    td.analyst_rating, _src = reco
                    td.data_sources["analyst"] = _src
            except Exception:
                pass

        if td.news_headlines:
            td.news_sentiment_score = self._score_sentiment(td.news_headlines)

        missing = []
        if not yf_data:
            missing.append("yfinance")
        if not maverick_data:
            missing.append("maverick")
        if not finviz_data:
            missing.append("finviz")
        td.missing_sources = missing
        if lite:
            # Deliberate partial fetch, not degraded data - labeled so
            # scheduler's phase-2 promotion (and any UI display) can tell
            # "we chose not to fetch external sources" apart from "we tried
            # and they failed".
            td.data_quality = "lite"
        else:
            td.data_quality = "complete" if not missing else ("partial" if len(missing) < 3 else "limited")

        # Lite results live longer (see engine/cache.py's TTL_TICKER_LITE
        # note) - at exactly TTL_TICKER=300s they expired precisely at
        # HYBRID's 5-min cadence, making every cycle cold for all ~38
        # screener candidates.
        cache.set(cache_key, td, TTL_TICKER_LITE if lite else TTL_TICKER)
        return td

    @staticmethod
    def _num(v, default=0.0) -> float:
        """Coerce a possibly-string/None numeric field to float (2026-07-15:
        a live crash - `ValueError: Unknown format code 'f' for object of
        type 'str'` in packet_builder - proved Yahoo's info payload can
        return numerics as strings, e.g. trailingPE 'N/A' or '12.5'). Never
        raises; strips %/commas; falls back to `default`."""
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(",", "").replace("%", "").strip())
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _parse_quote_time(v):
        """Coerce a provider's market-timestamp field (2026-07-21, external
        review - "validate staleness from the provider's market timestamp,
        not from the time your code performed the request") into a
        timezone-aware UTC datetime. Providers disagree on shape - Alpaca
        gives RFC3339 strings, Finnhub/TwelveData/financequery give
        unix-epoch seconds, Tiingo's field is unverified - so this tries
        both defensively. Returns None (never raises) on anything
        unparseable; the caller falls back to the old 0-age approximation
        in that case, exactly as before this pass."""
        if v is None or v == "":
            return None
        try:
            from datetime import datetime, timezone
            if isinstance(v, (int, float)):
                # Alpaca-style nanosecond epoch vs. plain second epoch -
                # anything bigger than ~year-5000-in-seconds is almost
                # certainly nanoseconds.
                if v > 1e15:
                    v = v / 1e9
                elif v > 1e12:
                    v = v / 1e3
                return datetime.fromtimestamp(float(v), tz=timezone.utc)
            s = str(v).strip()
            if s.isdigit():
                return TickerAnalyzer._parse_quote_time(float(s))
            dt = pd.to_datetime(s, utc=True, errors="coerce")
            if pd.isna(dt):
                return None
            return dt.to_pydatetime()
        except Exception:
            return None

    def _parse_yfinance(self, td: TickerData, data: dict):
        info = data.get("info") or {}
        _n = self._num
        td.company_name = info.get("longName") or info.get("shortName") or ""
        td.price = _n(info.get("regularMarketPrice")) or _n(info.get("currentPrice"))
        td.prev_close = _n(info.get("previousClose")) or td.price
        if td.prev_close:
            td.change_pct = ((td.price - td.prev_close) / td.prev_close) * 100
        td.volume = int(_n(info.get("regularMarketVolume")))
        td.bid = _n(info.get("bid")) or td.price
        td.ask = _n(info.get("ask")) or td.price
        td.pe_ratio = _n(info.get("trailingPE"))
        td.eps = _n(info.get("trailingEps"))
        td.beta = _n(info.get("beta"), 1.0)
        td.market_cap = _n(info.get("marketCap"))
        td.w52_high = _n(info.get("fiftyTwoWeekHigh"))
        td.w52_low = _n(info.get("fiftyTwoWeekLow"))
        td.avg_volume = int(_n(info.get("averageVolume"), 1)) or 1
        td.quote_type = (info.get("quoteType") or "EQUITY").upper()
        # Sector from yfinance info (2026-07-15): sector used to come ONLY
        # from finviz, so when finviz was down every ticker's sector read
        # "N/A" and every sector-relative-strength signal (industry_rs 13
        # pts, sector_rs_1d 8 pts) silently died with it - confirmed in
        # production (0 sector_rs fires in 207 post-fix signals). yfinance's
        # info payload carries the same GICS-style sector name for equities.
        if info.get("sector"):
            td.sector = info["sector"]
        # §18: same payload, same reasoning as sector above - yfinance carries
        # industry alongside it, so this costs nothing and gives the
        # concentration check a second, finer axis to measure on.
        if info.get("industry"):
            td.industry = info["industry"]

        # Analyst consensus + short float from yfinance (2026-07-15d - the
        # "no Finviz Elite" fallback chain): both fields were finviz-only,
        # but Yahoo's own info payload carries recommendationKey (the
        # analyst-consensus label) and shortPercentOfFloat for free. finviz,
        # when present, still overrides these (see _parse_finviz);
        # Finnhub's free recommendations endpoint is a further fallback
        # (see analyze()). EXTERNAL no longer depends on a paid scraper.
        _RECO_MAP = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
                     "underperform": "Underperform", "sell": "Sell",
                     "strongbuy": "Strong Buy", "strongsell": "Sell"}
        reco = str(info.get("recommendationKey") or "").lower().replace("-", "_")
        if reco in _RECO_MAP and (td.analyst_rating in ("", "N/A")):
            td.analyst_rating = _RECO_MAP[reco]
        spf = info.get("shortPercentOfFloat") or info.get("sharesShortPercentOfFloat")
        if spf and not td.short_float:
            spf = _n(spf)
            # Yahoo reports a fraction (0.031) - convert; guard against a
            # payload that already sends percent (3.1).
            td.short_float = spf * 100 if spf < 1 else spf

        # News shape hardening (2026-07-15): production showed news_sentiment
        # pinned at its 0.5 default in 149/149 signals - headlines were being
        # lost whenever yfmcp returned a shape other than [{"title": ...}]
        # (e.g. {"news": [...]} wrapper, capitalized "Title" keys from the
        # markdown-table fallback parser, or "headline" keys). Extract
        # tolerantly instead of silently getting [].
        news = data.get("news") or []
        if isinstance(news, dict):
            news = news.get("news") or news.get("items") or news.get("articles") or []
        headlines = []
        for n in news if isinstance(news, list) else []:
            if isinstance(n, str):
                headlines.append(n)
            elif isinstance(n, dict):
                t = n.get("title") or n.get("Title") or n.get("headline") or n.get("Headline") or ""
                if not t and isinstance(n.get("content"), dict):
                    t = n["content"].get("title", "")
                if t:
                    headlines.append(t)
        td.news_headlines = headlines[:10]

        options = data.get("options") or {}
        if options:
            calls_vol = sum(c.get("volume", 0) or 0 for c in options.get("calls", []))
            puts_vol = sum(p.get("volume", 0) or 0 for p in options.get("puts", []))
            td.options_put_call_ratio = puts_vol / calls_vol if calls_vol > 0 else 1.0

        if td.avg_volume > 0 and td.volume > 0:
            td.volume_ratio = td.volume / td.avg_volume

    def _calc_indicators(self, td: TickerData, daily_data, intraday_data):
        # Data Provenance Circuit Breaker (see TickerData.stale_indicators'
        # docstring): tracked independently of the try/except below so a
        # LATE failure (e.g. ADX/CMF raising past RSI/MACD/TREND succeeding)
        # doesn't wrongly mark early-computed indicators stale too - each
        # flag only flips True at the exact point its own real calc didn't
        # happen. Starts assuming the worst (nothing computed yet); cleared
        # to False the moment each indicator's REAL value is actually set.
        rsi_stale, macd_stale, trend_stale, vwap_stale = True, True, True, True
        try:
            if isinstance(daily_data, dict) and "data" in daily_data:
                df = pd.DataFrame(daily_data["data"])
            elif isinstance(daily_data, list):
                df = pd.DataFrame(daily_data)
            else:
                # 2026-07-14: this branch used to `return` SILENTLY - no
                # exception, no log line - which is exactly how a real
                # production incident (every ticker, including TSLA, showing
                # 5/5 stale indicators during regular market hours) went
                # undiagnosed by log-reading alone. The most likely cause:
                # mcp_clients/base.py's call_tool() falls back to
                # {"raw": <text>} when Yahoo's response isn't valid JSON
                # (e.g. a rate-limit/error page under heavy call volume) -
                # that dict has no "data" key, so it silently fails this
                # shape check with zero breadcrumbs. Logging here doesn't fix
                # the underlying data availability, but makes the NEXT
                # occurrence diagnosable in the Logs tab in seconds instead
                # of requiring a full DB/log archaeology pass like this one.
                snippet = repr(daily_data)[:200] if daily_data is not None else "None"
                logging.getLogger(__name__).warning(
                    f"{td.ticker}: daily_ohlcv had an unrecognized shape ({type(daily_data).__name__}), "
                    f"can't compute any indicator this cycle. First 200 chars: {snippet}"
                )
                return

            df.columns = [c.lower() for c in df.columns]
            required = ["open", "high", "low", "close", "volume"]
            if not all(c in df.columns for c in required):
                logging.getLogger(__name__).warning(
                    f"{td.ticker}: daily_ohlcv parsed but is missing required columns "
                    f"(have: {list(df.columns)}, need: {required})"
                )
                return

            # Preserve the date index (2026-07-21, external review -
            # "prioritize preserving the daily OHLCV date index all the way
            # through ticker_analyzer.py") BEFORE the OHLCV-only slice below
            # drops every other column. Captured against df's current index
            # so it can be realigned after dropna() below trims rows.
            # Providers that don't supply a date field yet (see
            # mcp_clients/market_data.py) leave this None, and
            # _calc_earnings_avwap() falls back to its old calendar-day
            # approximation exactly as before - this is additive, not a
            # behavior change for those sources.
            _date_col = next((c for c in ("date", "timestamp", "t", "time") if c in df.columns), None)
            _dates_raw = df[_date_col].copy() if _date_col else None

            df = df[required].astype(float).dropna()
            _dates = None
            if _dates_raw is not None:
                try:
                    _dates = pd.to_datetime(
                        _dates_raw.reindex(df.index), utc=True, errors="coerce"
                    ).dt.date
                    if _dates.isna().all():
                        _dates = None
                except Exception:
                    _dates = None
            if len(df) < 20:
                logging.getLogger(__name__).warning(
                    f"{td.ticker}: only {len(df)} usable daily bars after cleaning (need >= 20) - "
                    f"too little history for any indicator this cycle"
                )
                return

            df.ta.sma(length=20, append=True)
            df.ta.sma(length=50, append=True)
            df.ta.sma(length=200, append=True)
            df.ta.ema(length=9, append=True)
            df.ta.ema(length=21, append=True)

            td.sma_20 = df["SMA_20"].iloc[-1] if "SMA_20" in df else 0.0
            td.sma_50 = df["SMA_50"].iloc[-1] if "SMA_50" in df else 0.0
            td.sma_200 = df["SMA_200"].iloc[-1] if "SMA_200" in df else 0.0
            td.ema_9 = df["EMA_9"].iloc[-1] if "EMA_9" in df else 0.0
            td.ema_21 = df["EMA_21"].iloc[-1] if "EMA_21" in df else 0.0
            # TREND is "stale" if ANY of the 5 moving averages it's built
            # from didn't get a real column back from pandas-ta (rather than
            # requiring all 5 to fail) - a trend read built on 3-of-5 real
            # MAs is still a degraded read, not a fully trustworthy one.
            trend_stale = not all(c in df.columns for c in ("SMA_20", "SMA_50", "SMA_200", "EMA_9", "EMA_21"))

            df.ta.rsi(length=14, append=True)
            td.rsi = df["RSI_14"].iloc[-1] if "RSI_14" in df else 50.0
            rsi_stale = "RSI_14" not in df.columns or pd.isna(df["RSI_14"].iloc[-1])

            df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
            td.stoch_k = df["STOCHk_14_3_3"].iloc[-1] if "STOCHk_14_3_3" in df else 50.0
            td.stoch_d = df["STOCHd_14_3_3"].iloc[-1] if "STOCHd_14_3_3" in df else 50.0

            df.ta.macd(fast=12, slow=26, signal=9, append=True)
            td.macd = df["MACD_12_26_9"].iloc[-1] if "MACD_12_26_9" in df else 0.0
            td.macd_signal = df["MACDs_12_26_9"].iloc[-1] if "MACDs_12_26_9" in df else 0.0
            td.macd_hist = df["MACDh_12_26_9"].iloc[-1] if "MACDh_12_26_9" in df else 0.0
            macd_stale = "MACD_12_26_9" not in df.columns or pd.isna(df["MACD_12_26_9"].iloc[-1])
            if len(df) >= 2:
                prev_macd = df["MACD_12_26_9"].iloc[-2] if "MACD_12_26_9" in df else 0
                prev_sig = df["MACDs_12_26_9"].iloc[-2] if "MACDs_12_26_9" in df else 0
                curr_above = td.macd > td.macd_signal
                prev_above = prev_macd > prev_sig
                td.macd_crossover = curr_above != prev_above
                td.macd_crossover_direction = "bullish" if curr_above else "bearish"

            if "MACDh_12_26_9" in df:
                td.macd_positive_days = self._consecutive_positive_days(df["MACDh_12_26_9"])

            df.ta.bbands(length=20, std=2, append=True)
            td.bb_upper = df["BBU_20_2.0"].iloc[-1] if "BBU_20_2.0" in df else 0.0
            td.bb_lower = df["BBL_20_2.0"].iloc[-1] if "BBL_20_2.0" in df else 0.0
            if td.bb_upper > td.bb_lower:
                td.bb_pct = (td.price - td.bb_lower) / (td.bb_upper - td.bb_lower)

            df.ta.atr(length=14, append=True)
            td.atr = df["ATRr_14"].iloc[-1] if "ATRr_14" in df else 0.0

            df.ta.adx(length=14, append=True)
            if "ADX_14" in df and not pd.isna(df["ADX_14"].iloc[-1]):
                td.adx = float(df["ADX_14"].iloc[-1])
                td.plus_di = float(df["DMP_14"].iloc[-1]) if not pd.isna(df["DMP_14"].iloc[-1]) else 0.0
                td.minus_di = float(df["DMN_14"].iloc[-1]) if not pd.isna(df["DMN_14"].iloc[-1]) else 0.0

            df.ta.cmf(length=20, append=True)
            if "CMF_20" in df and not pd.isna(df["CMF_20"].iloc[-1]):
                td.cmf = float(df["CMF_20"].iloc[-1])

            if len(df) >= 20:
                donchian_high = df["high"].rolling(20).max().iloc[-1]
                if not pd.isna(donchian_high):
                    td.donchian_20d_high = float(donchian_high)

            self._calc_swing_low_avwap(td, df)
            self._calc_earnings_avwap(td, df, _dates)

            self._calc_volatility_compression(td, df)

            df.ta.obv(append=True)
            if "OBV" in df and len(df) >= 5:
                obv_recent = df["OBV"].iloc[-5:].values
                if obv_recent[-1] > obv_recent[0] * 1.01:
                    td.obv_trend = "rising"
                elif obv_recent[-1] < obv_recent[0] * 0.99:
                    td.obv_trend = "falling"

            # ---- Accumulation signals (2026-07-15) - same bars, zero extra
            # MCP calls. See TickerData field comments for definitions. ----
            if "OBV" in df and len(df) >= 20:
                obv20 = df["OBV"].iloc[-20:]
                close20 = df["close"].iloc[-20:]
                td.obv_new_high_20d = bool(obv20.iloc[-1] >= obv20.max())
                # Divergence: OBV at a 20-bar high while price sits >=0.5%
                # below its own 20-bar high - volume leading price.
                td.obv_divergence = bool(
                    td.obv_new_high_20d and close20.iloc[-1] < close20.max() * 0.995
                )
            if len(df) >= 50:
                dollar_vol = df["close"] * df["volume"]
                dv20 = float(dollar_vol.iloc[-20:].mean())
                dv50 = float(dollar_vol.iloc[-50:].mean())
                if dv50 > 0:
                    td.dollar_vol_ratio_20_50 = dv20 / dv50
            if len(df) >= 30:
                vol20 = df["volume"].rolling(20).mean()
                accum = (df["close"].diff() > 0) & (df["volume"] > vol20)
                td.accumulation_days_10 = int(accum.iloc[-10:].sum())
            if len(df) >= 22:
                base_close = float(df["close"].iloc[-22])
                if base_close > 0:
                    td.return_1m_pct = (float(df["close"].iloc[-1]) / base_close - 1.0) * 100

            # True weekly resample (2026-07-15, external review): the old
            # weekly_trend_aligned rule (8 pts) used a pure daily-data
            # proxy (price > daily SMA20). With 1y of daily bars now
            # fetched, real trading-week closes are available: every 5th
            # bar's close (provider bars carry no dates, so consecutive
            # 5-bar blocks approximate trading weeks - correct within one
            # holiday-shortened week per quarter, far closer to a real
            # weekly resample than the daily proxy was).
            closes = df["close"].to_numpy()
            n_weeks = len(closes) // 5
            if n_weeks >= 20:
                weekly_closes = closes[len(closes) - n_weeks * 5:].reshape(n_weeks, 5)[:, -1]
                w_sma20 = weekly_closes[-20:].mean()
                td.weekly_above_sma20 = bool(weekly_closes[-1] > w_sma20)
                if n_weeks >= 50:
                    w_sma50 = weekly_closes[-50:].mean()
                    td.weekly_above_sma50 = bool(weekly_closes[-1] > w_sma50)

            if intraday_data:
                try:
                    if isinstance(intraday_data, dict) and "data" in intraday_data:
                        idf = pd.DataFrame(intraday_data["data"])
                    else:
                        idf = pd.DataFrame(intraday_data if isinstance(intraday_data, list) else [])
                    idf.columns = [c.lower() for c in idf.columns]
                    if all(c in idf.columns for c in ["high", "low", "close", "volume"]):
                        idf = idf[["high", "low", "close", "volume"]].astype(float)
                        # Manual VWAP (2026-07-15): pandas_ta's df.ta.vwap
                        # refuses to run without an ordered DatetimeIndex
                        # ("[!] VWAP requires an ordered DatetimeIndex" seen
                        # live - MCP/provider bars arrive with a plain
                        # RangeIndex, so VWAP silently stayed stale every
                        # cycle on machines with the real pandas_ta). The
                        # formula needs no index at all: cum(typical*vol)/cum(vol).
                        typical = (idf["high"] + idf["low"] + idf["close"]) / 3
                        cum_vol = idf["volume"].cumsum()
                        vwap_series = (typical * idf["volume"]).cumsum() / cum_vol.replace(0, pd.NA)
                        if len(vwap_series) and not pd.isna(vwap_series.iloc[-1]):
                            td.vwap = float(vwap_series.iloc[-1])
                            vwap_stale = False
                except Exception:
                    pass
            # else: intraday_data was falsy - vwap_stale stays True, same as
            # the exception path above; td.vwap keeps its 0.0 dataclass default.

            recent = df.tail(20)
            highs = recent["high"].nlargest(3).tolist()
            lows = recent["low"].nsmallest(3).tolist()
            td.resistance_levels = [round(h, 2) for h in highs]
            td.support_levels = [round(low, 2) for low in lows]

        except Exception as e:
            # 2026-07-14: removed a local `import logging` that used to live
            # right here - Python scopes a name as local to the WHOLE
            # function the moment it sees any `import`/assignment to it
            # anywhere in the function body, even below where it's used. That
            # silently shadowed the module-level `import logging` (top of
            # this file) for every use of `logging` earlier in this method
            # (including the new diagnostic logging above), causing
            # "UnboundLocalError: local variable 'logging' referenced before
            # assignment" the moment this function ran - which would have
            # made the very diagnostics meant to catch the 2026-07-14
            # "100% of tickers stale" incident crash instead of log. Caught
            # by testing before shipping.
            logging.getLogger(__name__).warning(f"Indicator calc error for {td.ticker}: {e}")
        finally:
            # Always runs, success or failure - whatever RSI/MACD/TREND/VWAP
            # flags got cleared before an exception (if any) hit are exactly
            # the ones that got a real value; anything still True here
            # genuinely fell back to its dataclass default this cycle.
            stale = []
            if rsi_stale:
                stale.append("RSI")
            if macd_stale:
                stale.append("MACD")
            if trend_stale:
                stale.append("TREND")
            if vwap_stale:
                stale.append("VWAP")
            td.stale_indicators = stale

    def _calc_volatility_compression(self, td: TickerData, df):
        """TTM Squeeze + NR7/NR4 + inside day - all computed from the same
        daily OHLCV bars already in `df` (this runs inside _calc_indicators,
        after bbands/atr/sma have been appended), so this is REAL, not a
        placeholder, and costs zero extra MCP calls.

        These measure something the rest of the scoring engine doesn't:
        volatility CONTRACTING before it expands, rather than the direction
        or strength of an existing move (that's what trend/momentum already
        cover). A squeeze/NR7/inside-day is a setup, not a signal on its
        own - see rules/swing_buy_rules.py's VOLATILITY_EXPANSION bucket,
        which deliberately has a 0% min-qualify threshold so this only adds
        bonus points when it fires and never blocks a trade that has no
        compression at all (most good trend/momentum setups won't).

        TTM Squeeze definition (John Carter): Bollinger Bands (20, 2std)
        contract inside Keltner Channels (basis SMA20, +/-1.5x ATR14 here -
        the original uses ATR20, but this codebase already computes ATR14
        for everything else, so reusing it avoids a second ATR calc for a
        ~1-bar-lag difference that doesn't matter at swing-trade horizons).
        "Firing" = the squeeze was ON within the last 5 bars and is OFF now
        (released) - the actual breakout signal, not just "currently
        squeezed" (a multi-week squeeze with no release yet isn't tradeable).
        """
        try:
            if not all(c in df for c in ("SMA_20", "ATRr_14", "BBU_20_2.0", "BBL_20_2.0")):
                return
            kc_mult = 1.5
            kc_upper = df["SMA_20"] + kc_mult * df["ATRr_14"]
            kc_lower = df["SMA_20"] - kc_mult * df["ATRr_14"]
            squeeze_series = ((df["BBU_20_2.0"] < kc_upper) & (df["BBL_20_2.0"] > kc_lower)).fillna(False)
            if len(squeeze_series) >= 6:
                was_compressed = bool(squeeze_series.iloc[-6:-1].any())
                compressed_now = bool(squeeze_series.iloc[-1])
                td.squeeze_active = was_compressed and not compressed_now

            ranges = df["high"] - df["low"]
            if len(ranges) >= 7:
                last7 = ranges.iloc[-7:]
                td.is_nr7 = bool(last7.idxmin() == last7.index[-1])
            if len(ranges) >= 4:
                last4 = ranges.iloc[-4:]
                td.is_nr4 = bool(last4.idxmin() == last4.index[-1])

            if len(df) >= 2:
                today_high, today_low = df["high"].iloc[-1], df["low"].iloc[-1]
                y_high, y_low = df["high"].iloc[-2], df["low"].iloc[-2]
                td.is_inside_day = bool(today_high <= y_high and today_low >= y_low)
        except Exception as e:
            import logging
            logging.warning(f"Volatility compression calc error for {td.ticker}: {e}")

    def _consecutive_positive_days(self, macd_hist_series) -> int:
        """Count consecutive trailing bars (including today) where MACD
        histogram > 0, stopping at the first non-positive bar walking
        backward. Capped at 60 bars (~3 months of daily data) so a division-
        by-zero-adjacent numeric quirk somewhere upstream can't produce an
        absurd streak length; MACD histogram realistically doesn't stay
        strictly positive for anywhere close to that long anyway."""
        count = 0
        for val in reversed(macd_hist_series.tolist()):
            if pd.isna(val) or val <= 0:
                break
            count += 1
            if count >= 60:
                break
        return count

    def _calc_swing_low_avwap(self, td: TickerData, df):
        """Anchored VWAP from the most recent swing low - REAL, computed from
        the same OHLCV+volume bars as everything else in this file, zero
        extra MCP calls.

        "Swing low" here is simply the lowest LOW in the trailing 20 bars
        (same lookback window support_levels/resistance_levels already use
        below) - not a fancier local-minimum-detection algorithm, matching
        the level of sophistication already established elsewhere in this
        file rather than inventing a new one. AVWAP is the cumulative
        volume-weighted average price from that bar forward through the most
        recent bar - used by rules/swing_buy_rules.py's VOLUME_PA bucket
        (avwap_swing_low_bounce) and by engine/stop_state_machine.py's
        TREND_FOLLOWING trail (recent_swing_low).

        NOTE: avwap_earnings (anchored VWAP from the LAST earnings date) is
        NOT computed here and stays PLACEHOLDER in
        engine/ticker_data_adapter.py - this codebase has no reliable source
        for the last (past) earnings date. td.earnings_date/days_to_earnings
        (see _parse_finviz) is a forward-looking guess at the NEXT earnings
        date, which isn't usable as a backward-looking anchor.
        """
        try:
            window = df.tail(20)
            if len(window) < 2:
                return
            swing_low_pos = int(window["low"].values.argmin())
            anchor_idx = window.index[swing_low_pos]
            td.recent_swing_low = float(window["low"].iloc[swing_low_pos])

            anchored = df.loc[anchor_idx:]
            if len(anchored) >= 1:
                typical = (anchored["high"] + anchored["low"] + anchored["close"]) / 3
                cum_vol = anchored["volume"].cumsum()
                cum_vol_price = (typical * anchored["volume"]).cumsum()
                vwap_series = cum_vol_price / cum_vol.replace(0, np.nan)
                last_vwap = vwap_series.iloc[-1]
                if not pd.isna(last_vwap):
                    td.avwap_swing_low = float(last_vwap)
        except Exception as e:
            import logging
            logging.warning(f"Swing-low AVWAP calc error for {td.ticker}: {e}")

    def _calc_earnings_avwap(self, td: TickerData, df, dates=None):
        """Anchored VWAP from the last REAL earnings report date - same
        cumulative-VWAP math as _calc_swing_low_avwap() above, now that
        td.last_earnings_date (2026-07-16, FMPProvider.get_last_earnings_date)
        finally gives this codebase a genuine backward-looking earnings
        anchor. _calc_swing_low_avwap()'s docstring used to note this was
        unusable - that's fixed as of this pass.

        REAL DATE ANCHOR (2026-07-21, external review - the old calendar-day
        5/7 ratio approximation "can produce incorrect anchors around
        holidays, closures, long weekends, delayed reports, pre/after-hours
        reports"): `dates` is a pandas Series of real per-bar session dates,
        aligned to df's index, threaded through from _calc_indicators() -
        see mcp_clients/market_data.py for which providers now supply a real
        date field. Session convention when a date index IS available:
          - time hint "bmo" (before market open): the report already landed
            in that session's own bar - anchor there.
          - time hint "amc" (after market close) or unknown: the reaction
            shows up in the NEXT session - anchor at the first bar strictly
            after the report date. This is also the conservative default
            when the hint is unknown, since it never leaks a pre-reaction
            bar into the anchor window.
        td.earnings_avwap_anchor_approximate is False only when a real date
        index was available AND the hint was a confirmed "bmo"/"amc" (not
        blank/unknown). Falls back to the old calendar-day approximation
        (always flagged approximate) when no usable date index exists this
        cycle - e.g. a provider without a date field, or every dated bar got
        trimmed by _calc_indicators()'s dropna(). Skips entirely (leaves
        td.avwap_earnings at its 0.0 default) when last_earnings_date is
        unset (FMP call failed/unavailable this cycle, or the free-tier key
        isn't configured at all)."""
        if not td.last_earnings_date:
            return
        try:
            from datetime import datetime
            earnings_dt = datetime.strptime(td.last_earnings_date, "%Y-%m-%d").date()
            hint = (td.last_earnings_time_hint or "").lower()
            anchor_idx = None
            approximate = True
            anchor_mode = "calendar_fallback_approx"
            anchor_date_str = ""

            if dates is not None:
                valid_dates = dates.dropna()
                if len(valid_dates):
                    if hint == "bmo":
                        candidates = valid_dates[valid_dates >= earnings_dt]
                        approximate = False
                        anchor_mode = "bmo_exact"
                    elif hint == "amc":
                        candidates = valid_dates[valid_dates > earnings_dt]
                        approximate = False
                        anchor_mode = "amc_exact"
                    else:
                        # Unknown time-of-day: default to the AMC convention
                        # (report reaction lands next session) since that
                        # never includes a pre-reaction bar, but keep it
                        # flagged approximate - we're guessing the session,
                        # not confirming it.
                        candidates = valid_dates[valid_dates > earnings_dt]
                        approximate = True
                        anchor_mode = "unknown_hint_approx"
                    if len(candidates):
                        anchor_idx = candidates.index[0]
                        anchor_date_str = candidates.iloc[0].isoformat()
                    else:
                        anchor_mode = "calendar_fallback_approx"

            if anchor_idx is not None:
                anchored = df.loc[anchor_idx:]
            else:
                # FALLBACK: no usable date index this cycle - keep the old
                # calendar-day approximation rather than dropping the signal
                # outright. Always flagged approximate.
                calendar_days = (datetime.utcnow().date() - earnings_dt).days
                if calendar_days <= 0:
                    return  # defensive - FMP's own epsActual filter should already guarantee a past date
                approx_trading_days = max(1, round(calendar_days * 5 / 7))
                anchor_n = min(approx_trading_days + 1, len(df))
                if anchor_n < 1:
                    return
                anchored = df.tail(anchor_n)
                approximate = True
                anchor_mode = "calendar_fallback_approx"
                anchor_date_str = ""

            if len(anchored) < 1:
                return
            typical = (anchored["high"] + anchored["low"] + anchored["close"]) / 3
            cum_vol = anchored["volume"].cumsum()
            cum_vol_price = (typical * anchored["volume"]).cumsum()
            vwap_series = cum_vol_price / cum_vol.replace(0, np.nan)
            last_vwap = vwap_series.iloc[-1]
            if not pd.isna(last_vwap):
                td.avwap_earnings = float(last_vwap)
                td.earnings_avwap_anchor_approximate = approximate
                td.earnings_avwap_anchor_mode = anchor_mode
                td.earnings_avwap_anchor_confidence = "low" if approximate else "high"
                td.earnings_avwap_anchor_date = anchor_date_str
        except Exception as e:
            import logging
            logging.warning(f"Earnings AVWAP calc error for {td.ticker}: {e}")

    def _parse_maverick(self, td: TickerData, data: dict):
        """Use Maverick data to supplement/override local pandas-ta calculations."""
        td.maverick_data_present = bool(
            any(v for v in data.values()) if isinstance(data, dict) else False
        )
        tech = data.get("technical") or {}
        if isinstance(tech, dict) and tech:
            # _num coercion (2026-07-15): same string-numeric hardening as
            # _parse_yfinance - a "N/A" from any source must never crash a
            # cycle thread with an unguarded float().
            if "rsi" in tech:
                td.rsi = self._num(tech["rsi"], td.rsi)
            if "macd" in tech:
                td.macd = self._num(tech["macd"], td.macd)
            if "macd_signal" in tech:
                td.macd_signal = self._num(tech["macd_signal"], td.macd_signal)
            if "bb_upper" in tech:
                td.bb_upper = self._num(tech["bb_upper"], td.bb_upper)
            if "bb_lower" in tech:
                td.bb_lower = self._num(tech["bb_lower"], td.bb_lower)

        sentiment = data.get("sentiment") or {}
        if isinstance(sentiment, dict) and sentiment:
            score = sentiment.get("sentiment_score") or sentiment.get("score")
            if score is not None:
                td.maverick_sentiment = self._num(score, td.maverick_sentiment)

    def _parse_finviz(self, td: TickerData, data: dict):
        if not data:
            return
        td.technical_rating = data.get("technical_rating", "N/A")
        # finviz overrides the yfinance-sourced fallbacks when it actually
        # has values - but must not clobber them with "N/A"/0 (2026-07-15d).
        if data.get("analyst_rating") and data.get("analyst_rating") != "N/A":
            td.analyst_rating = data["analyst_rating"]
        td.analyst_target = self._num(data.get("target_price")) or td.analyst_target
        td.short_float = self._num(data.get("short_float")) or td.short_float
        td.earnings_date = data.get("earnings_date", "N/A")
        # Don't clobber a real yfinance-sourced sector with finviz's "N/A"
        if data.get("sector") and data.get("sector") != "N/A":
            td.sector = data["sector"]
        if data.get("industry") and data.get("industry") != "N/A":
            td.industry = data["industry"]

        if td.earnings_date and td.earnings_date != "N/A":
            try:
                from datetime import datetime
                import pytz
                et = pytz.timezone("US/Eastern")
                ed = datetime.strptime(td.earnings_date, "%b %d")
                ed = ed.replace(year=datetime.now().year)
                today = datetime.now(et).date()
                days = (ed.date() - today).days
                # finviz gives no year, so two failure modes exist (found
                # 2026-07-16 via paper exits reading "Earnings in -72 days"):
                #  1. Dec->Jan rollover: "Jan 15" parsed in December lands
                #     ~11 months in the past - it's really NEXT year.
                #  2. finviz showing the LAST report date ("May 05" seen in
                #     July = -72d): a past date says nothing about the NEXT
                #     earnings, so treat as unknown (999 sentinel, same as
                #     the parse-failure path). Downstream consumers
                #     (sell_rules earnings_approaching, exit_scorer bucket 5,
                #     hard_vetoes earnings risk) all interpret small values
                #     as "earnings imminent" - a negative here caused
                #     positions to be sold hours after entry.
                if days < -180:
                    days = (ed.replace(year=ed.year + 1).date() - today).days
                td.days_to_earnings = days if days >= 0 else 999
            except Exception:
                td.days_to_earnings = 999

    # TradingView's own vocabulary (STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL,
    # sometimes lowercase/hyphenated) normalized to this codebase's existing
    # Buy/Strong Buy/Hold/Sell/Strong Sell convention (matches finviz's
    # _derive_analyst_rating() labels so downstream consumers - EXTERNAL
    # bucket's analyst_/finviz_ checks - don't need two vocabularies).
    _TRADINGVIEW_RATING_MAP = {
        "strong_buy": "Strong Buy", "strongbuy": "Strong Buy",
        "buy": "Buy",
        "neutral": "Hold", "hold": "Hold",
        "sell": "Sell",
        "strong_sell": "Strong Sell", "strongsell": "Strong Sell",
    }

    def _parse_scanner(self, td: TickerData, data: dict):
        """2026-07-22 (Trinath: "source finviz/FMP's data elsewhere") - see
        mcp_clients/stock_scanner.py's module docstring for why insider_trades
        is now backed by SEC EDGAR (edgar_insider_trades) instead of the dead
        get_insider_trades, and for the two brand-new fields (technical_rating,
        unusual_options) this parses. None of the three response shapes below
        are verified against a live call (this MCP isn't reachable from where
        this fix was written) - every branch is defensive/best-effort and
        wrapped so an unexpected shape degrades to "no signal", never a
        crash or a fabricated value. If these stay empty in production, the
        shape guesses here are the first thing to check (see
        mcp_clients/maverick.py's 2026-07-15 wrong-argument-name bug for
        exactly this failure mode happening before)."""
        if not data:
            return

        # ---- insider trades: SEC EDGAR filings (edgar_insider_trades) ----
        # Old shape (pre-aggregated {"recent_buys": N, "recent_sells": N})
        # kept as a fallback in case a future server version returns that
        # again; the more likely REAL shape for a raw EDGAR wrapper is a
        # list of individual Form 4 transactions, each tagged buy/sell by
        # some combination of code/type/action fields - handled generically
        # below rather than assuming one exact key name.
        insider = data.get("insider_trades")
        buys = sells = None
        if isinstance(insider, dict) and ("recent_buys" in insider or "recent_sells" in insider):
            buys = int(insider.get("recent_buys") or 0)
            sells = int(insider.get("recent_sells") or 0)
        elif isinstance(insider, list) and insider:
            buys, sells = 0, 0
            for tx in insider:
                if not isinstance(tx, dict):
                    continue
                raw = str(
                    tx.get("transactionCode") or tx.get("transaction_code") or
                    tx.get("transactionType") or tx.get("transaction_type") or
                    tx.get("acquiredDisposedCode") or tx.get("acquired_disposed_code") or
                    tx.get("code") or tx.get("action") or tx.get("type") or ""
                ).strip().upper()
                if raw in ("P", "A", "BUY", "PURCHASE"):
                    buys += 1
                elif raw in ("S", "D", "SELL", "SALE"):
                    sells += 1
        if buys is not None:
            td.insider_buys_30d = buys
            td.insider_sells_30d = sells
            if buys > sells * 1.5:
                td.insider_net_direction = "buying"
            elif sells > buys * 1.5:
                td.insider_net_direction = "selling"
            else:
                td.insider_net_direction = "neutral"

        # ---- technical rating: TradingView gauge (tradingview_technicals) ----
        # 2026-07-22 (param-name fix follow-up): this tool takes {"tickers":
        # [...]} - a BATCH-shaped call, not a single-symbol one like every
        # other per-ticker tool here - confirmed via the live "expected:
        # array, path: tickers" error that motivated fixing the call site in
        # mcp_clients/stock_scanner.py. A batch CALL shape strongly implies a
        # batch RESPONSE shape too (keyed by ticker, or a list of per-ticker
        # records) rather than one flat dict describing the single queried
        # ticker directly - unwrap that first, then fall through to the
        # original flat-dict handling for whatever's left, so this degrades
        # to "no rating" instead of misreading an unrelated key if the real
        # shape turns out to be flat after all.
        tv = data.get("technical_rating")
        if isinstance(tv, dict) and td.ticker in tv:
            tv = tv[td.ticker]
        elif isinstance(tv, list) and tv:
            tv = next((r for r in tv if isinstance(r, dict) and
                       str(r.get("ticker") or r.get("symbol") or "").upper() == td.ticker.upper()), tv[0])
        if isinstance(tv, dict):
            raw_rating = (
                tv.get("summary") or tv.get("recommendation") or tv.get("rating") or
                (tv.get("summary") or {}).get("RECOMMENDATION") if isinstance(tv.get("summary"), dict) else None
            )
            if raw_rating:
                key = str(raw_rating).strip().lower().replace("-", "_").replace(" ", "_")
                mapped = self._TRADINGVIEW_RATING_MAP.get(key)
                if mapped:
                    td.tradingview_rating = mapped
        elif isinstance(tv, str) and tv:
            key = tv.strip().lower().replace("-", "_").replace(" ", "_")
            mapped = self._TRADINGVIEW_RATING_MAP.get(key)
            if mapped:
                td.tradingview_rating = mapped

        # ---- unusual options activity (options_unusual_activity) ----
        uo = data.get("unusual_options")
        try:
            if isinstance(uo, dict):
                sentiment = str(uo.get("sentiment") or uo.get("bias") or "").strip().lower()
                if sentiment:
                    td.unusual_options_bullish = sentiment in ("bullish", "buy", "call", "calls")
                else:
                    calls = float(uo.get("call_volume") or uo.get("calls") or 0)
                    puts = float(uo.get("put_volume") or uo.get("puts") or 0)
                    if calls or puts:
                        td.unusual_options_bullish = calls > puts * 1.3
            elif isinstance(uo, list) and uo:
                calls = sum(1 for r in uo if isinstance(r, dict) and
                            str(r.get("type") or r.get("side") or "").strip().lower() in ("call", "calls"))
                puts = sum(1 for r in uo if isinstance(r, dict) and
                           str(r.get("type") or r.get("side") or "").strip().lower() in ("put", "puts"))
                if calls or puts:
                    td.unusual_options_bullish = calls > puts * 1.3
        except (TypeError, ValueError):
            pass  # malformed record - leave unusual_options_bullish at None (no credit, not a crash)

        short = data.get("short_interest") or {}
        if short:
            sf = short.get("short_float") or short.get("shortFloat")
            if sf:
                td.short_float = float(str(sf).replace("%", ""))

    def _score_sentiment(self, headlines: list[str]) -> float:
        """Simple free/local keyword scorer - zero extra dependencies, no API key."""
        positive = ["beat", "surge", "rally", "upgrade", "record", "strong", "growth", "profit",
                    "exceed", "bullish", "buy", "outperform", "raise", "boost", "gain", "climb",
                    "jump", "rise"]
        negative = ["miss", "drop", "downgrade", "loss", "weak", "cut", "decline", "fall",
                    "bearish", "sell", "underperform", "reduce", "warning", "concern", "risk",
                    "crash", "plunge"]
        pos_count = sum(1 for h in headlines for word in positive if word in h.lower())
        neg_count = sum(1 for h in headlines for word in negative if word in h.lower())
        total = pos_count + neg_count
        if total == 0:
            return 0.5
        return round(pos_count / total, 2)
