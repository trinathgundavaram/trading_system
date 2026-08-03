#!/usr/bin/env python3
"""Blind walk-forward validation for the RSI(2)/Connors-style SPY mean-
reversion strategy (2026-08-02) - engine/rsi2_mean_reversion_backtest.py.
Same discipline as every other walk-forward this session (regime overlay,
intraday momentum, the core unfiltered engine): lock the split date, grid,
selection metric, and success criteria BEFORE looking at any TEST-period
number, select the winner on TRAIN only, freeze it, then run it unmodified
on TEST and report the honest result either way.

DATA. scripts/../spy_daily_ohlc.csv - real SPY daily OHLCV derived from the
Alpaca SIP 1-minute bars Trinath fetched this session (2016-01-04 to
2026-07-30, 2658 sessions), aggregated via engine.intraday_momentum_
backtest.prepare_bars() + groupby. No network access needed to run this
script - unlike the intraday and core-engine walk-forwards, which required
Trinath to run the fetch/replay on his own machine, this one runs directly
against an already-downloaded CSV.

SIGNAL COMPUTATION SPANS THE FULL HISTORY, NOT JUST TRAIN OR TEST
SEPARATELY. generate_trades() is called ONCE on the entire 2016-2026
series, then the resulting trade list is split into TRAIN/TEST buckets by
entry_date. This is deliberate, not an oversight: SMA(200)/RSI(2) are
backward-looking indicators with no lookahead, so computing them
continuously across the full timeline uses no TEST-period information to
generate TRAIN signals (or vice versa) - it just avoids forcing the TEST
period to "cold-start" its own 200-day SMA warmup on 2023-01-01, which
would be an unrealistic handicap no live system would actually have (a
real trader running this strategy in 2023 would have years of prior SPY
history already in hand, not a blank slate). Running generate_trades()
separately on TRAIN-only and TEST-only slices would be the more
conservative-looking choice but is not actually more correct - it is
strictly worse information for both without changing what the walk-forward
is testing (the trade list within each window is IDENTICAL either way,
since SMA/RSI values that far into the window's own history no longer
depend on whether earlier bars from the other window were technically
"in scope").

SPLIT (LOCKED BEFORE ANY RESULT WAS SEEN): fixed calendar date, 2023-01-01.
  TRAIN: 2016-01-04 - 2022-12-31 (~7 years, includes the 2018 Q4 selloff,
         2020 COVID crash/recovery, and 2022's bear market - a real mix of
         regimes, not a cherry-picked calm stretch).
  TEST:  2023-01-01 - 2026-07-30 (~3.5 years, entirely unseen by selection,
         includes the 2023-2024 bull run and whatever regime unfolded into
         2026 - genuinely never touched while picking the winning combo).
This mirrors the fixed-date option Trinath's own spec offered (vs. a
percentage split) and was chosen because it fits the full available data
range naturally and gives TRAIN enough history to include multiple real
drawdowns, not just an uninterrupted bull run.

GRID (4 combos, per spec - "minimal tuning... canonical parameters are
already well-studied"):
  rsi_threshold ∈ {5, 10}  x  exit_mode ∈ {sma5, rsi70}
Every other parameter is held at engine/rsi2_mean_reversion_backtest.py's
published default (sma_trend_period=200, sma_exit_period=5, rsi_period=2,
max_hold_days=10) - these are the paper's own canonical values, not free
parameters this walk-forward is searching over.

COST ASSUMPTION (fixed, not swept): round_trip_bps=10.0 - the midpoint of
the 5-10bps range Trinath's spec itself suggested as reasonable for SPY
(one of the most liquid instruments that exists). This is a cost
ASSUMPTION applied identically to every combo, the same way commission/
slippage were fixed (not grid parameters) in every other cost-aware sweep
this session - the grid searches for signal-quality differences, not for
the cost assumption that makes a strategy look best.

SELECTION METRIC: highest TRAIN CAGR (via scripts/equity_curve.simulate(),
risk_pct=100.0 - see engine/rsi2_mean_reversion_backtest.py's POSITION
SIZING section for why this reproduces "100% of capital when in a trade"
sizing exactly), subject to a MIN_TRAIN_TRADES floor of 30 - matching
scripts/walk_forward_core_engine.py's convention (CAGR, not PF, was that
walk-forward's selection metric too; consistency across this session's
walk-forwards matters more than re-relitigating the choice each time).

PRE-COMMITTED ABSOLUTE SUCCESS CRITERIA (Trinath's spec, locked before any
TEST number was seen - these are ABSOLUTE bars, not "best of the 4 combos
that happened to run"):
  TEST profit_factor        > 1.1
  TEST Sharpe                >= 0.5
  TEST max_drawdown_pct      <= 35%   (spec's own stated 30-35% range - the
                                        lenient/outer edge, so failing this
                                        outer bound is unambiguous failure)
  TEST cagr_pct               >= 7%    (spec's own stated 7-8% range - the
                                        lenient/lower edge, same reasoning)
If the frozen TEST result misses ANY of these, the honest verdict is
"does not clear the bar," full stop - regardless of how it compares to
other strategies tried this session.

Usage:
    python3 scripts/walk_forward_rsi2.py
    python3 scripts/walk_forward_rsi2.py --split-date 2023-01-01 --starting-equity 100000
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.rsi2_mean_reversion_backtest import generate_trades
from engine.backtest_engine import summarize
from scripts.equity_curve import simulate

DATA_CSV = Path(__file__).resolve().parent.parent / "spy_daily_ohlc.csv"
SPLIT_DATE_DEFAULT = "2023-01-01"
MIN_TRAIN_TRADES = 30
ROUND_TRIP_BPS = 10.0

COMBOS = [
    {"rsi_threshold": 5, "exit_mode": "sma5"},
    {"rsi_threshold": 5, "exit_mode": "rsi70"},
    {"rsi_threshold": 10, "exit_mode": "sma5"},
    {"rsi_threshold": 10, "exit_mode": "rsi70"},
]

SUCCESS_CRITERIA = {
    "profit_factor_min": 1.1,
    "sharpe_min": 0.5,
    "max_drawdown_pct_max": 35.0,
    "cagr_pct_min": 7.0,
}


def load_daily_bars() -> pd.DataFrame:
    if not DATA_CSV.exists():
        raise FileNotFoundError(
            f"{DATA_CSV} not found - this walk-forward needs the real SPY daily OHLCV "
            "derived from the Alpaca SIP minute bars fetched this session.")
    df = pd.read_csv(DATA_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def split_trades(trades: list, split_date: str) -> tuple:
    train = [t for t in trades if t["entry_date"] < split_date]
    test = [t for t in trades if t["entry_date"] >= split_date]
    return train, test


def evaluate(trades: list, starting_equity: float) -> dict:
    """Combines engine.backtest_engine.summarize()'s trade-quality stats
    (PF, win_rate, expectancy_r) with scripts.equity_curve.simulate()'s
    portfolio-level stats (CAGR, Sharpe, MaxDD) on the same trade list -
    the same two-lens reporting used for the core-engine walk-forward."""
    if not trades:
        return {"n_trades": 0}
    stats = summarize(trades)
    sim = simulate(trades, risk_pct=100.0, starting_equity=starting_equity)
    return {**stats, **{f"sim_{k}": v for k, v in sim.items()}}


def check_criteria(sim_result: dict) -> dict:
    pf = sim_result.get("profit_factor")
    sharpe = sim_result.get("sim_sharpe")
    mdd = sim_result.get("sim_max_drawdown_pct")
    cagr = sim_result.get("sim_cagr_pct")
    checks = {
        "profit_factor": (pf is not None and pf > SUCCESS_CRITERIA["profit_factor_min"], pf),
        "sharpe": (sharpe is not None and sharpe >= SUCCESS_CRITERIA["sharpe_min"], sharpe),
        "max_drawdown_pct": (mdd is not None and mdd <= SUCCESS_CRITERIA["max_drawdown_pct_max"], mdd),
        "cagr_pct": (cagr is not None and cagr >= SUCCESS_CRITERIA["cagr_pct_min"], cagr),
    }
    checks["all_pass"] = all(v[0] for v in checks.values())
    return checks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split-date", default=SPLIT_DATE_DEFAULT,
                     help=f"TRAIN/TEST boundary, entry_date >= this is TEST (default: {SPLIT_DATE_DEFAULT}, locked)")
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--round-trip-bps", type=float, default=ROUND_TRIP_BPS)
    ap.add_argument("--min-train-trades", type=int, default=MIN_TRAIN_TRADES)
    args = ap.parse_args()

    daily_bars = load_daily_bars()
    print(f"Loaded {len(daily_bars)} sessions from {DATA_CSV.name}: "
          f"{daily_bars['date'].iloc[0]} to {daily_bars['date'].iloc[-1]}")
    print(f"Split date: {args.split_date}  (TRAIN < split, TEST >= split)")
    print(f"Cost assumption: {args.round_trip_bps} bps round-trip (fixed, not swept)")
    print()

    print("=" * 100)
    print("TRAIN (selection happens here ONLY - TEST is not touched below this point)")
    print("=" * 100)
    train_results = {}
    header = (f"{'rsi_thr':>8} {'exit_mode':>10} {'n_trades':>9} {'win%':>6} {'PF':>6} "
              f"{'exp_r':>7} {'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>8}")
    print(header)
    for combo in COMBOS:
        trades = generate_trades(daily_bars, round_trip_bps=args.round_trip_bps, **combo)
        train_trades, test_trades = split_trades(trades, args.split_date)
        key = (combo["rsi_threshold"], combo["exit_mode"])
        train_eval = evaluate(train_trades, args.starting_equity)
        train_results[key] = {"combo": combo, "train_trades": train_trades,
                               "test_trades": test_trades, "train_eval": train_eval}
        if train_eval.get("n_trades", 0) == 0:
            print(f"{combo['rsi_threshold']:>8} {combo['exit_mode']:>10}   no trades in TRAIN window")
            continue
        print(f"{combo['rsi_threshold']:>8} {combo['exit_mode']:>10} {train_eval['n_trades']:>9} "
              f"{train_eval['win_rate']:>6} {train_eval.get('profit_factor'):>6} "
              f"{train_eval.get('expectancy_r'):>7} {train_eval.get('sim_cagr_pct'):>7} "
              f"{train_eval.get('sim_max_drawdown_pct'):>7} {train_eval.get('sim_sharpe'):>8}")
    print()

    eligible = {k: v for k, v in train_results.items()
                if v["train_eval"].get("n_trades", 0) >= args.min_train_trades
                and v["train_eval"].get("sim_cagr_pct") is not None}
    if not eligible:
        print(f"NO combo cleared the min-train-trades floor ({args.min_train_trades}). "
              "Cannot select a winner - stopping without touching TEST.")
        return

    winner_key = max(eligible, key=lambda k: eligible[k]["train_eval"]["sim_cagr_pct"])
    winner = eligible[winner_key]
    print(f"WINNER (highest TRAIN CAGR, >= {args.min_train_trades} TRAIN trades): "
          f"rsi_threshold={winner_key[0]} exit_mode={winner_key[1]} "
          f"(TRAIN CAGR={winner['train_eval']['sim_cagr_pct']}%, "
          f"TRAIN n_trades={winner['train_eval']['n_trades']})")
    print("Freezing this combo. Running it, unmodified, on the never-touched TEST window now.")
    print()

    print("=" * 100)
    print("TEST (frozen winner, run once - this is the honest verdict)")
    print("=" * 100)
    test_trades = winner["test_trades"]
    test_eval = evaluate(test_trades, args.starting_equity)
    if test_eval.get("n_trades", 0) == 0:
        print("WINNER produced ZERO trades in the TEST window - cannot evaluate success criteria.")
        return

    print(f"n_trades={test_eval['n_trades']}  win_rate={test_eval['win_rate']}%  "
          f"profit_factor={test_eval.get('profit_factor')}  expectancy_r={test_eval.get('expectancy_r')}")
    print(f"CAGR={test_eval.get('sim_cagr_pct')}%  total_return={test_eval.get('sim_total_return_pct')}%  "
          f"MaxDD={test_eval.get('sim_max_drawdown_pct')}%  Sharpe={test_eval.get('sim_sharpe')}  "
          f"years={test_eval.get('sim_years')}")
    print(f"exit_reason_counts={test_eval.get('exit_reason_counts')}")
    print()

    print("-" * 100)
    print("PRE-COMMITTED SUCCESS CRITERIA CHECK (locked before this run - see module docstring)")
    print("-" * 100)
    checks = check_criteria(test_eval)
    print(f"  profit_factor > {SUCCESS_CRITERIA['profit_factor_min']}: "
          f"{'PASS' if checks['profit_factor'][0] else 'FAIL'}  (actual: {checks['profit_factor'][1]})")
    print(f"  sharpe >= {SUCCESS_CRITERIA['sharpe_min']}: "
          f"{'PASS' if checks['sharpe'][0] else 'FAIL'}  (actual: {checks['sharpe'][1]})")
    print(f"  max_drawdown_pct <= {SUCCESS_CRITERIA['max_drawdown_pct_max']}%: "
          f"{'PASS' if checks['max_drawdown_pct'][0] else 'FAIL'}  (actual: {checks['max_drawdown_pct'][1]}%)")
    print(f"  cagr_pct >= {SUCCESS_CRITERIA['cagr_pct_min']}%: "
          f"{'PASS' if checks['cagr_pct'][0] else 'FAIL'}  (actual: {checks['cagr_pct'][1]}%)")
    print()
    print(f"VERDICT: {'CLEARS ALL PRE-COMMITTED SUCCESS CRITERIA' if checks['all_pass'] else 'DOES NOT CLEAR ALL SUCCESS CRITERIA'}")


if __name__ == "__main__":
    sys.exit(main())
