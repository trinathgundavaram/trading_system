"""RSI(2) mean reversion + regime-timed forced exits (2026-08-13) - a
combination requested explicitly, not a new independent idea: "pick the
good aspect from each analysis and put it together." Two prior, separately
committed findings this reuses without modifying either:

  1. engine/rsi2_mean_reversion_backtest.py - the one signal family in the
     post-v3.4.0 research arc with real out-of-sample evidence behind it
     (frozen TRAIN-selected params rsi_threshold=10/exit_mode=rsi70, blind
     TEST: PF 2.05, Sharpe 0.81, MaxDD 4.5%, win rate 78.4%, CAGR 4.0% -
     short of the CAGR bar because the signal is in the market too rarely
     to compound on its own).
  2. engine/regime_timed_backtest.py - the ONE mechanism no other overlay
     tried this session (force-closing an ALREADY-OPEN position when the
     regime turns risk-off, not just gating new entries). It FAILED blind
     TEST when applied to the core swing-picking engine - but that failure
     was about which STOCKS the core engine picked, never re-tested against
     a completely different, already-validated signal family. This module
     asks the narrower question: does the forced-exit mechanism itself help
     or hurt when layered onto RSI2 specifically, where RSI2 already brings
     its own (simpler, non-hysteresis) SMA(200) trend filter at entry?

WHAT'S NEW HERE, and what isn't: entry signal generation (RSI(2) < threshold
while flat, `close > SMA(200)`) is copied verbatim from generate_trades() in
engine/rsi2_mean_reversion_backtest.py - not imported, because the regime
check has to be interleaved INTO the day-by-day hold loop, not wrapped
around it. compute_regime_timeline() and _regime_asof_yesterday() are
imported unmodified from engine/regime_timed_backtest.py - same hysteresis
regime read used there, same "only ever look at yesterday's close" no-lookahead
discipline. Neither source module is modified by this file.

TWO GATES, independently toggleable, so a sweep can tell them apart:
  - gate_entries: skip a new RSI2 entry if yesterday's regime read is
    risk-off (on top of RSI2's own SMA200 filter - the regime read adds
    hysteresis, RSI2's own filter doesn't have any).
  - gate_exits: the new mechanism. If yesterday's regime read is risk-off
    on a day where a position is already open, force-close TODAY AT THE
    OPEN (before checking the normal sma5/rsi70/time-stop exit condition
    for that day) - mirrors regime_timed_backtest.py's
    simulate_forward_exit_regime_gated() fill-timing choice exactly.

METHODOLOGY, same discipline as scripts/walk_forward_rsi2.py (the module
this one's TRAIN/TEST split date is deliberately copied from, since it's
the same SPY daily bars): pick (regime_sma_period, regime_buffer_pct, gate_entries,
gate_exits) using ONLY TRAIN-period (< 2023-01-01) CAGR, freeze, run once on
TEST, report the honest result either way. See scripts/walk_forward_rsi2_
regime_gated.py for the actual TRAIN-then-TEST driver.

HONEST CAVEAT ABOUT THE TEST WINDOW ITSELF, stated up front rather than left
for the report to discover: 2023-01-01 through the end of the available data
is the SAME TEST window scripts/walk_forward_rsi2.py already spent to select
and validate the RSI2 signal in the first place, and scripts/portfolio_blend_
rsi2_spy.py looked at again to validate the satellite-blend framing. This is
now at least the third look at that same holdout. A clean result here is
still real evidence the regime-exit MECHANISM doesn't obviously break a
working signal, but it carries less weight than a genuinely fresh holdout
would - the repeated-look problem is exactly what this codebase's own
walk-forward discipline exists to avoid, and this module cannot manufacture
data that doesn't exist to fully escape it. Forward (paper) evidence
accumulated after today, on a signal frozen today, would be the actual fix.
"""
import pandas as pd

from engine import execution_costs
from engine.regime_timed_backtest import compute_regime_timeline, _regime_asof_yesterday
from engine.rsi2_mean_reversion_backtest import compute_rsi, compute_sma, REQUIRED_COLUMNS

