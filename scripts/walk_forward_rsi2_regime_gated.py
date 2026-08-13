#!/usr/bin/env python3
"""Walk-forward for engine/rsi2_regime_gated_backtest.py (2026-08-13) -
"pick the good aspect of each analysis and combine them, then test the
combination." Same TRAIN/TEST split and same evaluate()/check_criteria()
shape as scripts/walk_forward_rsi2.py, on purpose, so this run is directly
comparable to the already-known RSI2-alone result rather than a fresh,
harder-to-compare methodology.

BASE SIGNAL IS FROZEN, NOT SWEPT AGAIN: rsi_threshold=10, exit_mode=rsi70 -
scripts/walk_forward_rsi2.py's own TRAIN-selected, TEST-validated winner.
Re-sweeping the base signal's own parameters here, on top of a NEW regime
grid, would be exactly the kind of multiple-comparison fishing this
codebase's walk-forward discipline exists to prevent. What IS swept here is
only the new thing being added: the regime read's own two parameters
(sma_period, buffer_pct - the same curated 6-combo grid
scripts/sweep_regime_timed_configs.py used) crossed with whether the regime
gate applies to entries as well as exits (gate_exits is always True - that
is the mechanism under test; gate_entries in {True, False} asks whether
ALSO blocking new entries during risk-off adds anything beyond RSI2's own
existing SMA(200) filter).

Selection metric: TRAIN CAGR, >= MIN_TRAIN_TRADES trades - identical rule to
scripts/walk_forward_rsi2.py. Winner is frozen and run ONCE on TEST.

See engine/rsi2_regime_gated_backtest.py's module docstring for the honest
caveat on why this TEST window is not a fresh holdout (RSI2 alone and the
RSI2/SPY blend already looked at it) - read that before trusting this
number as more than "the mechanism didn't obviously break a working
signal."
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.rsi2_regime_gated_backtest import generate_trades_regime_gated
from engine.rsi2_mean_reversion_backtest import generate_trades as generate_trades_baseline
from engine.backtest_engine import summarize
from scripts.equity_curve import simulate

DATA_CSV = Path(__file__).resolve().parent.parent / "spy_daily_ohlc.csv"
SPLIT_DATE_DEFAULT = "2023-01-01"
MIN_TRAIN_TRADES = 30
ROUND_TRIP_BPS = 10.0
FROZEN_BASE = {"rsi_threshold": 10, "exit_mode": "rsi70"}  # walk_forward_rsi2.py's own winner

REGIME_COMBOS = [(200, 0.0), (200, 3.0), (200, 5.0), (100, 3.0), (150, 3.0), (250, 5.0)]
GATE_ENTRIES_OPTIONS = [False, True]

SUCCESS_CRITERIA = {
    "profit_factor_min": 1.1,
    "sharpe_min": 0.5,
    "max_drawdown_pct_max": 35.0,
    "cagr_pct_min": 7.0,
}


def load_daily_bars() -> pd.DataFrame:
    if not DATA_CSV.exists():
        raise FileNotFoundError(f"{DATA_CSV} not found.")
    df = pd.read_csv(DATA_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date").reset_index(drop=True)


def split_trades(trades: list, split_date: str) -> tuple:
    train = [t for t in trades if t["entry_date"] < split_date]
    test = [t for t in trades if t["entry_date"] >= split_date]
    return train, test


def evaluate(trades: list, starting_equity: float, sizing_mode: str = "current_equity",
             capital_mode: str = "reject") -> dict:
    if not trades:
        return {"n_trades": 0}
    stats = summarize(trades)
    sim = simulate(trades, risk_pct=100.0, starting_equity=starting_equity,
                    sizing_mode=sizing_mode, capital_mode=capital_mode)
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


def print_row(label, ev):
    if ev.get("n_trades", 0) == 0:
        print(f"{label:<32} no trades")
        return
    print(f"{label:<32} n={ev['n_trades']:>4} win%={ev['win_rate']:>6} PF={ev.get('profit_factor'):>6} "
          f"exp_r={ev.get('expectancy_r'):>7} CAGR%={ev.get('sim_cagr_pct'):>7} "
          f"MaxDD%={ev.get('sim_max_drawdown_pct'):>7} Sharpe={ev.get('sim_sharpe'):>7}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split-date", default=SPLIT_DATE_DEFAULT)
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--round-trip-bps", type=float, default=ROUND_TRIP_BPS)
    ap.add_argument("--min-train-trades", type=int, default=MIN_TRAIN_TRADES)
    args = ap.parse_args()

    daily_bars = load_daily_bars()
    print(f"Loaded {len(daily_bars)} sessions from {DATA_CSV.name}: "
          f"{daily_bars['date'].iloc[0]} to {daily_bars['date'].iloc[-1]}")
    print(f"Split date: {args.split_date}  (TRAIN < split, TEST >= split)")
    print(f"Base signal FROZEN at {FROZEN_BASE} (walk_forward_rsi2.py's own winner - not re-swept)")
    print(f"Cost assumption: {args.round_trip_bps} bps round-trip")
    print()

    # ---- Baseline: RSI2 alone, same split, for a direct side-by-side -----
    print("=" * 100)
    print("BASELINE (RSI2 alone, no regime gate) - reproducing the known result on this exact split")
    print("=" * 100)
    base_trades = generate_trades_baseline(daily_bars, round_trip_bps=args.round_trip_bps, **FROZEN_BASE)
    base_train, base_test = split_trades(base_trades, args.split_date)
    print_row("TRAIN", evaluate(base_train, args.starting_equity))
    print_row("TEST", evaluate(base_test, args.starting_equity))
    print()

    # ---- TRAIN sweep: regime (sma,buffer) x gate_entries, gate_exits=True always
    print("=" * 100)
    print("TRAIN (regime-gate selection happens here ONLY - TEST is not touched below this point)")
    print("=" * 100)
    train_results = {}
    for sma_period, buffer_pct in REGIME_COMBOS:
        for gate_entries in GATE_ENTRIES_OPTIONS:
            trades, meta = generate_trades_regime_gated(
                daily_bars, round_trip_bps=args.round_trip_bps,
                regime_sma_period=sma_period, regime_buffer_pct=buffer_pct,
                gate_entries=gate_entries, gate_exits=True, **FROZEN_BASE)
            train_trades, test_trades = split_trades(trades, args.split_date)
            key = (sma_period, buffer_pct, gate_entries)
            train_eval = evaluate(train_trades, args.starting_equity)
            train_results[key] = {"train_trades": train_trades, "test_trades": test_trades,
                                   "train_eval": train_eval, "meta": meta}
            label = f"sma={sma_period} buf={buffer_pct}% gate_entries={gate_entries}"
            print_row(label, train_eval)
    print()

    eligible = {k: v for k, v in train_results.items()
                if v["train_eval"].get("n_trades", 0) >= args.min_train_trades
                and v["train_eval"].get("sim_cagr_pct") is not None}
    if not eligible:
        print(f"NO config cleared the min-train-trades floor ({args.min_train_trades}). Stopping.")
        return

    winner_key = max(eligible, key=lambda k: eligible[k]["train_eval"]["sim_cagr_pct"])
    winner = eligible[winner_key]
    sma_period, buffer_pct, gate_entries = winner_key
    print(f"WINNER (highest TRAIN CAGR, >= {args.min_train_trades} TRAIN trades): "
          f"sma={sma_period} buffer={buffer_pct}% gate_entries={gate_entries} "
          f"(TRAIN CAGR={winner['train_eval']['sim_cagr_pct']}%, n_trades={winner['train_eval']['n_trades']}, "
          f"entries_blocked_by_regime={winner['meta']['n_entries_blocked_by_regime']})")
    print("Freezing this config. Running it, unmodified, on TEST now.")
    print()

    print("=" * 100)
    print("TEST (frozen winner, run once)")
    print("=" * 100)
    test_trades = winner["test_trades"]
    test_eval = evaluate(test_trades, args.starting_equity)
    if test_eval.get("n_trades", 0) == 0:
        print("WINNER produced ZERO trades in TEST - cannot evaluate.")
        return
    print_row("current_equity sizing", test_eval)
    hw_eval = evaluate(test_trades, args.starting_equity, sizing_mode="high_watermark",
                        capital_mode="partial_resize")
    print_row("high_watermark sizing (partial_resize)", hw_eval)
    n_forced = sum(1 for t in test_trades if t.get("regime_forced_exit"))
    print(f"regime-forced exits in TEST: {n_forced}/{len(test_trades)}  "
          f"exit_reason_counts={test_eval.get('exit_reason_counts')}")
    print()

    print("-" * 100)
    print("PRE-COMMITTED SUCCESS CRITERIA CHECK (same bars as scripts/walk_forward_rsi2.py)")
    print("-" * 100)
    for label, ev in (("current_equity", test_eval), ("high_watermark", hw_eval)):
        checks = check_criteria(ev)
        print(f"[{label}]")
        print(f"  profit_factor > {SUCCESS_CRITERIA['profit_factor_min']}: "
              f"{'PASS' if checks['profit_factor'][0] else 'FAIL'}  (actual: {checks['profit_factor'][1]})")
        print(f"  sharpe >= {SUCCESS_CRITERIA['sharpe_min']}: "
              f"{'PASS' if checks['sharpe'][0] else 'FAIL'}  (actual: {checks['sharpe'][1]})")
        print(f"  max_drawdown_pct <= {SUCCESS_CRITERIA['max_drawdown_pct_max']}%: "
              f"{'PASS' if checks['max_drawdown_pct'][0] else 'FAIL'}  (actual: {checks['max_drawdown_pct'][1]}%)")
        print(f"  cagr_pct >= {SUCCESS_CRITERIA['cagr_pct_min']}%: "
              f"{'PASS' if checks['cagr_pct'][0] else 'FAIL'}  (actual: {checks['cagr_pct'][1]}%)")
        print(f"  VERDICT: {'CLEARS ALL' if checks['all_pass'] else 'DOES NOT CLEAR ALL'}")
        print()

    print("=" * 100)
    print("SIDE-BY-SIDE: RSI2 alone vs RSI2 + regime-gated exits, same TEST window")
    print("=" * 100)
    print_row("RSI2 alone", evaluate(base_test, args.starting_equity))
    print_row("RSI2 + regime gate", test_eval)


if __name__ == "__main__":
    main()
