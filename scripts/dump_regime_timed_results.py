#!/usr/bin/env python3
"""Dump ONE engine/regime_timed_backtest.py config's trades to a
results.json-shaped file, directly consumable by
scripts/analyze_backtest_windows.py --split-date UNCHANGED (2026-07-28,
follow-up to docs/beating_the_market_analysis_2026-07-28.md sec 9b's
question: "does sma=250/buffer=5%'s edge hold up in the back half of the
data on its own, or is it the best of six guesses on a fixed window").

WHY THIS EXISTS. scripts/sweep_regime_timed_configs.py's checkpoint only
ever stored AGGREGATE per-config stats (summary/windowed/equity) - there was
no reason to persist ~750-830 individual trade records x 6 configs when the
sweep itself only needed year-bucketed aggregates. A real sequential split
(analyze_backtest_windows.py's sequential_split_stats(), arbitrary split
date, not just calendar-year buckets) needs the raw trades, which the
checkpoint doesn't have. This dumps exactly ONE config - fast, a single
replay not the full 6-config sweep, and the bar cache from the earlier
sweeps is already warm on this machine - in the {"trades": [...], "config":
{...}} shape analyze_backtest_windows.py already expects from a normal
run_backtest.py output, so that tool runs on it completely unmodified.

Usage:
    python3 scripts/dump_regime_timed_results.py --sma-period 250 --buffer-pct 5.0 --out sma250_buffer5.json
    python3 scripts/analyze_backtest_windows.py sma250_buffer5.json --window-months 12 --split-date 2024-01-01
"""
import argparse
import json
import os
import pickle
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

CACHE = "/tmp/bt_sweep_bars_cache.pkl"  # same cache file both sweep scripts use -
                                         # this run should hit it for every ticker.

# Same 44-ticker "Run D" universe as the two sweep scripts.
DEFAULT_TICKERS = [
    "PFE", "BABA", "CMCSA", "FUBO", "PLAY", "Z", "CLSK", "RKT", "QXO", "AEHR", "ZG",
    "MSFT", "JEF", "BAC", "GOOGL", "NMRK", "SHLS", "CBRE", "CWK", "C", "NCLH", "TCEHY",
    "WFC", "UMC", "GOOG", "BEKE", "F", "AEO", "PATH", "SGRY", "AGNC", "ADPT", "HIMS",
    "XPEV", "XRAY", "MARA", "FLYW", "MQ", "COMP", "BILL", "BNY", "KMX", "BSX", "PLTR",
]


def _install_bar_cache(total_tickers=None):
    import engine.backtest_engine as be
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cache = pickle.load(f)
        except Exception:
            cache = {}
    real = be.fetch_daily_bars
    state = {"fetched": 0}

    def cached(ticker, start, end):
        key = (ticker, start, end)
        if key not in cache:
            state["fetched"] += 1
            n = f"{state['fetched']}/{total_tickers}" if total_tickers else str(state["fetched"])
            print(f"  [bars] fetching {ticker} ({n})...", flush=True)
            cache[key] = real(ticker, start, end)
            with open(CACHE, "wb") as f:
                pickle.dump(cache, f)
        return cache[key].copy()

    be.fetch_daily_bars = cached
    return be


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    ap.add_argument("--months", type=int, default=60,
                     help="Window length ending today (default: 60 = 5yr, matches sec 9b's sweep). "
                          "Ignored if --start/--end are both given.")
    ap.add_argument("--start", default=None,
                     help="Exact window start (YYYY-MM-DD), e.g. to dump ONE side of a walk-forward "
                          "split (2026-07-28 addendum, sec 9e: needed to inspect the TEST period's raw "
                          "trades after the blind walk-forward's CAGR/PF mismatch). Overrides --months.")
    ap.add_argument("--end", default=None, help="Exact window end (YYYY-MM-DD). Overrides --months.")
    ap.add_argument("--execution-cost-bps", type=float, default=25.0)
    ap.add_argument("--sma-period", type=int, required=True)
    ap.add_argument("--buffer-pct", type=float, required=True)
    ap.add_argument("--no-entry-filter", action="store_true",
                     help="Match sec 9a's OLD (unfiltered) comparison instead of sec 9b's default ON.")
    ap.add_argument("--out", required=True, help="Path to write the results.json-shaped output")
    args = ap.parse_args()

    be = _install_bar_cache(total_tickers=len(args.tickers))
    import engine.regime_timed_backtest as rtb
    from config_loader import load_config_dict

    base_cfg = dict(load_config_dict())
    base_cfg["backtest"] = dict(base_cfg.get("backtest") or {}, execution_cost_bps=args.execution_cost_bps)
    if not args.no_entry_filter:
        # The same filter every sec 7g/9b config was built on.
        base_cfg["backtest"]["entry_filter"] = {"min_tier": "standard", "exclude_regimes": ["BULL"]}

    if args.start and args.end:
        start, end = args.start, args.end
    else:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=int(args.months * 30.44))).isoformat()

    def _progress(i, total, ticker):
        print(f"  [replay {i}/{total}] {ticker}", flush=True)

    print(f"Running sma={args.sma_period} buffer={args.buffer_pct}% "
          f"entry_filter={'OFF' if args.no_entry_filter else 'ON'} over {len(args.tickers)} tickers, "
          f"{start} .. {end}\n")
    res = rtb.run_regime_timed_replay(args.tickers, start, end, base_cfg,
                                       sma_period=args.sma_period, buffer_pct=args.buffer_pct,
                                       max_hold_days=20, progress_cb=_progress)

    out = {
        "trades": res["trades"],
        "config": res["config"],
        "summary": res["summary"],
        "n_regime_blocked_entries": res.get("n_regime_blocked_entries"),
        "n_regime_forced_exits": res.get("n_regime_forced_exits"),
        "n_entry_filter_rejected": res.get("n_entry_filter_rejected"),
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)

    n = res["summary"].get("n_trades", 0)
    print(f"\n{n} trades written to {args.out}")
    print(f"Now run (unmodified, existing tool):\n"
          f"  python3 scripts/analyze_backtest_windows.py {args.out} --window-months 12 --split-date 2024-01-01")


if __name__ == "__main__":
    sys.exit(main())
