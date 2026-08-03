"""Intraday momentum backtest (2026-07-30) - Zarattini/Aziz/Barbon "Beat the
Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)" (SFI
Research Paper 24-97), scoped in docs/
intraday_momentum_scoping_2026-07-30.md. Phase 1 of that doc: a new,
self-contained, data-source-agnostic backtest module - takes a plain
OHLCV-with-volume minute-bar DataFrame, returns trades. Does NOT touch
engine/backtest_engine.py, rules/, engine/regime_engine.py, or any
live/paper execution path - same scope boundary as every other addition
this session.

STRATEGY, as documented in the paper's public abstract plus an independent
paper-review breakdown (CXO Advisory's methodology summary - the full
43-page paper PDF was not accessed, only the abstract and outside
summaries):
  1. NOISE BAND. Each minute, upper/lower boundaries = today's open price
     times (1 +/- sigma_t), where sigma_t is the trailing-N-day average of
     |close/open - 1| AT THAT SAME MINUTE OFFSET since the open (captures
     the U-shaped intraday vol profile - noisier near open/close, calmer
     midday - rather than a flat all-day band). Adjusted for the overnight
     gap: a gap-down pushes the upper bound further up, a gap-up pushes the
     lower bound further down (so a stale band doesn't fire a breakout
     purely off yesterday's close-to-today's-open jump).
  2. ENTRY. At each clock hour/half-hour, if price has moved above (below)
     the band, go long (short).
  3. TRAILING STOP - two variants, both checked only at clock marks:
     - "band": exit at the OPPOSITE band (lower for a long, upper for a
       short); paper explicitly reverses into the opposite trade when this
       triggers, since crossing that boundary is itself a fresh signal in
       the other direction.
     - "band_or_vwap": exit level = max(upper_band, VWAP) for a long,
       min(lower_band, VWAP) for a short. VWAP is a genuinely
       price-trailing level (today's running average); the static
       sigma-band is not. This lets VWAP take over as the effective stop
       once a trend has run far enough that VWAP has caught up past the
       band - see this module's git history / PR description for the
       reasoning chain on why the paper's literal wording ("higher of the
       upper boundary or VWAP, for longs") makes sense read this way.
  4. EOD FLATTEN. Any open position is force-closed at the session's last
     bar, every day, no overnight exposure.

WHAT IS EXPLICIT VS AMBIGUOUS IN THE SOURCE MATERIAL - flagged rather than
silently resolved one way, per this session's standing practice (compare
engine/regime_timed_backtest.py's HONEST FRAMING section):
  - FILL TIMING is NOT specified by the paper and was flagged by an
    independent reviewer as the single biggest open question ("what are
    the execution assumptions? do you assume instant fill once a signal is
    generated?"). This module makes it an explicit, named, first-class
    parameter (`fill_timing`) rather than picking one interpretation and
    hiding it:
      - "same_bar": fill AT the signal bar's own close - the OPTIMISTIC
        reading, assumes zero reaction latency.
      - "next_bar": fill at the FOLLOWING bar's open - the CONSERVATIVE,
        realistically-achievable reading. Per the phase-1 success
        criterion in the scoping doc, "next_bar" is the one that has to
        clear the bar; "same_bar" is reported alongside for comparison
        only, never substituted in if next_bar fails.
    Applies uniformly to entries, band-stop reversals, and vwap-stop exits
    - the same detection-vs-execution gap exists in all three.
  - The noise-band MULTIPLIER (N x sigma instead of 1 x sigma) is exposed
    as `band_multiplier` - the same reviewer's note that the paper explores
    this as a real knob, not a fixed constant.
  - Position sizing (vol-target, up to 4x leverage) is computed and
    attached to each trade as metadata (`leverage_x`) but is NOT yet
    compounded into a portfolio equity curve - see compute_position_
    sizing()'s docstring. A $/share cost model (engine/
    intraday_execution_costs.py) makes outcome_pct independent of position
    size, so leverage doesn't change any trade's OWN outcome_pct; it would
    only matter for a portfolio-level equity simulation, which is a
    natural next step (mirroring scripts/equity_curve.py's role for the
    daily engine) but out of scope for this first module.

DATA BLOCKER (2026-07-30, logged the day this module was written): this
environment has no working path to multi-year real 1-minute SPY history -
the bash sandbox has no network route to data.alpaca.markets/
api.alpaca.markets (allowlisted to package registries only), and the
available Robinhood-linked market-data MCP connector's minute-bar history
is real for only about the trailing month (verified: real, volume-bearing
bars as of 2026-07-01; a flat, zero-volume, `interpolated: true` placeholder
price for every date tested from 2026-06-15 back through 2016). This module
is fully covered by SYNTHETIC-data unit tests (tests/
test_intraday_momentum_backtest.py) proving the math is correct, but it has
NOT been run against a real multi-year dataset. Do not treat any run of
this module as a validated phase-1 result until it has been - see the
scoping doc's phase-1 success criterion (next_bar fills + per-share costs +
a frozen walk-forward split, on real data).
"""
import logging

