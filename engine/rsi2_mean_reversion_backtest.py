"""RSI(2) / Connors-style mean-reversion backtest for SPY (2026-07-31) -
Trinath's spec, next direction after sec 10's finding that the core
swing-picking engine, completely unfiltered, does not beat SPY under any of
its own real configurations. This is a DIFFERENT signal family entirely
(mean reversion, not momentum/trend-following), long-only, single
instrument (SPY), daily bars - deliberately simple, since the spec's own
framing is "minimal tuning... the canonical parameters are already
well-studied."

SCOPE. New, self-contained module under engine/ - same boundary as every
other addition this session (regime_timed_backtest.py, intraday_momentum_
backtest.py): does NOT touch rules/swing_buy_rules.py, engine/backtest_
engine.py, or any live/paper trading path. Trinath's spec suggested rules/
rsi2_spy_swing.py; this lives in engine/ instead, matching every prior
backtest-only addition's location this session rather than the directory
the real live decision pipeline (rules/hard_vetoes.py, rules/swing_buy_
rules.py) lives in - purely a location choice for consistency/clarity, not
a disagreement with the strategy spec itself.

STRATEGY (long-only, daily bars):
  1. TREND FILTER: close > SMA(200). Only long entries are ever considered
     when this holds.
  2. ENTRY SIGNAL: RSI(2) < rsi_threshold (5 or 10 - the two canonical
     values in the published literature) while flat and the trend filter
     holds. Entry executes at the FOLLOWING day's open (signal detected at
     close of day t, filled at day t+1's open) - the conservative,
     realistically-achievable assumption, consistent with this session's
     standing fill-timing discipline (see engine/intraday_momentum_
     backtest.py's FILL TIMING section for why "signal day == fill day"
     is treated as optimistic elsewhere in this codebase).
  3. EXIT - two modes, per the spec:
     - "sma5": exit AT THE CLOSE of the first day close > SMA(5) - the
       classic Connors rule. Same-day (not next-open) by design: this is
       the canonical published rule, and unlike the ENTRY signal (which
       needs a next-day fill because a real trader could not have reacted
       within the same close that generated it), the exit condition and
       its fill are conventionally treated as the same event in the
       published methodology this strategy is drawn from - noted here
       explicitly rather than silently applied, since it is a real,
       if standard and heavily documented, simplification.
     - "rsi70": exit at the close of the first day RSI(2) > 70.
  4. TIME STOP (on by default, `max_hold_days`): force-exit at that day's
     close if neither exit condition has fired within max_hold_days
     trading days - bounds the degenerate case where price keeps grinding
     without crossing back above SMA(5) or RSI(2) never recovering above
     70. The spec calls this "optional"; defaulted ON here (10 trading
     days) because an unbounded hold is a real, if presumably rare, tail
     risk this backtest should not silently ignore. exit_reason
     distinguishes it ("time_stop") so its frequency is visible in the
     summary, not blended into the other two exit reasons.

POSITION SIZING: per the spec ("100% of allocated capital when in a trade,
0%% when out") - this is NOT the risk/stop-distance framing scripts/
equity_curve.py's simulate() was originally built around (there is no stop
level here at all; the exit is signal-based, not price-based-risk-based).
Rather than writing a parallel equity-curve implementation, this reuses
simulate() by exploiting a special case that is mathematically exactly
equivalent to full-allocation sizing: every trade dict below carries a
CONSTANT risk_pct=100.0 (position value == dollar risk, i.e. no leverage,
no partial sizing) and outcome_r/outcome_pct set to the trade's own raw
(cost-adjusted) return. Calling simulate(trades, risk_pct=100.0, ...) then
sizes every trade at exactly 100% of current equity - the intended
behavior - without inventing new equity-curve code. Since this is a single
instrument with no overlapping positions (always flat before the next
entry), the capital-constraint machinery in simulate() never actually
binds either way (reject vs partial_resize are equivalent here) - noted,
not a coincidence to rely on if this module is ever extended to multiple
instruments simultaneously.

COST MODEL: reuses engine/execution_costs.py's existing round-trip-bps
model (NOT engine/intraday_execution_costs.py's $/share model built for
the intraday module) - this strategy trades in %-of-notional terms like
the rest of the daily swing engine, not the per-share terms the intraday
paper's own disclosed cost assumption used. Default off (0bps, matching
execution_costs.py's own default-off posture) - callers pass round_trip_
bps explicitly to activate it, e.g. 10.0 for the spec's suggested 5-10bps
range.
"""
import pandas as pd

from engine import execution_costs

DEFAULT_SMA_TREND_PERIOD = 200
DEFAULT_SMA_EXIT_PERIOD = 5
DEFAULT_RSI_PERIOD = 2
DEFAULT_EXIT_RSI_THRESHOLD = 70.0
DEFAULT_MAX_HOLD_DAYS = 10

REQUIRED_COLUMNS = {"date", "open", "high", "low", "close"}


