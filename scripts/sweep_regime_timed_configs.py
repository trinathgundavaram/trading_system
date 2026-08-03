#!/usr/bin/env python3
"""Sweep for engine/regime_timed_backtest.py (2026-07-28) - the new
regime-timing overlay built after docs/beating_the_market_analysis_2026-07-28.md
sec 7g's 9-config entry-filter sweep confirmed every filter that ONLY blocks
new entries failed to beat SPY. This sweeps the one new mechanism's own two
parameters - sma_period (how long a trend read) and buffer_pct (how wide the
hysteresis deadband is, to avoid whipsaw right at the line) - against the
SAME universe/window/cost model/capital constraint as sec 7g's table, so the
two are directly comparable.

Trend-timing performance is well known to be sensitive to exactly these two
choices (a short SMA + no buffer whipsaws constantly; a long SMA + a wide
buffer reacts too late to matter) - this is why it's a curated grid over
both, not a single guess.

Reuses the same infra scripts/sweep_backtest_configs.py built earlier today:
on-disk bar caching (one fetch per ticker across the whole sweep), a
resumable checkpoint (--out, default sweep_regime_checkpoint.json, written
after every config), per-ticker progress printing, and equity_curve.simulate()
for capital-constrained CAGR/Sharpe/MaxDD.

2026-07-28 SAME-DAY FIX, important if you ran this before today's update:
the first version of this script never applied backtest_engine's
standard-tier/exclude-BULL entry_filter (the SAME filter sec 7g's whole
table was built on) - it isolated the regime-timing mechanism so cleanly it
also isolated it from the filter everything else gets compared against. A
real run silently regressed to sec 1's UNFILTERED Run A baseline (matched
its trades/ticker/year almost exactly) while being compared against sec
7g's FILTERED one - a badly misleading result. Fixed: the entry_filter is
now ON by default (--no-entry-filter to turn it off and reproduce the old
comparison, e.g. for its own sake). Every config's name in the output now
encodes filter state ("filter=on"/"filter=off") specifically so an old
checkpoint's unfiltered entries can never silently collide with, and be
skipped as "already done", a new filtered run - they're different keys,
both kept, both clearly labeled.

Usage:
    python3 scripts/sweep_regime_timed_configs.py
    python3 scripts/sweep_regime_timed_configs.py --tickers AAPL MSFT --months 60
    python3 scripts/sweep_regime_timed_configs.py --no-entry-filter   # old, unfiltered comparison
"""
import argparse
import json
import os
import pickle
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

import analyze_backtest_windows as abw  # noqa: E402
import equity_curve as ec  # noqa: E402

CACHE = "/tmp/bt_sweep_bars_cache.pkl"  # SAME cache file as sweep_backtest_configs.py -
                                         # both scripts fetch the exact same 44-ticker
                                         # bars, no reason to pay for the fetch twice.

# Same 44-ticker "Run D" universe as sweep_backtest_configs.py's DEFAULT_TICKERS -
# see docs/beating_the_market_analysis_2026-07-28.md sec 1.
DEFAULT_TICKERS = [
    "PFE", "BABA", "CMCSA", "FUBO", "PLAY", "Z", "CLSK", "RKT", "QXO", "AEHR", "ZG",
    "MSFT", "JEF", "BAC", "GOOGL", "NMRK", "SHLS", "CBRE", "CWK", "C", "NCLH", "TCEHY",
    "WFC", "UMC", "GOOG", "BEKE", "F", "AEO", "PATH", "SGRY", "AGNC", "ADPT", "HIMS",
    "XPEV", "XRAY", "MARA", "FLYW", "MQ", "COMP", "BILL", "BNY", "KMX", "BSX", "PLTR",
]