import numpy as np
import pandas as pd

from engine import intraday_execution_costs as costs

logger = logging.getLogger(__name__)

EASTERN = "America/New_York"
SESSION_OPEN_MINUTES = 9 * 60 + 30   # 9:30 ET
SESSION_CLOSE_MINUTES = 16 * 60      # 16:00 ET

REQUIRED_RAW_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


# ---------------------------------------------------------------------------
# 1. Data prep - normalizes ANY source's raw minute bars into this module's
#    schema. Deliberately does not know or care whether the caller's source
#    was Alpaca, the Robinhood-style historicals MCP, or a FirstRate Data
#    CSV - see the module docstring's DATA BLOCKER note for why this
#    separation matters right now.
# ---------------------------------------------------------------------------

def prepare_bars(raw_bars: pd.DataFrame, tz_source: str = "UTC") -> pd.DataFrame:
    """Normalizes a raw minute-bar DataFrame into: tz-aware 'timestamp' in
    America/New_York, 'date' (session date, a python date), OHLCV columns,
    sorted ascending, filtered to the regular session (9:30-16:00 ET,
    inclusive of 9:30, exclusive of 16:00 - the 16:00 bar itself, if a
    source provides one, is the closing auction print and is dropped here
    since it isn't a tradable intraday bar under this strategy's rules).

    Does NOT forward-fill, interpolate, or otherwise manufacture missing
    minutes - a real feed has real gaps (a minute with no trade), and
    fabricating a flat bar to fill one would corrupt both the noise-band
    calculation (which needs the return-since-open to reflect what actually
    happened) and the VWAP (which needs real volume). Placeholder/
    synthetic bars from a source that lacks real history for a date (see
    module docstring) must be filtered out by the CALLER before this
    function ever sees them, not silently accepted here.

    Raises ValueError if raw_bars is missing any required column, rather
    than proceeding on a partial schema and failing confusingly deeper in
    the pipeline."""
    missing = REQUIRED_RAW_COLUMNS - set(raw_bars.columns)
    if missing:
        raise ValueError(f"prepare_bars: raw_bars missing required column(s): {sorted(missing)}")
    df = raw_bars.copy()
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(tz_source)
    df["timestamp"] = ts.dt.tz_convert(EASTERN)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    df["date"] = df["timestamp"].dt.date
    minute_of_day = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
    df = df[(minute_of_day >= SESSION_OPEN_MINUTES) & (minute_of_day < SESSION_CLOSE_MINUTES)].reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _minutes_since_open(ts_series: pd.Series) -> pd.Series:
    minute_of_day = ts_series.dt.hour * 60 + ts_series.dt.minute
    return minute_of_day - SESSION_OPEN_MINUTES


