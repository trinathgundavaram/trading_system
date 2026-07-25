"""Stage 1 historical replay engine (market-data-only) - Priority 1 from the
2026-07-23 review exchange: "the bottleneck isn't the model, it's the
data-generation mechanism." This module reuses the REAL production decision
chain (rules/hard_vetoes.py's check(), rules/swing_buy_rules.py's score(),
which internally calls rules/dynamic_thresholds.py + rules/execution_quality.py
+ rules/probabilistic_decision.py) against historical daily bars, instead of
a separately-maintained "backtest version" of the strategy that would
inevitably drift from production.

SCOPE (Stage 1 only, matching the reviewer's own two-stage plan - "start with
features that can be reconstructed reliably from historical OHLCV... add
point-in-time external data only if you can guarantee point-in-time
correctness, otherwise mark those features unavailable"):

  REAL, from historical bars (point-in-time correct by construction - every
  indicator is computed from a bars window truncated to the simulated date,
  nothing later ever leaks in):
    TREND, MOMENTUM, VOLUME_PA, VOLATILITY_EXPANSION buckets (SMA/EMA/RSI/
    MACD/Stochastic/ADX/Donchian/Bollinger/ATR/OBV/CMF/weekly-trend/RS-vs-SPY/
    squeeze-NR7-NR4-inside-day/accumulation signals).
    VIX - real historical ^VIX close (yfinance) - point-in-time safe, VIX is
    a published daily index value, never restated.

  Deliberately marked UNAVAILABLE, not faked (same "UNKNOWN != FALSE"
  philosophy engine/ticker_data_adapter.py already uses on a live data
  outage - this codebase has the machinery to redistribute a dark bucket's
  weight to the buckets that DO have real data, rather than scoring the
  bucket's neutral defaults as if they were measured evidence):
    EXTERNAL bucket (analyst consensus/estimates, maverick sentiment, finviz/
    tradingview rating) - no free point-in-time historical source exists for
    any of these; a naive replay querying "today's" analyst consensus for a
    trade dated a year ago would be textbook look-ahead bias. Leaving these
    fields at TickerData's own dataclass defaults makes
    rules/swing_buy_rules.py's existing external_data_available check
    correctly read False, triggering the real production redistribution
    logic unmodified.
    SENTIMENT_MACRO's news/insider/F&G fields, MARKET_BREADTH's sector-ETF
    proxy - same reasoning; Fear & Greed in particular has no clean free
    historical archive. UNLIKE EXTERNAL, production's SENTIMENT_MACRO/
    MARKET_BREADTH scoring had no "unavailable" concept at all before
    2026-07-24 (a zero-trades audit found these two buckets were silently
    scoring this module's neutral placeholders as real measured evidence,
    not flagged as a gap) - see rules/swing_buy_rules.py's BUCKET
    AVAILABILITY section. Fixed by: (1) reusing market_data["breadth_stale"],
    already hardcoded True a few lines below in build_market_data_asof, for
    MARKET_BREADTH: (2) _td_to_dict below now sets the new
    "sentiment_macro_data_available": False flag explicitly for
    SENTIMENT_MACRO. Both now get the same 75%-redistributed/25%-dead
    treatment EXTERNAL already had, all-or-nothing (no per-rule tri-state
    fraction like EXTERNAL's yet).
    Intraday VWAP - this pipeline has no historical intraday-bar source
    wired in, so td.vwap stays at its dataclass default (0.0, "stale") and
    the above_vwap rule simply never fires, exactly like a live cycle with
    no intraday data this session.

KNOWN SIMPLIFICATIONS (documented, not hidden):
  - Exit modeling uses a fixed ATR-tiered initial stop + fixed R-multiple
    take-profit target (simulate_forward_exit below), NOT a full replay of
    engine/stop_state_machine.py's 6-state trailing stop. A faithful replay
    of the trailing-stop state machine bar-by-bar is a real Stage-1.5
    enhancement, not built here.
  - hard_vetoes.check()'s two DB-coupled checks (cooldown, already-open) are
    served by _BacktestFakeDB below rather than a live Postgres connection -
    every OTHER veto/scoring/threshold function is the exact unmodified
    production code, called the same way scheduler.py calls it.
  - Bid/ask are synthetic (price +/- 0.05%, a tight but not free-lunch
    spread) since no historical bid/ask exists for daily bars - this means
    SPREAD_WIDE and the execution-quality spread component are non-
    discriminating in Stage 1, not a real signal. Documented gap, not a
    fabricated one.
  - Indicator computation always goes through the REAL production
    engine/ticker_analyzer.py's _calc_indicators (same df.ta.* calls the live
    scheduler uses every cycle) - no separate backtest-only reimplementation.
    That function itself transparently uses either real pandas_ta or
    engine/ta_fallback.py's hand-rolled accessor (same method/column names)
    when pandas_ta can't be imported (see that file's docstring - pandas_ta
    currently has no PyPI wheel for Python <3.12), so this module never needs
    to know or care which backend is active.
"""
import calendar
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Historical bars
# ---------------------------------------------------------------------------