# (sma_period, buffer_pct) pairs. 200/0 is the raw-crossing baseline (no
# hysteresis at all, isolates whether the band is earning its keep); 200/3
# and 200/5 test wider deadbands at the classic "200-day" lookback; 100/3 and
# 150/3 test faster trend reads (react sooner, whipsaw more); 250/5 tests a
# slower, more patient read.
COMBOS = [
    (200, 0.0),
    (200, 3.0),
    (200, 5.0),
    (100, 3.0),
    (150, 3.0),
    (250, 5.0),
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
            t0 = time.time()
            print(f"  [bars] fetching {ticker} ({n})...", end="", flush=True)
            cache[key] = real(ticker, start, end)
            with open(CACHE, "wb") as f:
                pickle.dump(cache, f)
            print(f" {len(cache[key])} rows in {time.time() - t0:.1f}s", flush=True)
        return cache[key].copy()

    be.fetch_daily_bars = cached
    return be


def _row_from_full_result(name: str, entry: dict) -> dict:
    if entry.get("no_trades"):
        return {"name": name, "n": 0}
    s = entry["summary"]
    windows = entry["windowed"]
    year_2022 = windows.get("2022", {})
    losing_years = sum(1 for y, w in windows.items() if w["n"] >= 10 and w["pf"] is not None and w["pf"] < 1.0)
    total_years = sum(1 for y, w in windows.items() if w["n"] >= 10)
    eq = entry["equity"]
    return {
        "name": name, "n": s["n_trades"], "win_rate": s["win_rate"], "pf": s.get("profit_factor"),
        "expectancy_r": s.get("expectancy_r"),
        "pf_2022": year_2022.get("pf"), "n_2022": year_2022.get("n", 0),
        "losing_years": f"{losing_years}/{total_years}",
        "cagr_pct": eq.get("cagr_pct"), "max_dd_pct": eq.get("max_drawdown_pct"), "sharpe": eq.get("sharpe"),
        "n_rejected": eq.get("n_capital_constrained_rejected", 0),
        "n_blocked": entry.get("n_regime_blocked_entries"),
        "n_forced_exits": entry.get("n_regime_forced_exits"),
    }


def _save_checkpoint(path: str, full_results: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(full_results, f, indent=2, default=str)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    ap.add_argument("--months", type=int, default=60,
                     help="Window length ending today (default: 60 = 5yr, so 2022 is in-sample)")
    ap.add_argument("--execution-cost-bps", type=float, default=25.0)
    ap.add_argument("--risk-pct", type=float, default=1.0,
                     help="Risk-per-trade %% for the CAGR/Sharpe columns (default: 1.0)")
    ap.add_argument("--starting-equity", type=float, default=100_000.0,
                     help="Fixed starting capital - see scripts/equity_curve.py's module docstring.")
    ap.add_argument("--sizing-mode", choices=["current_equity", "high_watermark"],
                     default="current_equity",
                     help="Position-sizing base for the CAGR/Sharpe columns - see "
                          "scripts/equity_curve.py's module docstring, sec 9e (2026-07-28). "
                          "Default 'current_equity' matches every prior sweep in this session.")
    ap.add_argument("--out", default="sweep_regime_checkpoint.json",
                     help="Path to save AND resume per-combo JSON results (default: "
                          "sweep_regime_checkpoint.json). Written after EVERY config, "
                          "resumable on restart. --fresh to ignore it and start over.")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--no-entry-filter", action="store_true",
                     help="Turn OFF the standard-tier/exclude-BULL entry_filter that's ON by "
                          "default. Off means testing the regime-timing mechanism in isolation "
                          "against raw scoring (what the pre-2026-07-28-fix version of this "
                          "script always did) - useful as its own comparison point, but NOT "
                          "apples-to-apples against sec 7g's table unless you know that's what "
                          "you're asking for.")
    args = ap.parse_args()

    entry_filter_on = not args.no_entry_filter

    full_results = {}
    if args.out and os.path.exists(args.out) and not args.fresh:
        try:
            with open(args.out) as f:
                full_results = json.load(f)
            print(f"Resuming from checkpoint {args.out}: {len(full_results)}/{len(COMBOS)} configs already complete.\n")
        except Exception as e:
            print(f"Could not read checkpoint {args.out} ({e}) - starting fresh.\n")
            full_results = {}

    be = _install_bar_cache(total_tickers=len(args.tickers))
    import engine.regime_timed_backtest as rtb
    from config_loader import load_config_dict
    base_cfg = load_config_dict()
    base_cfg = dict(base_cfg)
    base_cfg["backtest"] = dict((base_cfg.get("backtest") or {}), execution_cost_bps=args.execution_cost_bps)
    if entry_filter_on:
        # The SAME filter every sec 7g config was built on - see the
        # module docstring's 2026-07-28 fix note for why this is now the
        # default instead of an opt-in.
        base_cfg["backtest"]["entry_filter"] = {"min_tier": "standard", "exclude_regimes": ["BULL"]}

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(args.months * 30.44))).isoformat()
    print(f"Sweeping {len(COMBOS)} regime-timed configs over {len(args.tickers)} tickers, {start} .. {end} "
          f"({args.execution_cost_bps}bps cost, entry_filter={'ON (standard-tier, exclude BULL)' if entry_filter_on else 'OFF'})")
    print("Fetching/caching bars on first use - subsequent combos reuse the same cache.\n")

    rows = []
    sweep_t0 = time.time()
    for idx, (sma_period, buffer_pct) in enumerate(COMBOS, start=1):
        name = f"sma={sma_period} buffer={buffer_pct}% filter={'on' if entry_filter_on else 'off'}"
        if name in full_results:
            print(f"[{idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
            rows.append(_row_from_full_result(name, full_results[name]))
            continue

        cfg_t0 = time.time()
        print(f"[{idx}/{len(COMBOS)}] {name} ...", flush=True)

        def _ticker_progress(i, total, ticker, _cfg_t0=cfg_t0):
            print(f"    [replay {i}/{total}] {ticker} ({time.time() - _cfg_t0:.0f}s elapsed)", flush=True)

        res = rtb.run_regime_timed_replay(args.tickers, start, end, base_cfg,
                                          sma_period=sma_period, buffer_pct=buffer_pct,
                                          max_hold_days=20, progress_cb=_ticker_progress)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades ({elapsed:.1f}s)", flush=True)
            full_results[name] = {
                "no_trades": True,
                "n_regime_blocked_entries": res.get("n_regime_blocked_entries"),
                "n_entry_filter_rejected": res.get("n_entry_filter_rejected"),
            }
            rows.append({"name": name, "n": 0})
            if args.out:
                _save_checkpoint(args.out, full_results)
            continue

        windows = abw.windowed_stats(res["trades"], 12)
        eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity,
                      sizing_mode=args.sizing_mode)

        full_results[name] = {
            "sma_period": sma_period, "buffer_pct": buffer_pct,
            "summary": s, "windowed": windows, "equity": eq,
            "n_regime_blocked_entries": res.get("n_regime_blocked_entries"),
            "n_regime_forced_exits": res.get("n_regime_forced_exits"),
        }
        if args.out:
            _save_checkpoint(args.out, full_results)
        row = _row_from_full_result(name, full_results[name])
        rows.append(row)
        print(f"    -> {s['n_trades']} trades, PF {s.get('profit_factor')}, "
              f"{res.get('n_entry_filter_rejected', 0)} filter-rejected, "
              f"{res.get('n_regime_blocked_entries')} regime-blocked, "
              f"{res.get('n_regime_forced_exits')} regime-forced exits "
              f"({elapsed:.1f}s, {time.time() - sweep_t0:.1f}s total so far)", flush=True)

    print(f"\nSweep finished in {time.time() - sweep_t0:.1f}s.\n", flush=True)

    print(f"{'Config':<32} {'N':>5} {'PF':>6} {'ExpR':>7} {'PF-2022':>8} {'n22':>5} "
          f"{'LossYrs':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'CapRej':>7} {'Blocked':>7} {'FcdExit':>7}")
    for r in rows:
        if r.get("n", 0) == 0:
            print(f"{r['name']:<32} {'NO TRADES':>5}")
            continue
        print(f"{r['name']:<32} {r['n']:>5} {r['pf']:>6} {r['expectancy_r']:>7} "
              f"{r['pf_2022'] if r['pf_2022'] is not None else '-':>8} {r['n_2022']:>5} "
              f"{r['losing_years']:>8} {r['cagr_pct']:>6}% {r['max_dd_pct']:>6}% "
              f"{r['sharpe'] if r['sharpe'] is not None else '-':>7} {r['n_rejected']:>7} "
              f"{r['n_blocked'] if r['n_blocked'] is not None else '-':>7} "
              f"{r['n_forced_exits'] if r['n_forced_exits'] is not None else '-':>7}")

    print()
    print(f"entry_filter this run: {'ON (standard-tier, exclude BULL)' if entry_filter_on else 'OFF'}. "
          "Blocked = candidates skipped because the regime read was risk-off BEFORE they were even "
          "scored; FcdExit = trades the regime overlay force-closed mid-hold AFTER opening - the new "
          "mechanism docs/beating_the_market_analysis_2026-07-28.md sec 9 exists to test, which none "
          "of sec 7g's nine entry-only filters ever did. PF-2022 is still the direct 'would this hold "
          "against a 2022-like year' answer. Compare CAGR against SPY's own for this window via: "
          f"python3 scripts/spy_benchmark.py --start {start} --end {end}")

    if args.out:
        print(f"\nFull per-combo results saved incrementally to {args.out} (--fresh to force a clean re-run).")


if __name__ == "__main__":
    sys.exit(main())