# ---------------------------------------------------------------------------
# 2. Noise bands - see module docstring sec 1. No look-ahead: sigma_t for
#    day D at a given minute offset is built from days STRICTLY BEFORE D
#    only (a groupby+rolling+shift(1), mirroring the no-lookahead discipline
#    engine/regime_timed_backtest.py's compute_regime_timeline() documents
#    for its own trailing SMA).
# ---------------------------------------------------------------------------

def compute_noise_bands(df: pd.DataFrame, lookback_days: int = 14, band_multiplier: float = 1.0) -> pd.DataFrame:
    """Adds minute_since_open, day_open, abs_return_since_open, sigma_t,
    prior_close, gap, upper_band, lower_band.

    Days without `lookback_days` full PRIOR sessions get sigma_t = NaN -
    those bars produce no tradable signal (the entry/exit scan skips NaN
    bands) rather than a fabricated number from a partial window. This
    matches this codebase's existing warmup posture elsewhere (e.g.
    ticker_data_adapter's PLACEHOLDER-tagging, regime_timed_backtest's
    pre-warmup default) - don't fake a signal, don't crash either.

    band_multiplier is the paper reviewer's "N x sigma" knob - default 1.0
    reproduces the paper's base case; larger values -> wider bands ->
    fewer, higher-conviction breakouts."""
    if df.empty:
        return df.assign(minute_since_open=[], day_open=[], abs_return_since_open=[],
                          sigma_t=[], prior_close=[], gap=[], upper_band=[], lower_band=[])
    df = df.copy()
    df["minute_since_open"] = _minutes_since_open(df["timestamp"])
    df["day_open"] = df.groupby("date")["open"].transform("first")
    df["abs_return_since_open"] = (df["close"] / df["day_open"] - 1).abs()

    pivot = df.pivot_table(index="date", columns="minute_since_open",
                            values="abs_return_since_open", aggfunc="last")
    sigma = pivot.rolling(lookback_days, min_periods=lookback_days).mean().shift(1)
    sigma_long = sigma.stack(future_stack=True).rename("sigma_t").reset_index()
    sigma_long.columns = ["date", "minute_since_open", "sigma_t"]
    df = df.merge(sigma_long, on=["date", "minute_since_open"], how="left")

    daily_close = df.groupby("date")["close"].last()
    session_dates = sorted(df["date"].unique())
    prior_close_map, prev_close = {}, None
    for d in session_dates:
        prior_close_map[d] = prev_close
        prev_close = daily_close.loc[d]
    df["prior_close"] = df["date"].map(prior_close_map)
    df["gap"] = df["day_open"] - df["prior_close"]

    half_width = df["sigma_t"] * band_multiplier
    df["upper_band"] = df["day_open"] * (1 + half_width)
    df["lower_band"] = df["day_open"] * (1 - half_width)
    gap_down_extra = (-df["gap"]).clip(lower=0)   # a gap DOWN pushes the upper bound UP further
    gap_up_extra = df["gap"].clip(lower=0)        # a gap UP pushes the lower bound DOWN further
    df["upper_band"] = df["upper_band"] + gap_down_extra
    df["lower_band"] = df["lower_band"] - gap_up_extra
    return df


def compute_intraday_vwap(df: pd.DataFrame) -> pd.Series:
    """Standard cumulative typical-price VWAP, reset every session day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv_cum = (typical * df["volume"]).groupby(df["date"]).cumsum()
    vol_cum = df["volume"].groupby(df["date"]).cumsum().replace(0, np.nan)
    return (pv_cum / vol_cum).rename("vwap")


def _is_clock_mark(ts_series: pd.Series) -> pd.Series:
    """True at each :00 and :30 bar - the paper's entry/stop-check cadence."""
    return ts_series.dt.minute.isin([0, 30])


# ---------------------------------------------------------------------------
# 3. Position sizing (vol-target, capped leverage) - computed and attached
#    to trades as metadata only; see module docstring for why it doesn't
#    yet feed a portfolio equity curve.
# ---------------------------------------------------------------------------

