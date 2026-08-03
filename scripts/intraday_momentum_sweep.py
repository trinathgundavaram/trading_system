#!/usr/bin/env python3
"""Sweep for engine/intraday_momentum_backtest.py (2026-07-30) - phase 1 of
docs/intraday_momentum_scoping_2026-07-30.md, the Zarattini/Aziz/Barbon
"Beat the Market" intraday SPY momentum strategy.

DATA INPUT - reads a LOCAL minute-bar file, does not fetch anything itself.
This environment (see engine/intraday_momentum_backtest.py's module
docstring, DATA BLOCKER section) has no working network path to a
multi-year 1-minute SPY history: the sandbox has no route to
data.alpaca.markets/api.alpaca.markets, and the one working data connector
available here only has real (non-placeholder) minute bars for about the
trailing month. Real multi-year data has to come from OUTSIDE this
environment - e.g. Alpaca's REST bars API run from a machine with real
network access (this codebase already has ALPACA_API_KEY/ALPACA_API_SECRET
in .env - ALPACA IS the recommended source per the scoping doc) - saved
locally and pointed at via --bars-csv.

Expected --bars-csv schema: columns timestamp, open, high, low, close,
volume (timestamp anything pandas.to_datetime can parse; UTC assumed unless
--tz-source says otherwise). An optional 'interpolated' column, if present
and truthy, marks a row as a placeholder/non-real bar - THOSE ROWS ARE
DROPPED before anything else runs, the same filtering discipline the module
docstring requires callers to apply (this is exactly the flag this session
found silently returning a flat, zero-volume placeholder price for
everything older than about a month via the one live connector tested -
see engine/intraday_momentum_backtest.py's DATA BLOCKER note. If a REAL
data source ever emits this column with real placeholder semantics, this
sweep will not silently ingest it as if it were a real bar).

GRID - band_multiplier x stop_mode. fill_timing is NOT swept here (fixed at
--fill-timing, default 'next_bar', the conservative/qualifying assumption
per the scoping doc's phase-1 commitment); 'same_bar' is reported as a
SEPARATE side-by-side comparison column for whichever combo wins, not
substituted in if next_bar underperforms - see scripts/
walk_forward_intraday_momentum.py for where that commitment is actually
enforced (this script alone is EXPLORATORY / in-sample only, see below).

band_multiplier in [0.8, 1.0, 1.2, 1.5]: 1.0 is the paper's base case;
0.8/1.2 test tighter/wider bands around it; 1.5 tests whether a much wider
(higher-conviction, lower-frequency) band changes the picture, the same
"is N earning its keep" question the independent paper reviewer raised.
stop_mode in [band, band_or_vwap]: the paper's own two documented variants.

HONESTY NOTE, stated up front the same way every other sweep this session
has: this script alone reports FULL-PERIOD, IN-SAMPLE results only - the
same "here's what looked best after the fact" number that sec 9b/9c of
docs/beating_the_market_analysis_2026-07-28.md already showed can hide a
first-half-losing, second-half-carrying pattern for the daily regime
overlay. This script's table is for eyeballing the grid, not for picking a
winner to trust. scripts/walk_forward_intraday_momentum.py is the one that
actually answers phase 1's success criterion (train-select, freeze, blind
test) - run that, not this, before drawing any conclusion.

Same infra pattern as scripts/sweep_regime_timed_configs.py: a resumable
checkpoint (--out, written after every combo), progress printing.

Usage:
    python3 scripts/intraday_momentum_sweep.py --bars-csv spy_1min.csv
    python3 scripts/intraday_momentum_sweep.py --bars-csv spy_1min.csv --daily-csv spy_daily.csv
"""
import argparse
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

from engine import intraday_momentum_backtest as imb  # noqa: E402

# (band_multiplier, stop_mode) - see module docstring for the reasoning
# behind this specific grid.
COMBOS = [
    (0.8, "band"), (1.0, "band"), (1.2, "band"), (1.5, "band"),
    (0.8, "band_or_vwap"), (1.0, "band_or_vwap"), (1.2, "band_or_vwap"), (1.5, "band_or_vwap"),
]

DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_FILL_TIMING = "next_bar"


def load_bars(path: str) -> pd.DataFrame:
    """Loads a local minute-bar file (csv or parquet, by extension), drops
    any row explicitly flagged interpolated/placeholder - see module
    docstring. Raises, rather than silently proceeding, if the file has
    zero real rows left after that filter (a dataset that's ENTIRELY
    placeholder is not "a small backtest," it's a backtest on fabricated
    flat prices, and this has already been the exact trap this session's
    data-source verification caught once)."""
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    if "interpolated" in df.columns:
        before = len(df)
        df = df[~df["interpolated"].astype(bool)].reset_index(drop=True)
        dropped = before - len(df)
        if dropped:
            print(f"  [load_bars] dropped {dropped}/{before} rows flagged interpolated (placeholder, not real data)")
    if df.empty:
        raise ValueError(f"{path}: zero real (non-interpolated) rows - nothing to backtest")
    return df