def compute_rsi(closes: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> pd.Series:
    """Wilder's RSI (the standard formula "RSI(2)" universally refers to in
    the published literature - Connors' own work included), parametrized
    at `period`. delta.diff() -> separate gain/loss series -> Wilder's
    recursive smoothing (an EWM with alpha=1/period, adjust=False - the
    textbook implementation, not a plain rolling mean, which would be a
    different and non-standard "RSI"). First `period` values are NaN (no
    full window yet) - callers must not treat those as tradable signals,
    same warmup posture as every other indicator in this codebase."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    # avg_loss == 0 (no losing bar in the smoothing window) is a genuine
    # RSI=100 (all gains), not a missing value - restore it explicitly
    # rather than leaving the div-by-zero's NaN in place.
    rsi = rsi.where(avg_loss != 0, 100.0)
    return rsi.rename(f"rsi_{period}")


def compute_sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period, min_periods=period).mean().rename(f"sma_{period}")


def generate_trades(daily_bars: pd.DataFrame, rsi_threshold: float = 5.0, exit_mode: str = "sma5",
                     exit_rsi_threshold: float = DEFAULT_EXIT_RSI_THRESHOLD,
                     max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
                     sma_trend_period: int = DEFAULT_SMA_TREND_PERIOD,
                     sma_exit_period: int = DEFAULT_SMA_EXIT_PERIOD,
                     rsi_period: int = DEFAULT_RSI_PERIOD, round_trip_bps: float = 0.0) -> list:
    """daily_bars: DataFrame with date/open/high/low/close, one row per
    session, sorted ascending, date a plain (non-tz) date-like value.
    Returns a list of trade dicts shaped for engine.backtest_engine.
    summarize() and scripts/equity_curve.simulate() (both reused
    unmodified - see module docstring's POSITION SIZING section for why
    risk_pct is a constant 100.0 here)."""
    if exit_mode not in ("sma5", "rsi70"):
        raise ValueError(f"exit_mode must be 'sma5' or 'rsi70', got {exit_mode!r}")
    missing = REQUIRED_COLUMNS - set(daily_bars.columns)
    if missing:
        raise ValueError(f"generate_trades: daily_bars missing required column(s): {sorted(missing)}")

    df = daily_bars.sort_values("date").reset_index(drop=True)
    closes = df["close"]
    df["rsi2"] = compute_rsi(closes, period=rsi_period)
    df["sma_trend"] = compute_sma(closes, period=sma_trend_period)
    df["sma_exit"] = compute_sma(closes, period=sma_exit_period)

    trades = []
    n = len(df)
    i = 0
    while i < n:
        row = df.iloc[i]
        if pd.isna(row["sma_trend"]) or pd.isna(row["rsi2"]):
            i += 1
            continue
        trend_ok = row["close"] > row["sma_trend"]
        signal = trend_ok and row["rsi2"] < rsi_threshold
        if not signal:
            i += 1
            continue
        # ENTRY fills at the NEXT day's open - see module docstring.
        entry_pos = i + 1
        if entry_pos >= n:
            break  # signal on the last available bar - no next open to fill on
        entry_row = df.iloc[entry_pos]
        entry_price = float(entry_row["open"])
        entry_date = entry_row["date"]

        exit_pos = None
        exit_reason = None
        j = entry_pos
        while j < min(entry_pos + max_hold_days, n):
            exit_row = df.iloc[j]
            if pd.isna(exit_row["sma_exit"]) or pd.isna(exit_row["rsi2"]):
                j += 1
                continue
            if exit_mode == "sma5" and exit_row["close"] > exit_row["sma_exit"]:
                exit_pos, exit_reason = j, "sma5_cross"
                break
            if exit_mode == "rsi70" and exit_row["rsi2"] > exit_rsi_threshold:
                exit_pos, exit_reason = j, "rsi_overbought"
                break
            j += 1
        if exit_pos is None:
            # neither exit condition fired within max_hold_days - forced
            # time-stop at the last bar of the hold window (or the last
            # bar of the dataset, if the data runs out first).
            exit_pos = min(entry_pos + max_hold_days, n) - 1
            exit_reason = "time_stop"

        exit_row = df.iloc[exit_pos]
        exit_price = float(exit_row["close"])
        exit_date = exit_row["date"]
        hold_days = exit_pos - entry_pos  # 0 = exited the same day it entered (next-open fill, same-close exit)

        cost_entry, cost_exit = execution_costs.apply_execution_costs(
            entry_price, exit_price, {"backtest": {"execution_cost_bps": round_trip_bps}})
        raw_return = (cost_exit / cost_entry - 1)
        trades.append({
            "ticker": "SPY",
            "entry_date": str(entry_date),
            "entry_price": round(entry_price, 4),
            "exit_date": str(exit_date),
            "exit_price": round(exit_price, 4),
            "outcome_pct": round(raw_return * 100, 4),
            "outcome_r": raw_return,          # see module docstring - risk_pct=100 makes this the trade's own return
            "risk_pct": 100.0,
            "hold_days": hold_days,
            "exit_reason": exit_reason,
            "execution_cost_bps": round_trip_bps,
            "entry_rsi2": round(float(row["rsi2"]), 2),
            "entry_sma_trend": round(float(row["sma_trend"]), 4),
        })
        i = exit_pos + 1  # flat again - no overlapping positions, single instrument

    return trades