def fetch_daily_bars(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Daily OHLCV via yfinance, normalized to lowercase open/high/low/close/
    volume columns plus a 'date' column, sorted ascending. Raises on an
    empty/failed fetch rather than silently returning nothing - a backtest
    that quietly ran on zero bars for a ticker would look like a real (if
    boring) result instead of the data problem it actually is."""
    import yfinance as yf

    # timeout=30: yfinance's default HTTP session has no read timeout, so a
    # stalled connection (rate limiting, a flaky network) can hang this call
    # indefinitely - and since this runs inside the backtest subprocess,
    # unlike a plain crash that would at least be visible, an indefinite
    # hang here just looks like "the backtest never finishes."
    df = yf.download(ticker, start=start, end=end, interval="1d",
                      progress=False, auto_adjust=False, timeout=30)
    if df is None or df.empty:
        raise ValueError(f"{ticker}: yfinance returned no historical bars for {start}..{end}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.reset_index()
    df.columns = [str(c).lower() for c in df.columns]
    date_col = "date" if "date" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.date
    keep = ["date", "open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]].dropna().sort_values("date").reset_index(drop=True)
    return df


def fetch_vix_series(start: str, end: str) -> pd.DataFrame:
    """Real historical VIX close, keyed by date - point-in-time safe (VIX is
    a published daily index, never restated after the fact)."""
    return fetch_daily_bars("^VIX", start, end)[["date", "close"]].rename(columns={"close": "vix"})


# ---------------------------------------------------------------------------
# 2. Point-in-time TickerData construction
# ---------------------------------------------------------------------------

def build_ticker_data_asof(ticker: str, window_df: pd.DataFrame, quote_type: str = "EQUITY",
                            sector: str = "N/A"):
    """window_df: bars UP TO AND INCLUDING the simulated date, already
    truncated by the caller - this function never looks past the last row,
    which is the entire point-in-time guarantee."""
    from engine.ticker_analyzer import TickerData

    td = TickerData(ticker=ticker)
    last = window_df.iloc[-1]
    prev = window_df.iloc[-2] if len(window_df) > 1 else last
    td.price = float(last["close"])
    td.prev_close = float(prev["close"])
    td.change_pct = round((td.price / td.prev_close - 1) * 100, 2) if td.prev_close else 0.0
    td.volume = int(last["volume"])
    vol_window = window_df["volume"].tail(20) if len(window_df) >= 5 else window_df["volume"]
    avg_vol = float(vol_window.mean()) if len(vol_window) else float(td.volume)
    td.avg_volume = int(avg_vol)
    td.volume_ratio = (td.volume / avg_vol) if avg_vol else 1.0
    # Synthetic tight spread - see module docstring's "known simplifications".
    td.bid = round(td.price * 0.9995, 4)
    td.ask = round(td.price * 1.0005, 4)
    td.quote_age_minutes = 0.0
    td.quote_age_is_measured = True  # the historical close IS "as of" this date, by construction
    td.days_to_earnings = 999        # no historical earnings calendar in Stage 1 - EARNINGS_RISK never fires
    td.sector = sector
    td.quote_type = quote_type
    td.data_sources = {"daily_bars": "historical_replay"}

    records = window_df.to_dict("records")
    _populate_indicators(td, records)
    return td


def _populate_indicators(td, records: list):
    """Always the real production code path - engine/ticker_analyzer.py's
    _calc_indicators itself transparently swaps between real pandas_ta and
    engine/ta_fallback.py's accessor (see module docstring), so there is no
    backtest-specific indicator math to maintain here at all."""
    from engine.ticker_analyzer import TickerAnalyzer
    TickerAnalyzer()._calc_indicators(td, records, intraday_data=None)
    # VWAP always stays at TickerData's dataclass default (0.0) / "stale" in
    # Stage 1 - _calc_indicators only computes it from intraday_data, which
    # this replay never supplies (see module docstring). Explicit here so a
    # reader doesn't have to infer it from the absence of a VWAP branch.
    if "VWAP" not in td.stale_indicators:
        td.stale_indicators = list(td.stale_indicators) + ["VWAP"]


# ---------------------------------------------------------------------------
# 3. Market-wide data + regime, point-in-time
# ---------------------------------------------------------------------------

def _opex_status_asof(d: date) -> str:
    """3rd-Friday-of-month OpEx calendar check for the SIMULATED date - pure
    calendar arithmetic, zero look-ahead risk regardless of when this code
    actually runs. Does NOT reuse engine/market_breadth.py's _opex_status()
    because that helper reads real wall-clock 'now', which would silently
    compute today's OpEx status for a trade dated a year ago."""
    cal = calendar.Calendar()
    fridays = sorted(dd for dd in cal.itermonthdates(d.year, d.month)
                      if dd.month == d.month and dd.weekday() == 4)
    if len(fridays) < 3:
        return "normal"
    third_friday = fridays[2]
    if third_friday - timedelta(days=4) <= d <= third_friday:
        return "opex_week"
    if third_friday < d <= third_friday + timedelta(days=3):
        return "post_opex"
    return "normal"


def build_market_data_asof(spy_window: pd.DataFrame, vix_close: float, asof_date: date) -> dict:
    """VIX is real historical data (point-in-time safe). Fear & Greed and
    sector-ETF breadth have no clean free historical source in Stage 1, so
    both sit at the SAME neutral defaults engine/market_breadth.py's own
    _NEUTRAL fallback already uses on a live data outage - not a new bias,
    the exact fallback behavior production already has when breadth is
    genuinely unavailable, just triggered here because Stage 1 doesn't fetch
    it at all rather than because a fetch failed."""
    spy_price = float(spy_window["close"].iloc[-1])
    spy_sma200 = float(spy_window["close"].rolling(200).mean().iloc[-1]) if len(spy_window) >= 200 else spy_price
    return {
        "vix": vix_close if vix_close is not None else 18.0,
        "fg_score": 50,
        "yield_spread_2s10s": 0.0,
        "upcoming_macro_event": "",
        "ad_ratio": 0.50, "mcclellan": 0.0,
        "pct_above_20ema": 50.0, "pct_above_50ema": 50.0,
        "breadth_acceleration": 0.0, "nh_nl_ratio": 1.0,
        "ad_slope_5d_positive": True, "spy_ad_aligned": True,
        "opex_status": _opex_status_asof(asof_date),
        "breadth_proxy_type": "unavailable_in_backtest", "breadth_coverage": 0,
        "breadth_stale": True, "ad_ratio_suspect": False,
        "spy_vs_200dma": (spy_price / spy_sma200) if spy_sma200 else 1.0,
    }


def compute_regime_asof(spy_window: pd.DataFrame, vix_close: float):
    from engine.regime_engine import calculate as regime_calculate
    spy_price = float(spy_window["close"].iloc[-1])
    spy_sma50 = float(spy_window["close"].rolling(50).mean().iloc[-1]) if len(spy_window) >= 50 else spy_price
    spy_sma200 = float(spy_window["close"].rolling(200).mean().iloc[-1]) if len(spy_window) >= 200 else spy_price
    return regime_calculate(spy_price, spy_sma50, spy_sma200, vix_close or 18.0, fg_score=50, ad_ratio=0.5)


# ---------------------------------------------------------------------------
# 4. Minimal DB shim - see module docstring's "known simplifications"
# ---------------------------------------------------------------------------

class _BacktestFakeDB:
    """Stand-in for storage.database.Database, injected ONLY for the two
    DB-coupled checks inside rules/hard_vetoes.py's check() (cooldown and
    already-open). Backed by the replay loop's own in-memory open-position
    tracker - ALREADY_OPEN reflects exactly what the backtest itself
    currently holds for that ticker, not a fake opinion. Cooldown is NOT
    simulated in Stage 1 - documented limitation, always reports clear."""

    def __init__(self, open_positions: dict):
        self._open = open_positions

    def ticker_in_cooldown(self, ticker):
        return False

    def get_open_position(self, ticker):
        return self._open.get(ticker)


def _patch_database(open_positions: dict):
    import storage.database as dbmod
    original = dbmod.Database
    dbmod.Database = lambda *a, **k: _BacktestFakeDB(open_positions)
    return original


def _unpatch_database(original):
    import storage.database as dbmod
    dbmod.Database = original


def _patch_market_breadth():
    """engine/ticker_data_adapter.py's ticker_to_dict() unconditionally calls
    engine/market_breadth.py's get_spy_return_1m() (for rs_vs_spy_1m) and
    get_sector_return() (for sector_rs_1d/1m) - both of which, on a cache
    miss, call a real live yfinance MCP fetch of the 11 sector ETFs + SPY.
    In a Stage 1 replay this fetch is guaranteed useless even when it
    succeeds (it would return TODAY's sector prices, not the simulated
    date's - exactly the look-ahead bias the whole replay exists to avoid),
    and when the MCP is unreachable (e.g. this sandbox) each failed attempt
    retries for several seconds with NO negative-caching, making a
    multi-hundred-day backtest impractically slow. Both functions already
    document 'returns neutral (0.0) when data is unavailable' as their
    designed fallback - this patch just returns that same neutral value
    immediately instead of paying for a network round trip guaranteed to be
    wrong or absent. Net effect (documented in the module docstring):
    rs_vs_spy_1m (TREND) and sector_rs_1d/1m (EXTERNAL/SENTIMENT_MACRO) are
    neutral/unavailable in Stage 1, same honest-gap treatment as the rest of
    EXTERNAL/SENTIMENT_MACRO/MARKET_BREADTH."""
    import engine.market_breadth as mb
    originals = (mb.get_spy_return_1m, mb.get_sector_return)
    mb.get_spy_return_1m = lambda: 0.0
    mb.get_sector_return = lambda sector_name: {"return_1d": 0.0, "return_1m": 0.0}
    return originals


def _unpatch_market_breadth(originals):
    import engine.market_breadth as mb
    mb.get_spy_return_1m, mb.get_sector_return = originals


# ---------------------------------------------------------------------------
# 5. Simplified forward-exit simulation
# ---------------------------------------------------------------------------

_ATR_MULT_BY_TIER = {"strong": 2.0, "standard": 1.5, "weak": 1.2}  # SWING defaults, config.yaml stop_machine.SWING


def _entry_tier(pct_score: float) -> str:
    if pct_score >= 85:
        return "strong"
    if pct_score >= 65:
        return "standard"
    return "weak"


def simulate_forward_exit(ticker_bars: pd.DataFrame, entry_idx: int, entry_price: float,
                           atr: float, pct_score: float, stop_loss_swing_pct: float,
                           r_multiple: float = 3.0, max_hold_days: int = 20) -> dict:
    """SIMPLIFIED exit model - see module docstring. Fixed ATR-tiered initial
    stop (same multipliers config.yaml's stop_machine.SWING uses) + fixed
    R-multiple take-profit (config.yaml's sell_rules.take_profit.r_multiple),
    checked bar-by-bar. If a bar's low and high both cross their respective
    levels the same day, the stop is assumed to hit first (standard,
    conservative backtesting convention)."""
    tier = _entry_tier(pct_score)
    atr_mult = _ATR_MULT_BY_TIER[tier]
    atr_dist = atr_mult * atr if atr > 0 else entry_price * (stop_loss_swing_pct / 100.0)
    cap_dist = entry_price * (stop_loss_swing_pct / 100.0)
    stop_dist = min(atr_dist, cap_dist) if cap_dist > 0 else atr_dist
    stop_price = entry_price - stop_dist
    risk_per_share = entry_price - stop_price
    target_price = entry_price + risk_per_share * r_multiple

    mae_pct, mfe_pct = 0.0, 0.0
    n = len(ticker_bars)
    last_idx = entry_idx
    for i in range(entry_idx + 1, min(entry_idx + 1 + max_hold_days, n)):
        bar = ticker_bars.iloc[i]
        low_, high_, close_ = float(bar["low"]), float(bar["high"]), float(bar["close"])
        mae_pct = min(mae_pct, (low_ / entry_price - 1) * 100)
        mfe_pct = max(mfe_pct, (high_ / entry_price - 1) * 100)
        last_idx = i
        if low_ <= stop_price:
            return _exit_result(i, stop_price, entry_price, entry_idx, mae_pct, mfe_pct, "stop_loss", tier)
        if high_ >= target_price:
            return _exit_result(i, target_price, entry_price, entry_idx, mae_pct, mfe_pct, "take_profit", tier)

    exit_price = float(ticker_bars.iloc[last_idx]["close"])
    return _exit_result(last_idx, exit_price, entry_price, entry_idx, mae_pct, mfe_pct, "time_based_close", tier)


def _exit_result(exit_idx, exit_price, entry_price, entry_idx, mae_pct, mfe_pct, reason, tier):
    outcome_pct = (exit_price / entry_price - 1) * 100
    return {
        "exit_idx": exit_idx, "exit_price": round(exit_price, 4),
        "outcome_pct": round(outcome_pct, 2), "mae_pct": round(mae_pct, 2), "mfe_pct": round(mfe_pct, 2),
        "hold_days": exit_idx - entry_idx, "exit_reason": reason, "entry_tier": tier,
    }


# ---------------------------------------------------------------------------
# 6. Main replay loop
# ---------------------------------------------------------------------------

def run_replay(tickers: list, start: str, end: str, cfg: dict,
                warmup_days: int = 260, max_hold_days: int = 20) -> dict:
    """Runs the REAL rules/hard_vetoes.py + rules/swing_buy_rules.py (which
    internally runs dynamic_thresholds/execution_quality/probabilistic_decision)
    against historical daily bars for each ticker independently. `warmup_days`
    of history are fetched BEFORE `start` so SMA200/weekly-trend rules have
    real data on day 1 of the actual replay window rather than reading as
    stale for the first ~10 months."""
    from rules import hard_vetoes, swing_buy_rules

    fetch_start = (pd.to_datetime(start) - pd.Timedelta(days=int(warmup_days * 1.6))).strftime("%Y-%m-%d")
    spy_bars = fetch_daily_bars("SPY", fetch_start, end)
    vix_series = fetch_vix_series(fetch_start, end)
    vix_by_date = dict(zip(vix_series["date"], vix_series["vix"]))

    risk_level = cfg.get("risk_level", "MODERATE")
    base_threshold = (cfg.get("risk", {}).get(risk_level, {}) or {}).get("buy_score_threshold_pct", 60)
    stop_loss_swing_pct = (cfg.get("risk", {}).get(risk_level, {}) or {}).get("stop_loss_swing_pct", 5.0)
    r_multiple = (cfg.get("sell_rules", {}).get("take_profit", {}) or {}).get("r_multiple", 3.0)

    open_positions: dict = {}
    original_db = _patch_database(open_positions)
    original_breadth = _patch_market_breadth()
    all_trades = []
    veto_counts: dict = {}
    n_scored = 0
    # Score-distribution telemetry (2026-07-24, added alongside the
    # SENTIMENT_MACRO/MARKET_BREADTH availability fix in
    # rules/swing_buy_rules.py): a "0 trades" run previously gave zero
    # visibility into HOW close candidates got - every scored score/
    # threshold pair was computed and then discarded. Tracking every
    # candidate's final_pct here (2997 rows is trivial memory) lets
    # run_and_persist() report a real distribution + the closest near-misses
    # even when nothing cleared the bar, so a future change's actual impact
    # on the score distribution is auditable instead of only visible as a
    # binary trade-count.
    _all_scores: list = []
    _near_misses: list = []

    try:
        for ticker in tickers:
            try:
                bars = fetch_daily_bars(ticker, fetch_start, end)
            except Exception as e:
                logger.warning(f"{ticker}: skipped, bar fetch failed ({e})")
                continue

            start_date = pd.to_datetime(start).date()
            start_idx = int((bars["date"] >= start_date).idxmax()) if (bars["date"] >= start_date).any() else None
            if start_idx is None or start_idx < 20:
                logger.warning(f"{ticker}: not enough warmup history before {start}, skipping")
                continue

            i = start_idx
            n = len(bars)
            open_positions.pop(ticker, None)
            while i < n:
                asof_date = bars["date"].iloc[i]
                window = bars.iloc[: i + 1]
                spy_window = spy_bars[spy_bars["date"] <= asof_date]
                if len(spy_window) < 50:
                    i += 1
                    continue
                vix_close = vix_by_date.get(asof_date)

                td = build_ticker_data_asof(ticker, window)
                ticker_dict = _td_to_dict(td)
                market_dict = build_market_data_asof(spy_window, vix_close, asof_date)
                regime = compute_regime_asof(spy_window, vix_close)

                veto = hard_vetoes.check(ticker, ticker_dict, market_dict, cfg, mode="swing")
                if veto.vetoed:
                    veto_counts[veto.veto_code] = veto_counts.get(veto.veto_code, 0) + 1
                    i += 1
                    continue

                n_scored += 1
                result = swing_buy_rules.score(ticker_dict, market_dict, regime, cfg, mode="swing", db=None, ticker=ticker)
                _all_scores.append(result.final_score_pct)
                if not result.passed:
                    _near_misses.append({
                        "ticker": ticker, "date": str(asof_date),
                        "score_pct": round(result.final_score_pct, 2),
                        "threshold_pct": round(result.threshold, 2),
                        "deficit_pct": round(result.threshold - result.final_score_pct, 2),
                    })

                if result.passed:
                    entry_price = td.price
                    exit_info = simulate_forward_exit(
                        bars, i, entry_price, td.atr, result.final_score_pct,
                        stop_loss_swing_pct, r_multiple, max_hold_days,
                    )
                    trade = {
                        "ticker": ticker,
                        "entry_date": str(asof_date),
                        "entry_price": round(entry_price, 4),
                        "exit_date": str(bars["date"].iloc[exit_info["exit_idx"]]),
                        "buy_score_pct": round(result.final_score_pct, 2),
                        "threshold_pct": round(result.threshold, 2),
                        "regime": regime.dominant_regime,
                        "rules_fired": result.rules_fired,
                        **exit_info,
                    }
                    all_trades.append(trade)
                    i = exit_info["exit_idx"] + 1
                else:
                    i += 1
    finally:
        _unpatch_database(original_db)
        _unpatch_market_breadth(original_breadth)

    return {
        "trades": all_trades,
        "n_scored": n_scored,
        "veto_counts": veto_counts,
        "summary": summarize(all_trades),
        "score_distribution": _summarize_scores(_all_scores, _near_misses),
        "config": {
            "risk_level": risk_level, "base_threshold_pct": base_threshold,
            "stop_loss_swing_pct": stop_loss_swing_pct, "r_multiple": r_multiple,
            "mode": "swing", "max_hold_days": max_hold_days,
            "pandas_ta_used": _pandas_ta_available(),
        },
    }


def _summarize_scores(all_scores: list, near_misses: list) -> dict:
    """Score-distribution telemetry (2026-07-24) - see run_replay's
    _all_scores/_near_misses comment. Computed even when n_trades == 0, so a
    'nothing cleared the bar' run still reports HOW close it got, not just
    the binary trade count. near_misses is sorted by deficit_pct (threshold
    minus score) ascending - the top entries are the candidates that came
    closest to actually buying."""
    if not all_scores:
        return {"n_scored": 0}
    near_misses_sorted = sorted(near_misses, key=lambda r: r["deficit_pct"])
    return {
        "n_scored": len(all_scores),
        "max_score_pct": round(max(all_scores), 2),
        "mean_score_pct": round(sum(all_scores) / len(all_scores), 2),
        "median_score_pct": round(float(np.median(all_scores)), 2),
        "pct_scoring_ge_40": round(100.0 * sum(1 for s in all_scores if s >= 40) / len(all_scores), 1),
        "pct_scoring_ge_45": round(100.0 * sum(1 for s in all_scores if s >= 45) / len(all_scores), 1),
        "pct_scoring_ge_48": round(100.0 * sum(1 for s in all_scores if s >= 48) / len(all_scores), 1),
        "closest_near_misses": near_misses_sorted[:15],
    }


def _td_to_dict(td) -> dict:
    """engine/ticker_data_adapter.ticker_to_dict() needs a MarketContextData-ish
    `mkt` positional arg it never actually reads (only td and cfg are used
    for every field it builds - see that file), so a lightweight None stand-in
    keeps this a real call to the unmodified adapter rather than a
    reimplementation of its dict shape."""
    from engine.ticker_data_adapter import ticker_to_dict
    d = ticker_to_dict(td, None, {})
    # sentiment_macro_data_available (2026-07-24, zero-trades follow-up):
    # this replay never fetches news/insider/short-float/real sector-ETF
    # history (see the module docstring's SCOPE section - no free
    # point-in-time source exists for any of them), so rules/
    # swing_buy_rules.py's SENTIMENT_MACRO bucket was silently scoring the
    # fixed neutral defaults in market_dict (fg_score=50 etc.) as if they
    # were measured evidence instead of a data gap. Explicitly False here
    # tells scoring to treat SENTIMENT_MACRO the same way EXTERNAL already
    # was - weight redistributed to the buckets that DO have real
    # point-in-time data (TREND/MOMENTUM/VOLUME_PA), not scored as neutral-
    # but-real. This key does not exist in live ticker_to_dict() output, so
    # every live caller keeps its old behavior (defaults to available=True
    # in rules/swing_buy_rules.py).
    d["sentiment_macro_data_available"] = False
    return d


def _pandas_ta_available() -> bool:
    """Reporting-only - see engine/ta_fallback.py's docstring for why this
    doesn't affect which code path _calc_indicators takes (that's decided
    once, at engine/ticker_analyzer.py import time, regardless of this
    check)."""
    try:
        import pandas_ta  # noqa: F401
        return True
    except Exception:
        return False


def summarize(trades: list) -> dict:
    if not trades:
        return {"n_trades": 0}
    outcomes = [t["outcome_pct"] for t in trades]
    wins = [o for o in outcomes if o > 0]
    losses = [o for o in outcomes if o <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    by_reason: dict = {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
    return {
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_outcome_pct": round(sum(outcomes) / len(outcomes), 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / len(trades), 1),
        "exit_reason_counts": by_reason,
    }


# ---------------------------------------------------------------------------
# 7. Single entry point for CLI / weekly scheduler trigger / UI "Run Now"
#    button - all three call THIS function so there's exactly one code path
#    that runs a replay and persists it, not three slightly-different copies.
# ---------------------------------------------------------------------------

def run_and_persist(tickers: list, start: str, end: str, cfg: dict, db=None,
                     triggered_by: str = "manual", out_root: str = None,
                     warmup_days: int = 260, max_hold_days: int = 20) -> dict:
    """Runs run_replay() and writes results to BOTH the filesystem
    (output/backtest_results/<timestamp>/results.json + summary.md - always,
    so this works even with no DB reachable) and, if `db` is given, the
    backtest_runs table (so the Learning tab's "Run Backtest Now" button and
    the weekly scheduler trigger have something queryable without re-reading
    files off disk). A row is inserted with status='running' BEFORE the
    replay starts (so a concurrent request can see one is already in
    progress - see engine/backtest_loop.py's guard) and updated to
    'completed' or 'failed' when it finishes - never left dangling on an
    exception."""
    import json
    from datetime import datetime as _datetime
    from pathlib import Path

    run_id = None
    if db is not None:
        try:
            run_id = db.log_backtest_run_start(tickers, start, end, triggered_by=triggered_by)
        except Exception as e:
            logger.warning(f"Could not log backtest_runs start row (continuing file-only): {e}")

    try:
        result = run_replay(tickers, start, end, cfg, warmup_days=warmup_days, max_hold_days=max_hold_days)
    except Exception as e:
        logger.error(f"Backtest replay failed: {e}", exc_info=True)
        if db is not None and run_id is not None:
            try:
                db.log_backtest_run_failed(run_id, str(e))
            except Exception:
                pass
        raise

    # 2026-07-24 bugfix: this used to read `from datetime import date as
    # _date` then `_date.today().strftime("%Y%m%d_%H%M%S")` - a plain date
    # has no time component, so %H%M%S always rendered as "000000" and every
    # same-day run silently overwrote the previous run's output_dir (lost
    # results.json/summary.md, no error). Using datetime.now() gives each
    # run its own folder as originally intended.
    ts = _datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(out_root) if out_root else Path("output") / "backtest_results" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, default=str))
    (out_dir / "summary.md").write_text(_render_summary_md(tickers, start, end, result))

    if db is not None and run_id is not None:
        try:
            db.log_backtest_run_complete(
                run_id, result["n_scored"], result["veto_counts"], result["summary"],
                result["trades"], result["config"], output_dir=str(out_dir),
            )
        except Exception as e:
            logger.warning(f"Could not log backtest_runs completion row: {e}")

    result["output_dir"] = str(out_dir)
    result["run_id"] = run_id
    return result


def _render_summary_md(tickers: list, start: str, end: str, result: dict) -> str:
    s, cfg_used = result["summary"], result["config"]
    sd = result.get("score_distribution", {}) or {}
    lines = [
        "# Stage 1 Historical Replay - Summary", "",
        f"**Window:** {start} to {end} | **Tickers:** {', '.join(tickers)}",
        f"**Risk level:** {cfg_used['risk_level']} (base threshold {cfg_used['base_threshold_pct']}%) | "
        f"**Mode:** {cfg_used['mode']} | **Max hold:** {cfg_used['max_hold_days']} trading days",
        f"**Indicator backend:** {'pandas_ta (full production fidelity)' if cfg_used['pandas_ta_used'] else 'engine/ta_fallback.py (pandas_ta not installed)'}",
        "", "## Candidates scored", "",
        f"- Candidate-days scored (passed all vetoes): {result['n_scored']}",
        f"- Veto breakdown: {result['veto_counts'] or 'none fired'}",
    ]
    # Score-distribution telemetry (2026-07-24) - shows HOW close the run
    # got even when nothing traded, instead of just the binary trade count.
    if sd.get("n_scored"):
        lines += [
            f"- Max score reached: {sd['max_score_pct']}% | Mean: {sd['mean_score_pct']}% | "
            f"Median: {sd['median_score_pct']}%",
            f"- % of scored candidates >=40% / >=45% / >=48%: "
            f"{sd['pct_scoring_ge_40']}% / {sd['pct_scoring_ge_45']}% / {sd['pct_scoring_ge_48']}%",
        ]
        top = sd.get("closest_near_misses") or []
        if top:
            lines += ["", "### Closest near-misses (smallest score-vs-threshold deficit)", "",
                      "| Ticker | Date | Score % | Threshold % | Deficit |", "|---|---|---|---|---|"]
            for r in top[:10]:
                lines.append(f"| {r['ticker']} | {r['date']} | {r['score_pct']} | "
                             f"{r['threshold_pct']} | {r['deficit_pct']} |")
    lines += ["", "## Trade results", ""]
    if not s.get("n_trades"):
        lines.append(
            "No trades cleared the dynamic threshold in this window - see "
            "engine/backtest_engine.py's module docstring on Stage 1's scope "
            "(analyst/news/insider/short-float/real breadth history all unavailable - "
            "SENTIMENT_MACRO/MARKET_BREADTH/EXTERNAL weight is redistributed to "
            "TREND/MOMENTUM/VOLUME_PA rather than scored as neutral-but-real, see "
            "rules/swing_buy_rules.py's BUCKET AVAILABILITY section) before assuming this is a bug. "
            "See the near-misses table above for how close candidates actually got."
        )
    else:
        lines += [
            "| Metric | Value |", "|---|---|",
            f"| Trades | {s['n_trades']} |", f"| Win rate | {s['win_rate']}% |",
            f"| Avg outcome | {s['avg_outcome_pct']}% |", f"| Profit factor | {s.get('profit_factor')} |",
            f"| Avg hold | {s['avg_hold_days']} days |", f"| Exit reasons | {s['exit_reason_counts']} |",
        ]
    return "\n".join(lines)