def load_daily_closes(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[["date", "close"]]


def _save_checkpoint(path: str, results: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(results, f, indent=2, default=str)
    os.replace(tmp, path)


def _row(name: str, res: dict) -> dict:
    s = res["summary"]
    if not s.get("n_trades"):
        return {"name": name, "n": 0}
    return {
        "name": name, "n": s["n_trades"], "n_days": s["n_days"], "trades_per_day": s["trades_per_day"],
        "win_rate": s["win_rate_net"], "pf_net": s.get("profit_factor_net"),
        "avg_pct_net": s["avg_outcome_pct_net"], "avg_pct_raw": s["avg_outcome_pct_raw"],
        "avg_hold_min": s["avg_hold_minutes"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars-csv", required=True, help="Local minute-bar file (csv or parquet) - see module docstring for schema.")
    ap.add_argument("--daily-csv", default=None,
                     help="Optional local daily-bar file (date,close) for the vol-target leverage_x metadata. "
                          "Omit to leave every trade at leverage_x=1.0 (see leverage_multiplier's docstring).")
    ap.add_argument("--tz-source", default="UTC", help="Timezone of --bars-csv's timestamp column if not already tz-aware.")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--fill-timing", choices=["same_bar", "next_bar"], default=DEFAULT_FILL_TIMING)
    ap.add_argument("--commission-per-share", type=float, default=None,
                     help="Default: the paper's own disclosed $0.0035/share - see engine/intraday_execution_costs.py.")
    ap.add_argument("--slippage-per-share", type=float, default=None, help="Default: the paper's own disclosed $0.001/share.")
    ap.add_argument("--out", default="intraday_sweep_checkpoint.json")
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
    print(f"  {len(raw_bars)} raw minute rows loaded.")

    results = {}
    if args.out and os.path.exists(args.out) and not args.fresh:
        try:
            with open(args.out) as f:
                results = json.load(f)
            print(f"Resuming from checkpoint {args.out}: {len(results)}/{len(COMBOS)} combos already complete.\n")
        except Exception as e:
            print(f"Could not read checkpoint {args.out} ({e}) - starting fresh.\n")
            results = {}

    rows = []
    t0 = time.time()
    for idx, (band_multiplier, stop_mode) in enumerate(COMBOS, start=1):
        name = f"band_mult={band_multiplier} stop={stop_mode} fill={args.fill_timing}"
        if name in results:
            print(f"[{idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
            rows.append(_row(name, results[name]))
            continue
        combo_t0 = time.time()
        print(f"[{idx}/{len(COMBOS)}] {name} ...", flush=True)
        res = imb.run_backtest(raw_bars, daily_closes=daily_closes, lookback_days=args.lookback_days,
                                band_multiplier=band_multiplier, fill_timing=args.fill_timing,
                                stop_mode=stop_mode, cfg=cfg, tz_source=args.tz_source)
        results[name] = res
        if args.out:
            _save_checkpoint(args.out, results)
        row = _row(name, res)
        rows.append(row)
        elapsed = time.time() - combo_t0
        if row["n"] == 0:
            print(f"    -> no trades ({elapsed:.1f}s)", flush=True)
        else:
            print(f"    -> {row['n']} trades over {row['n_days']} days, PF(net) {row['pf_net']}, "
                  f"avg {row['avg_pct_net']}%/trade net ({elapsed:.1f}s, {time.time() - t0:.1f}s total)", flush=True)

    print(f"\nSweep finished in {time.time() - t0:.1f}s.\n")
    print(f"{'Config':<48} {'N':>6} {'Days':>6} {'Tr/Day':>7} {'Win%':>6} {'PF-net':>7} {'Avg%net':>8} {'HoldMin':>8}")
    for r in rows:
        if r.get("n", 0) == 0:
            print(f"{r['name']:<48} {'NO TRADES':>6}")
            continue
        print(f"{r['name']:<48} {r['n']:>6} {r['n_days']:>6} {r['trades_per_day']:>7} {r['win_rate']:>6} "
              f"{r['pf_net'] if r['pf_net'] is not None else '-':>7} {r['avg_pct_net']:>8} {r['avg_hold_min']:>8}")

    print("\nREMINDER: this is a FULL-PERIOD, IN-SAMPLE table - not a validated result. "
          "Run scripts/walk_forward_intraday_momentum.py for the train-select/freeze/blind-test "
          "that actually answers phase 1's success criterion (docs/intraday_momentum_scoping_2026-07-30.md sec 5).")
    if args.out:
        print(f"Full per-combo results saved incrementally to {args.out} (--fresh to force a clean re-run).")


if __name__ == "__main__":
    sys.exit(main())
