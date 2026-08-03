#!/usr/bin/env python3
"""Genuine blind walk-forward for engine/intraday_momentum_backtest.py
(2026-07-30) - the phase-1 GATE from docs/
intraday_momentum_scoping_2026-07-30.md sec 5: "edge must survive next_bar
fills, per-share costs, and a frozen walk-forward split... If it doesn't,
stop there." scripts/intraday_momentum_sweep.py's table is exploratory,
in-sample, and explicitly NOT this test - see that script's own module
docstring. THIS script is the one whose result actually decides whether
phase 2 (spending ~$300 on FirstRate Data) is warranted.

DESIGN LOCKED BEFORE ANY RESULT IS SEEN (2026-07-30) - same discipline as
scripts/walk_forward_regime_timed.py's "SELECTION METRIC, stated up front
and fixed before any results are seen" section, and for the identical
reason: sec 9c of docs/beating_the_market_analysis_2026-07-28.md found the
daily regime overlay's apparent full-period edge was concentrated almost
entirely in one half of the window - a config search over the full period
would never have surfaced that. Committing the design here, in writing,
before this script has ever been run against real data, is what makes that
kind of after-the-fact rationalization impossible later.

  1. SPLIT POLICY: --split-date if given (an explicit calendar date -
     preferred once the real fetched data's actual range is known);
     otherwise --train-frac (default 0.7) of the loaded dataset's own
     session dates, in calendar order, is TRAIN and the rest is TEST. 0.7
     is a plain, defensible default chosen for being generic to whatever
     window ends up available (unlike the daily regime work, this
     strategy's real data source and date range were not yet nailed down
     at the time this script was written - see the scoping doc's open
     item #1) - NOT tuned by looking at this strategy's own results on any
     particular window.
  2. GRID: the SAME 8 (band_multiplier, stop_mode) combos as
     scripts/intraday_momentum_sweep.py's COMBOS - imported, not
     duplicated, so the two scripts can never silently drift apart.
  3. FILL TIMING: fixed at 'next_bar' for both selection and the frozen
     test - the conservative, realistically-achievable assumption, per the
     scoping doc's explicit commitment that same_bar is reported
     alongside for comparison ONLY and is never substituted in if
     next_bar underperforms. This script also reports the SAME winning
     combo's same_bar numbers on both TRAIN and TEST, side by side, for
     exactly that comparison - never used for selection.
  4. SELECTION METRIC: TRAIN-period net profit factor (profit_factor_net),
     among combos clearing a minimum TRAIN trade-count floor
     (--min-train-trades, default 30 - a combo with a handful of trades
     winning on profit factor is noise, not a signal, the same reasoning
     sweep_regime_timed_configs.py's windowed_stats applies with its own
     n>=10 floor per window). Net PF (not raw, not avg_outcome_pct) is the
     trade-level metric that already bakes in the paper's own disclosed
     cost assumption - the single number this whole exercise exists to
     stress-test.
  5. FREEZE. Whichever combo wins on TRAIN is run, completely unmodified,
     on TEST. TEST is never inspected, scored, or compared against ANY
     other combo before that freeze - this script's own control flow
     enforces that structurally (the TEST run only happens after the
     TRAIN-only selection loop has fully finished and committed to one
     name).

WHAT COUNTS AS "SURVIVED" (read together with the scoping doc, not
re-litigated here): the frozen TEST run's net profit factor > 1.0, run on
next_bar fills and the paper's own per-share cost assumption. That is
necessary, not sufficient - a single walk-forward split is one data point,
not a robustness guarantee, and this script's own printed output says so
rather than declaring victory on a bare PF>1.

Usage:
    python3 scripts/walk_forward_intraday_momentum.py --bars-csv spy_1min.csv
    python3 scripts/walk_forward_intraday_momentum.py --bars-csv spy_1min.csv --split-date 2024-06-01
"""
import argparse
import json
import os
import sys
import time
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

from engine import intraday_momentum_backtest as imb  # noqa: E402
from intraday_momentum_sweep import COMBOS, load_bars, load_daily_closes  # noqa: E402

MIN_TRAIN_TRADES_DEFAULT = 30
TRAIN_FRAC_DEFAULT = 0.7


def _session_dates(raw_bars: pd.DataFrame, tz_source: str) -> list:
    prepared = imb.prepare_bars(raw_bars, tz_source=tz_source)
    return sorted(prepared["date"].unique())