def compute_trailing_realized_vol_pct(daily_closes: pd.DataFrame, lookback_days: int = 14) -> pd.Series:
    """daily_closes: DataFrame with 'date' and 'close', one row per session
    (a DAILY bar series, distinct from the minute bars this module mostly
    works with - the paper sizes off "actual SPY volatility over the past
    14 trading days," a close-to-close measure). Returns a Series of
    trailing realized vol in %, indexed by date, using only days STRICTLY
    BEFORE each date (shift(1) - no lookahead, same discipline as
    compute_noise_bands' sigma_t)."""
    d = daily_closes.sort_values("date").reset_index(drop=True)
    rets = d["close"].pct_change()
    vol_pct = rets.rolling(lookback_days, min_periods=lookback_days).std().shift(1) * 100
    return pd.Series(vol_pct.values, index=d["date"], name="realized_vol_pct")


def leverage_multiplier(realized_vol_pct, target_daily_vol_pct: float = 2.0, max_leverage: float = 4.0) -> float:
    """min(max_leverage, target/realized) - a calm market (low realized
    vol) sizes UP toward the vol target, capped at max_leverage; a volatile
    market sizes DOWN. Missing/non-finite/non-positive realized vol
    (warmup, or a zero-vol data glitch) defaults to 1.0x - a neutral,
    non-fabricated fallback, not an attempt to guess a real value."""
    if realized_vol_pct is None or not np.isfinite(realized_vol_pct) or realized_vol_pct <= 0:
        return 1.0
    return float(min(max_leverage, target_daily_vol_pct / realized_vol_pct))


# ---------------------------------------------------------------------------
# 4. Single-session simulation. A position ALWAYS fully closes by EOD (the
#    paper's own rule), so - unlike the daily swing engine - state never
#    carries across days. Each day is simulated independently.
# ---------------------------------------------------------------------------

def _resolve_fill(day_df: pd.DataFrame, signal_pos: int, fill_timing: str):
    """Returns (fill_row_pos, fill_price) or None if no fill is achievable
    (fill_timing='next_bar' requested on the session's last bar - no bar
    left to fill on. Returning None here means the signal is simply not
    acted on, rather than forcing an artificial after-hours fill - see
    module docstring's FILL TIMING section)."""
    if fill_timing == "same_bar":
        row = day_df.iloc[signal_pos]
        return signal_pos, float(row["close"])
    if fill_timing == "next_bar":
        if signal_pos + 1 >= len(day_df):
            return None
        row = day_df.iloc[signal_pos + 1]
        return signal_pos + 1, float(row["open"])
    raise ValueError(f"fill_timing must be 'same_bar' or 'next_bar', got {fill_timing!r}")


