"""RSI(2)/Connors-style SPY mean-reversion — LIVE decision layer (2026-08-02).

This is the live/paper-callable counterpart to engine/rsi2_mean_reversion_
backtest.py, which stays backtest-only per this session's own established
convention (research/validation code lives in engine/, never rules/). Now
that Trinath has said he's ready to deploy the validated strategy, THIS
file is the one live_trader.py / paper_trader.py / a daily signal script
should import - it lives in rules/ deliberately, alongside rules/swing_buy_
rules.py, because it now IS a live decision function, not a backtest.

DOES NOT REIMPLEMENT THE MATH. compute_rsi()/compute_sma() are imported
directly from engine/rsi2_mean_reversion_backtest.py - the exact functions
that were unit-tested (13 tests) and walk-forward validated (docs/beating_
the_market_analysis_2026-07-28.md sec 11-12). A live decision function that
quietly reimplemented "the same" RSI(2) formula would risk a silent
divergence from what was actually validated - importing guarantees byte-
for-byte the same computation.

FROZEN PARAMETERS - do not tune these here. Per Trinath's own explicit
instruction (sec 12 of the analysis doc): "not a candidate for further
filters, regime overlays, or parameter tuning... The canonical rule set
validated in sec 11 is the version that cleared 3 of 4 pre-committed
criteria; it should be implemented as-is if operationalized, not iterated
on further." rsi_threshold=10 and exit_mode="rsi70" are the sec-11
TRAIN-selected, TEST-validated winner - hardcoded as this module's
defaults, not left as free parameters for a caller to casually override.

ENTRY/EXIT TIMING - same asymmetry as the backtest, restated here because
it is the single most important thing for a human or automated caller to
get right:
  - ENTRY fills at the NEXT session's open (the signal is read off the
    most recently COMPLETED bar - "yesterday's close" if you're deciding
    what to do before today's open, or "today's close" if you're deciding
    what to do before tomorrow's open).
  - EXIT fills at the SAME close that satisfies the exit condition - the
    backtest's own canonical (if aggressive) assumption. A fully automated
    caller running near the close can achieve this. A human reacting to a
    notification after the close cannot - see scripts/rsi2_signal_check.py
    for how the signal-only tier discloses and handles that gap rather
    than silently overstating what a manual exit can actually achieve.

WHAT THIS FILE DOES NOT DO: it does not place orders, does not touch the
`positions` table, does not call paper_trader.execute_buy/live_trader.
execute_buy_live, and does not read config.yaml's live-execution gates.
It answers exactly one question - "given this price history and this
position state, what does the RSI(2) rule say to do right now" - and
leaves execution, position tracking, and capital allocation to its
callers, each of which has very different risk profiles (a notification
script vs. an order-placing one) and should make those decisions
explicitly, not inherit them implicitly from this module.
"""
import pandas as pd

from engine.rsi2_mean_reversion_backtest import compute_rsi, compute_sma

RSI_THRESHOLD = 10          # frozen, sec-11 TRAIN-selected winner - do not tune
EXIT_MODE = "rsi70"         # frozen, sec-11 TRAIN-selected winner - do not tune
SMA_TREND_PERIOD = 200
SMA_EXIT_PERIOD = 5
RSI_PERIOD = 2
EXIT_RSI_THRESHOLD = 70.0
MAX_HOLD_DAYS = 10

REQUIRED_COLUMNS = {"date", "open", "high", "low", "close"}


def evaluate(daily_bars: pd.DataFrame, position: dict = None) -> dict:
    """daily_bars: date/open/high/low/close, ascending, most recent row =
    the most recently COMPLETED session (not an in-progress/intraday bar -
    see module docstring's ENTRY/EXIT TIMING section). Needs at least
    SMA_TREND_PERIOD+1 rows for a non-null signal; callers should fetch
    generously (400 daily bars, e.g. mcp_clients.market_data.MarketData
    Router.get_daily_bars('SPY'), comfortably covers the 200-day warmup
    with room to spare).

    position: None if flat, else {"entry_date": "YYYY-MM-DD",
    "entry_price": float} - the CALLER's own record of whether it believes
    itself to be in a trade. This function does not maintain that state
    itself (see module docstring).

    Returns a dict with at minimum: action, reason, reference_date,
    reference_close, rsi2, sma_trend, sma_exit, trend_ok. action is one of
    "ENTER_NEXT_OPEN", "EXIT_THIS_CLOSE", "HOLD", "NO_SIGNAL_STAY_FLAT".
    """
    missing = REQUIRED_COLUMNS - set(daily_bars.columns)
    if missing:
        raise ValueError(f"evaluate: daily_bars missing required column(s): {sorted(missing)}")
    if len(daily_bars) < SMA_TREND_PERIOD + 1:
        raise ValueError(f"evaluate: need at least {SMA_TREND_PERIOD + 1} daily bars for a valid "
                          f"SMA({SMA_TREND_PERIOD}) trend read, got {len(daily_bars)}")

    df = daily_bars.sort_values("date").reset_index(drop=True)
    closes = df["close"]
    rsi2 = compute_rsi(closes, period=RSI_PERIOD)
    sma_trend = compute_sma(closes, period=SMA_TREND_PERIOD)
    sma_exit = compute_sma(closes, period=SMA_EXIT_PERIOD)

    ref = df.iloc[-1]
    ref_rsi2 = float(rsi2.iloc[-1])
    ref_sma_trend = float(sma_trend.iloc[-1])
    ref_sma_exit = float(sma_exit.iloc[-1])
    trend_ok = bool(ref["close"] > ref_sma_trend)

    base = {
        "reference_date": str(ref["date"]),
        "reference_close": float(ref["close"]),
        "rsi2": round(ref_rsi2, 2),
        "sma_trend": round(ref_sma_trend, 4),
        "sma_exit": round(ref_sma_exit, 4),
        "trend_ok": trend_ok,
        "rsi_threshold": RSI_THRESHOLD,
        "exit_mode": EXIT_MODE,
    }

    if position is None:
        signal = trend_ok and ref_rsi2 < RSI_THRESHOLD
        if signal:
            return {**base, "action": "ENTER_NEXT_OPEN", "reason": "rsi2_oversold_in_uptrend"}
        return {**base, "action": "NO_SIGNAL_STAY_FLAT", "reason": None}

    # In a position - evaluate this reference bar as a candidate exit day,
    # exactly the loop generate_trades() runs per-day during a hold.
    entry_idx = df.index[df["date"] == position["entry_date"]]
    days_held = int(df.index[-1] - entry_idx[0]) if len(entry_idx) else None

    if EXIT_MODE == "sma5" and ref["close"] > ref_sma_exit:
        return {**base, "action": "EXIT_THIS_CLOSE", "reason": "sma5_cross", "days_held": days_held}
    if EXIT_MODE == "rsi70" and ref_rsi2 > EXIT_RSI_THRESHOLD:
        return {**base, "action": "EXIT_THIS_CLOSE", "reason": "rsi_overbought", "days_held": days_held}
    if days_held is not None and days_held >= MAX_HOLD_DAYS:
        return {**base, "action": "EXIT_THIS_CLOSE", "reason": "time_stop", "days_held": days_held}
    return {**base, "action": "HOLD", "reason": None, "days_held": days_held}