def _split_bars(raw_bars: pd.DataFrame, split_date, tz_source: str) -> tuple:
    prepared = imb.prepare_bars(raw_bars, tz_source=tz_source)
    train = prepared[prepared["date"] < split_date]
    test = prepared[prepared["date"] >= split_date]
    # Hand back RAW-shaped frames (prepare_bars is idempotent on its own
    # output - calling it twice is harmless and keeps run_backtest's own
    # interface, which expects raw bars, unchanged) rather than plumbing a
    # second "already prepared" code path through run_backtest.
    return train.copy(), test.copy()


def _run_one(raw_bars, daily_closes, band_multiplier, stop_mode, fill_timing, cfg, tz_source, lookback_days):
    return imb.run_backtest(raw_bars, daily_closes=daily_closes, lookback_days=lookback_days,
                             band_multiplier=band_multiplier, fill_timing=fill_timing,
                             stop_mode=stop_mode, cfg=cfg, tz_source=tz_source)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars-csv", required=True)
    ap.add_argument("--daily-csv", default=None)
    ap.add_argument("--tz-source", default="UTC")
    ap.add_argument("--lookback-days", type=int, default=14)
    ap.add_argument("--split-date", default=None, help="Explicit YYYY-MM-DD. Overrides --train-frac if given.")
    ap.add_argument("--train-frac", type=float, default=TRAIN_FRAC_DEFAULT)
    ap.add_argument("--min-train-trades", type=int, default=MIN_TRAIN_TRADES_DEFAULT)
    ap.add_argument("--commission-per-share", type=float, default=None)
    ap.add_argument("--slippage-per-share", type=float, default=None)
    ap.add_argument("--out", default="intraday_walk_forward_checkpoint.json")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    cfg = {}
    if args.commission_per_share is not None or args.slippage_per_share is not None:
        from engine.intraday_execution_costs import DEFAULT_PER_SHARE_COMMISSION, DEFAULT_PER_SHARE_SLIPPAGE
        cfg["intraday_backtest"] = {
            "commission_per_share": args.commission_per_share if args.commission_per_share is not None else DEFAULT_PER_SHARE_COMMISSION,
            "slippage_per_share": args.slippage_per_share if args.slippage_per_share is not None else DEFAULT_PER_SHARE_SLIPPAGE,
        }

    print(f"Loading bars from {args.bars_csv} ...")
    raw_bars = load_bars(args.bars_csv)
    daily_closes = load_daily_closes(args.daily_csv) if args.daily_csv else None

    dates = _session_dates(raw_bars, args.tz_source)
    if args.split_date:
        split_date = date.fromisoformat(args.split_date)
    else:
        split_idx = int(len(dates) * args.train_frac)
        split_date = dates[split_idx]
    train_raw, test_raw = _split_bars(raw_bars, split_date, args.tz_source)
    n_train_days = sum(1 for d in dates if d < split_date)
    n_test_days = len(dates) - n_train_days
    print(f"Split: {dates[0]} .. {dates[-1]} ({len(dates)} sessions). "
          f"TRAIN = {dates[0]} .. {split_date} ({n_train_days} sessions), "
          f"TEST = {split_date} .. {dates[-1]} ({n_test_days} sessions).\n")
    if n_train_days < 20 or n_test_days < 10:
        print(f"WARNING: TRAIN ({n_train_days}) or TEST ({n_test_days}) session count is very low - "
              "a walk-forward split needs enough sessions in EACH half to mean anything. Proceeding "
              "anyway since this is a mechanical check, not a hard stop, but treat any result below "
              "with real skepticism if these counts are small.\n")

    results = {}
    if args.out and os.path.exists(args.out) and not args.fresh:
        try:
            with open(args.out) as f:
                results = json.load(f)
            print(f"Resuming from checkpoint {args.out}.\n")
        except Exception as e:
            print(f"Could not read checkpoint ({e}) - starting fresh.\n")
            results = {}

    # --- Phase A: TRAIN-only selection. TEST is never touched here. -------
    print("=== PHASE A: TRAIN-only selection (next_bar fills, all 8 combos) ===")
    train_rows = []
    t0 = time.time()
    for idx, (band_multiplier, stop_mode) in enumerate(COMBOS, start=1):
        name = f"band_mult={band_multiplier} stop={stop_mode}"
        key = f"train::{name}"
        if key in results:
            print(f"[{idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
        else:
            combo_t0 = time.time()
            print(f"[{idx}/{len(COMBOS)}] {name} ...", flush=True)
            res = _run_one(train_raw, daily_closes, band_multiplier, stop_mode, "next_bar", cfg,
                            args.tz_source, args.lookback_days)
            results[key] = res
            if args.out:
                _save_checkpoint(args.out, results)
            print(f"    -> {res['summary'].get('n_trades', 0)} trades, "
                  f"PF(net) {res['summary'].get('profit_factor_net')} ({time.time() - combo_t0:.1f}s)", flush=True)
        s = results[key]["summary"]
        train_rows.append({"band_multiplier": band_multiplier, "stop_mode": stop_mode, "name": name,
                            "n_trades": s.get("n_trades", 0), "pf_net": s.get("profit_factor_net")})

    eligible = [r for r in train_rows if r["n_trades"] >= args.min_train_trades and r["pf_net"] is not None]
    print(f"\nTRAIN results (min {args.min_train_trades} trades to be eligible):")
    for r in train_rows:
        flag = "" if r in eligible else "  [INELIGIBLE - too few trades or no losing trades to compute PF]"
        print(f"  {r['name']:<32} n={r['n_trades']:>5}  PF(net)={r['pf_net']}{flag}")

    if not eligible:
        print("\nNo combo cleared the minimum TRAIN trade count. Cannot select a winner - "
              "phase 1 does not clear its own gate on this dataset. Stopping (per the scoping doc: "
              "'if it doesn't, stop there').")
        return

    winner = max(eligible, key=lambda r: r["pf_net"])
    print(f"\nSELECTED (TRAIN-only, highest net PF among eligible combos): {winner['name']} "
          f"(TRAIN n={winner['n_trades']}, TRAIN PF(net)={winner['pf_net']})")

    # --- Phase B: freeze, run on TEST, unmodified, next_bar fills. --------
    print("\n=== PHASE B: frozen TEST run (next_bar fills - the qualifying assumption) ===")
    test_key = f"test::{winner['name']}"
    if test_key in results:
        print("(already in checkpoint)")
    else:
        res = _run_one(test_raw, daily_closes, winner["band_multiplier"], winner["stop_mode"], "next_bar",
                        cfg, args.tz_source, args.lookback_days)
        results[test_key] = res
        if args.out:
            _save_checkpoint(args.out, results)
    test_summary = results[test_key]["summary"]
    test_n = test_summary.get("n_trades", 0)
    test_pf = test_summary.get("profit_factor_net")
    print(f"TEST: {test_n} trades, PF(net)={test_pf}, avg%/trade(net)={test_summary.get('avg_outcome_pct_net')}")

    # --- Phase C: same_bar reported for comparison ONLY, both windows. ----
    print("\n=== same_bar fills, SAME winning combo, reported for comparison ONLY (never selection) ===")
    for label, bars in (("TRAIN", train_raw), ("TEST", test_raw)):
        key = f"{label.lower()}_same_bar::{winner['name']}"
        if key not in results:
            res = _run_one(bars, daily_closes, winner["band_multiplier"], winner["stop_mode"], "same_bar",
                            cfg, args.tz_source, args.lookback_days)
            results[key] = res
            if args.out:
                _save_checkpoint(args.out, results)
        s = results[key]["summary"]
        print(f"  {label} (same_bar): {s.get('n_trades', 0)} trades, PF(net)={s.get('profit_factor_net')}")

    # --- Verdict against the scoping doc's phase-1 success criterion. -----
    print("\n=== VERDICT (docs/intraday_momentum_scoping_2026-07-30.md sec 5's phase-1 criterion) ===")
    if test_n < args.min_train_trades:
        print(f"TEST produced only {test_n} trades (< {args.min_train_trades}) - too few to call a verdict "
              "either way. This is a 'need more data' result, not a pass or a fail.")
    elif test_pf is not None and test_pf > 1.0:
        print(f"TEST net PF = {test_pf} > 1.0: the TRAIN-selected combo's edge SURVIVED next_bar fills, "
              "per-share costs, and a frozen out-of-sample test. Necessary, not sufficient, for phase 2 - "
              "this is one split on one dataset, not a robustness guarantee. See this script's own module "
              "docstring before treating this as final.")
    else:
        print(f"TEST net PF = {test_pf}: the edge did NOT survive the frozen out-of-sample test under the "
              "conservative fill assumption. Per the scoping doc: 'if it doesn't, stop there' - this is "
              "the $0 answer to whether phase 2 (FirstRate Data, ~$300) is worth spending on.")


def _save_checkpoint(path: str, results: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(results, f, indent=2, default=str)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
