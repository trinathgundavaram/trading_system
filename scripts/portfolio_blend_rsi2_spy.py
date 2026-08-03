#!/usr/bin/env python3
"""Portfolio-level blend analysis: RSI(2) SPY mean-reversion sleeve (sec 11
of docs/beating_the_market_analysis_2026-07-28.md) combined with a passive
buy-and-hold SPY core, at several illustrative sleeve weights (2026-08-02,
Trinath's "Path A" framing - "high-Sharpe, low-CAGR sleeve", option 2 of
his concrete next steps: "Run a simple portfolio-level backtest").

WHAT THIS DOES NOT DO: pick a sleeve weight for Trinath. He was explicit
that sizing (20-40% suggested) is "a strategic choice, not a backtest
question." This script instead answers the adjacent, legitimately
backtest-able question: for a handful of illustrative weights, what would
the BLENDED portfolio's CAGR/vol/Sharpe/MaxDD actually have looked like,
historically real dollars, real dates, so that strategic choice can be made
with real numbers in front of it instead of abstract intuition.

RETURN-MARKING CONVENTION - deliberately reused, not reinvented. The RSI2
sleeve's daily return series marks the ENTIRE trade's cost-adjusted
round-trip return on its EXIT date, and 0% on every other date (in a trade
or flat) - i.e. exactly the "marks equity only at trade CLOSE" convention
scripts/equity_curve.py's own Sharpe section already documents and
concedes as a real (not free) simplification ("understates real volatility
slightly... a live account holding open positions would show intra-month
mark-to-market swings this trade-close-only replay doesn't capture").
Reusing it here rather than inventing a new intraday mark-to-market model
keeps this analysis on the same honest footing as every other number in
this document, at the cost of the same known understatement of the
sleeve's own realized volatility while a trade is actually open. SPY's own
buy-and-hold leg uses real, continuous close-to-close daily returns (it has
no "trade close" event - it is always marked).

TWO WINDOWS REPORTED, DELIBERATELY LABELED DIFFERENTLY:
  - TEST-only (2023-01-01 to 2026-07-30): the honest, blind out-of-sample
    window from sec 11's walk-forward - the winning combo (rsi_threshold=10,
    exit_mode=rsi70) was NOT selected using this data.
  - FULL (2016-01-04 to 2026-07-30): a larger sample for a fuller picture
    of blended behavior across more regimes (2018 selloff, 2020 crash,
    2022 bear market all included) - but NOT a blind result, since this
    combo's parameters were themselves chosen using TRAIN performance
    inside this same window. Labeled "illustrative, not blind" throughout
    this script's output for that reason - it should not be read with the
    same evidentiary weight as the TEST-only numbers.

Usage:
    python3 scripts/portfolio_blend_rsi2_spy.py
    python3 scripts/portfolio_blend_rsi2_spy.py --weights 0 20 30 40 100
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.rsi2_mean_reversion_backtest import generate_trades

DATA_CSV = Path(__file__).resolve().parent.parent / "spy_daily_ohlc.csv"
FROZEN_COMBO = {"rsi_threshold": 10, "exit_mode": "rsi70"}  # sec 11's TRAIN-selected, TEST-validated winner
ROUND_TRIP_BPS = 10.0
TEST_SPLIT_DATE = "2023-01-01"
DEFAULT_WEIGHTS = [0, 20, 30, 40, 100]  # % allocated to the RSI2 sleeve; rest to buy-and-hold SPY


def load_daily_bars() -> pd.DataFrame:
    df = pd.read_csv(DATA_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def build_sleeve_daily_returns(daily_bars: pd.DataFrame, trades: list) -> pd.Series:
    """One row per session in daily_bars. 0.0 everywhere except a trade's
    EXIT date, which carries that trade's full cost-adjusted outcome_pct/100
    - see module docstring's RETURN-MARKING CONVENTION section for why."""
    s = pd.Series(0.0, index=daily_bars["date"])
    for t in trades:
        if t["exit_date"] in s.index:
            s.loc[t["exit_date"]] += t["outcome_pct"] / 100.0
    return s