def simulate_day(day_df: pd.DataFrame, fill_timing: str = "next_bar", stop_mode: str = "band",
                  leverage_x: float = 1.0, commission: float = costs.DEFAULT_PER_SHARE_COMMISSION,
                  slippage: float = costs.DEFAULT_PER_SHARE_SLIPPAGE) -> list:
    """day_df: ONE session's bars, already carrying upper_band/lower_band/
    vwap columns, sorted ascending, index reset (positional access via
    .iloc throughout - callers must reset_index per day before calling).

    Returns a list of trade dicts for this single day. A day can contain
    more than one trade (band-stop reversals, or a fresh breakout after an
    earlier trade's vwap-stop exit)."""
    if day_df.empty:
        return []
    trades = []
    clock_mark = _is_clock_mark(day_df["timestamp"]).values
    n = len(day_df)
    position = None  # {"direction", "entry_pos", "entry_price"}

    def _open(direction, at_pos):
        fill = _resolve_fill(day_df, at_pos, fill_timing)
        if fill is None:
            return None
        fill_pos, fill_price = fill
        return {"direction": direction, "entry_pos": fill_pos, "entry_price": fill_price}

    def _close(pos, at_pos, exit_reason):
        fill = _resolve_fill(day_df, at_pos, fill_timing)
        if fill is None:
            # No bar left to fill an exit on - EOD flatten below will
            # close it at the literal last bar's close instead.
            return None
        fill_pos, fill_price = fill
        cost_info = costs.apply_execution_costs(pos["entry_price"], fill_price, pos["direction"],
                                                  commission=commission, slippage=slippage)
        entry_row = day_df.iloc[pos["entry_pos"]]
        exit_row = day_df.iloc[fill_pos]
        return {
            "date": str(day_df["date"].iloc[0]),
            "direction": pos["direction"],
            "entry_time": str(entry_row["timestamp"]),
            "entry_price": round(pos["entry_price"], 4),
            "exit_time": str(exit_row["timestamp"]),
            "exit_price": round(fill_price, 4),
            "exit_reason": exit_reason,
            "hold_minutes": int((exit_row["timestamp"] - entry_row["timestamp"]).total_seconds() // 60),
            "leverage_x": round(leverage_x, 3),
            **cost_info,
        }

    i = 0
    while i < n:
        if not clock_mark[i] or pd.isna(day_df["upper_band"].iloc[i]) or pd.isna(day_df["lower_band"].iloc[i]):
            i += 1
            continue
        close_i = float(day_df["close"].iloc[i])
        upper_i = float(day_df["upper_band"].iloc[i])
        lower_i = float(day_df["lower_band"].iloc[i])
        vwap_i = day_df["vwap"].iloc[i]

        if position is None:
            if close_i > upper_i:
                position = _open("long", i)
            elif close_i < lower_i:
                position = _open("short", i)
            i += 1
            continue

        # Position open - evaluate the trailing stop for this stop_mode.
        if stop_mode == "band":
            stop_hit = (position["direction"] == "long" and close_i <= lower_i) or \
                       (position["direction"] == "short" and close_i >= upper_i)
        elif stop_mode == "band_or_vwap":
            if pd.isna(vwap_i):
                stop_hit = False
            elif position["direction"] == "long":
                stop_level = max(upper_i, float(vwap_i))
                stop_hit = close_i < stop_level
            else:
                stop_level = min(lower_i, float(vwap_i))
                stop_hit = close_i > stop_level
        else:
            raise ValueError(f"stop_mode must be 'band' or 'band_or_vwap', got {stop_mode!r}")

        if stop_hit:
            trade = _close(position, i, "trailing_stop")
            if trade is not None:
                trades.append(trade)
                position = None
                # "band" mode reverses immediately - crossing the opposite
                # boundary IS the opposite entry signal too. band_or_vwap
                # does not auto-reverse (a VWAP cross isn't necessarily
                # also a fresh boundary breakout) - it waits for the
                # ordinary entry scan on a later clock mark.
                if stop_mode == "band":
                    reverse_dir = "short" if trade["direction"] == "long" else "long"
                    position = _open(reverse_dir, i)
            else:
                position = None  # could not fill the exit - EOD flatten handles it
        i += 1

    if position is not None:
        last = day_df.iloc[-1]
        cost_info = costs.apply_execution_costs(position["entry_price"], float(last["close"]),
                                                  position["direction"], commission=commission, slippage=slippage)
        entry_row = day_df.iloc[position["entry_pos"]]
        trades.append({
            "date": str(day_df["date"].iloc[0]),
            "direction": position["direction"],
            "entry_time": str(entry_row["timestamp"]),
            "entry_price": round(position["entry_price"], 4),
            "exit_time": str(last["timestamp"]),
            "exit_price": round(float(last["close"]), 4),
            "exit_reason": "eod_flatten",
            "hold_minutes": int((last["timestamp"] - entry_row["timestamp"]).total_seconds() // 60),
            "leverage_x": round(leverage_x, 3),
            **cost_info,
        })

    return trades


# ---------------------------------------------------------------------------
# 5. Full replay across all days in the prepared bar set.
# ---------------------------------------------------------------------------

def run_backtest(raw_bars: pd.DataFrame, daily_closes: pd.DataFrame = None, lookback_days: int = 14,
                  band_multiplier: float = 1.0, fill_timing: str = "next_bar", stop_mode: str = "band",
                  target_daily_vol_pct: float = 2.0, max_leverage: float = 4.0, cfg: dict = None,
                  tz_source: str = "UTC") -> dict:
    """Top-level entry point. raw_bars: any source's minute bars (see
    prepare_bars). daily_closes: optional daily-bar DataFrame (date, close)
    for the vol-target sizing metadata - if omitted, every trade gets
    leverage_x=1.0 (neutral, see leverage_multiplier's docstring) rather
    than a fabricated vol estimate from minute data alone.

    cfg (optional) supplies commission_per_share/slippage_per_share via
    engine.intraday_execution_costs.get_cost_params - explicit commission/
    slippage kwargs to this function, if ever added, would take priority;
    today this function always reads cost params from cfg (or the paper's
    own defaults if cfg is None), matching this module's cost-model default
    posture (see intraday_execution_costs.py docstring)."""
    commission, slippage = costs.get_cost_params(cfg)
    prepared = prepare_bars(raw_bars, tz_source=tz_source)
    with_bands = compute_noise_bands(prepared, lookback_days=lookback_days, band_multiplier=band_multiplier)
    with_bands["vwap"] = compute_intraday_vwap(with_bands)

    vol_by_date = {}
    if daily_closes is not None and not daily_closes.empty:
        vol_series = compute_trailing_realized_vol_pct(daily_closes, lookback_days=lookback_days)
        vol_by_date = vol_series.to_dict()

    all_trades = []
    for d, day_df in with_bands.groupby("date", sort=True):
        day_df = day_df.reset_index(drop=True)
        lev = leverage_multiplier(vol_by_date.get(d), target_daily_vol_pct, max_leverage)
        all_trades.extend(simulate_day(day_df, fill_timing=fill_timing, stop_mode=stop_mode,
                                        leverage_x=lev, commission=commission, slippage=slippage))

    return {
        "trades": all_trades,
        "summary": summarize(all_trades),
        "config": {
            "lookback_days": lookback_days, "band_multiplier": band_multiplier,
            "fill_timing": fill_timing, "stop_mode": stop_mode,
            "target_daily_vol_pct": target_daily_vol_pct, "max_leverage": max_leverage,
            "commission_per_share": commission, "slippage_per_share": slippage,
        },
    }


def summarize(trades: list) -> dict:
    """Intraday-specific summary - deliberately NOT engine.backtest_engine.
    summarize(), which requires a 'hold_days' key and is built around a
    daily-bar swing trade's shape. This strategy's trades are same-day and
    can run long or short, so this reports hold_minutes and a long/short
    split instead. Reports both RAW and NET (cost-adjusted) figures side by
    side, same "show the cost sensitivity, don't hide it" posture as
    engine/execution_costs.py's module docstring describes for the daily
    engine's own cost model."""
    if not trades:
        return {"n_trades": 0}
    net = [t["net_outcome_pct"] for t in trades]
    raw = [t["raw_outcome_pct"] for t in trades]
    wins = [o for o in net if o > 0]
    losses = [o for o in net if o <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    by_reason: dict = {}
    by_direction: dict = {}
    for t in trades:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
        by_direction[t["direction"]] = by_direction.get(t["direction"], 0) + 1
    n_days = len(set(t["date"] for t in trades))
    return {
        "n_trades": len(trades),
        "n_days": n_days,
        "trades_per_day": round(len(trades) / n_days, 2) if n_days else None,
        "win_rate_net": round(len(wins) / len(trades) * 100, 1),
        "avg_outcome_pct_net": round(sum(net) / len(net), 4),
        "avg_outcome_pct_raw": round(sum(raw) / len(raw), 4),
        "avg_win_pct_net": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_pct_net": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "profit_factor_net": round(gross_win / gross_loss, 3) if gross_loss else None,
        "avg_hold_minutes": round(sum(t["hold_minutes"] for t in trades) / len(trades), 1),
        "exit_reason_counts": by_reason,
        "direction_counts": by_direction,
    }