DEFAULT_SMA_TREND_PERIOD = 200
DEFAULT_SMA_EXIT_PERIOD = 5
DEFAULT_RSI_PERIOD = 2
DEFAULT_EXIT_RSI_THRESHOLD = 70.0
DEFAULT_MAX_HOLD_DAYS = 10


def generate_trades_regime_gated(daily_bars: pd.DataFrame, rsi_threshold: float = 10.0,
                                  exit_mode: str = "rsi70",
                                  exit_rsi_threshold: float = DEFAULT_EXIT_RSI_THRESHOLD,
                                  max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
                                  sma_trend_period: int = DEFAULT_SMA_TREND_PERIOD,
                                  sma_exit_period: int = DEFAULT_SMA_EXIT_PERIOD,
                                  rsi_period: int = DEFAULT_RSI_PERIOD, round_trip_bps: float = 0.0,
                                  regime_sma_period: int = 200, regime_buffer_pct: float = 3.0,
                                  gate_entries: bool = True, gate_exits: bool = True) -> list:
    """Same entry/exit contract and trade-dict shape as
    engine.rsi2_mean_reversion_backtest.generate_trades() (reusable by the
    same summarize()/equity_curve.simulate() callers) - two extra keys on
    each trade dict (regime_forced_exit, regime_blocked_entries_skipped is
    NOT tracked per-trade, see n_entries_blocked_by_regime in the return-
    adjacent counter printed by the driver script instead)."""
    if exit_mode not in ("sma5", "rsi70"):
        raise ValueError(f"exit_mode must be 'sma5' or 'rsi70', got {exit_mode!r}")
    missing = REQUIRED_COLUMNS - set(daily_bars.columns)
    if missing:
        raise ValueError(f"generate_trades_regime_gated: daily_bars missing required column(s): {sorted(missing)}")

    df = daily_bars.sort_values("date").reset_index(drop=True)
    closes = df["close"]
    df["rsi2"] = compute_rsi(closes, period=rsi_period)
    df["sma_trend"] = compute_sma(closes, period=sma_trend_period)
    df["sma_exit"] = compute_sma(closes, period=sma_exit_period)

    regime_by_date = compute_regime_timeline(df[["date", "close"]], sma_period=regime_sma_period,
                                              buffer_pct=regime_buffer_pct)
    spy_dates_sorted = sorted(regime_by_date.keys())

    def risk_on_yesterday(target_date) -> bool:
        return _regime_asof_yesterday(spy_dates_sorted, regime_by_date, target_date)

    trades = []
    n = len(df)
    i = 0
    n_entries_blocked_by_regime = 0
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
        entry_pos = i + 1
        if entry_pos >= n:
            break
        entry_row = df.iloc[entry_pos]
        entry_date = entry_row["date"]

        if gate_entries and not risk_on_yesterday(entry_date):
            n_entries_blocked_by_regime += 1
            i += 1
            continue

        entry_price = float(entry_row["open"])

        exit_pos = None
        exit_reason = None
        regime_forced_exit = False
        j = entry_pos
        while j < min(entry_pos + max_hold_days, n):
            exit_row = df.iloc[j]
            exit_date_j = exit_row["date"]
            if gate_exits and not risk_on_yesterday(exit_date_j):
                # Force-close at TODAY's open, before the normal exit check -
                # mirrors regime_timed_backtest.py's fill-timing choice.
                exit_pos, exit_reason, regime_forced_exit = j, "rule_exit", True
                break
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
            exit_pos = min(entry_pos + max_hold_days, n) - 1
            exit_reason = "time_stop"

        exit_row = df.iloc[exit_pos]
        exit_price = float(exit_row["open"]) if regime_forced_exit else float(exit_row["close"])
        exit_date = exit_row["date"]
        hold_days = exit_pos - entry_pos

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
            "outcome_r": raw_return,
            "risk_pct": 100.0,
            "hold_days": hold_days,
            "exit_reason": exit_reason,
            "regime_forced_exit": regime_forced_exit,
            "execution_cost_bps": round_trip_bps,
            "entry_rsi2": round(float(row["rsi2"]), 2),
            "entry_sma_trend": round(float(row["sma_trend"]), 4),
        })
        i = exit_pos + 1

    trades_meta = {"n_entries_blocked_by_regime": n_entries_blocked_by_regime}
    return trades, trades_meta