def annualized_stats(daily_returns: pd.Series, starting_equity: float = 100_000.0) -> dict:
    equity = starting_equity * (1 + daily_returns).cumprod()
    n_days = len(daily_returns)
    years = n_days / 252.0
    total_return_pct = (equity.iloc[-1] / starting_equity - 1) * 100
    cagr_pct = ((equity.iloc[-1] / starting_equity) ** (1 / years) - 1) * 100 if years > 0 else None
    vol_annual_pct = daily_returns.std(ddof=1) * (252 ** 0.5) * 100
    mean_annual = daily_returns.mean() * 252
    std_annual = daily_returns.std(ddof=1) * (252 ** 0.5)
    sharpe = mean_annual / std_annual if std_annual > 0 else None
    running_max = equity.cummax()
    drawdown_pct = (running_max - equity) / running_max * 100
    max_dd_pct = drawdown_pct.max()
    return {
        "years": round(years, 2),
        "cagr_pct": round(cagr_pct, 2) if cagr_pct is not None else None,
        "total_return_pct": round(total_return_pct, 1),
        "vol_annual_pct": round(vol_annual_pct, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "max_drawdown_pct": round(max_dd_pct, 2),
        "final_equity": round(equity.iloc[-1], 2),
    }


def run_window(daily_bars: pd.DataFrame, sleeve_returns: pd.Series, spy_returns: pd.Series,
               weights: list, label: str, honest: bool) -> None:
    print(f"\n{'=' * 100}")
    tag = "(honest, blind out-of-sample)" if honest else "(illustrative, NOT a blind result - see module docstring)"
    print(f"WINDOW: {label} {tag}")
    print(f"{'=' * 100}")
    print(f"{'RSI2 weight':>12} {'SPY weight':>11} {'Years':>7} {'CAGR%':>8} {'TotalRet%':>10} "
          f"{'Vol%':>7} {'Sharpe':>8} {'MaxDD%':>8}")
    for w in weights:
        w_frac = w / 100.0
        blended = w_frac * sleeve_returns + (1 - w_frac) * spy_returns
        stats = annualized_stats(blended)
        print(f"{w:>11}% {100 - w:>10}% {stats['years']:>7} {stats['cagr_pct']:>7}% "
              f"{stats['total_return_pct']:>9}% {stats['vol_annual_pct']:>6}% "
              f"{stats['sharpe']:>8} {stats['max_drawdown_pct']:>7}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", type=float, nargs="+", default=DEFAULT_WEIGHTS,
                     help="RSI2 sleeve weights (%%) to evaluate; remainder goes to buy-and-hold SPY")
    ap.add_argument("--test-split-date", default=TEST_SPLIT_DATE)
    args = ap.parse_args()

    daily_bars = load_daily_bars()
    trades = generate_trades(daily_bars, round_trip_bps=ROUND_TRIP_BPS, **FROZEN_COMBO)
    sleeve_returns = build_sleeve_daily_returns(daily_bars, trades)
    spy_returns = daily_bars.set_index("date")["close"].pct_change().fillna(0.0)
    spy_returns.index = daily_bars["date"]  # align index type with sleeve_returns

    print(f"Frozen sleeve combo (sec 11 winner): {FROZEN_COMBO}, {ROUND_TRIP_BPS}bps round-trip cost")
    print(f"Full data range: {daily_bars['date'].iloc[0]} to {daily_bars['date'].iloc[-1]} "
          f"({len(daily_bars)} sessions, {len(trades)} sleeve trades)")

    test_mask = daily_bars["date"] >= args.test_split_date
    test_dates = daily_bars.loc[test_mask, "date"]
    run_window(daily_bars, sleeve_returns.loc[test_dates], spy_returns.loc[test_dates],
               args.weights, f"TEST ({args.test_split_date} to {daily_bars['date'].iloc[-1]})", honest=True)
    run_window(daily_bars, sleeve_returns, spy_returns,
               args.weights, f"FULL ({daily_bars['date'].iloc[0]} to {daily_bars['date'].iloc[-1]})", honest=False)

    print(f"\n{'-' * 100}")
    print("Read this as: at 0% RSI2 weight, the row IS 100% buy-and-hold SPY over that window (a sanity-check "
          "baseline, not a strategy). At 100% RSI2 weight, the row reproduces sec 11's own sleeve-only numbers "
          "(a second sanity check that this script's return-marking convention matches the walk-forward's).")


if __name__ == "__main__":
    sys.exit(main())
