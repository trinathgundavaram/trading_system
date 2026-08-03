#!/usr/bin/env python3
"""Curated config sweep (2026-07-28) - Trinath's ask: "is there a way to do
simulation what would hold against 2022 year like market case and beat spy
and consistent across other years... by changing the parameters... and
trying different rules, and see what would be the best."

SCOPE, deliberately bounded. This sweeps BACKTEST-ONLY parameters that
engine/backtest_engine.py already supports as safe overrides - entry_filter
fields (min_tier, exclude_regimes, max_vix, max_realized_vol_pct,
min_spy_trend_pct), stop_loss_swing_pct, and sell_rules.take_profit.r_multiple.
None of these touch rules/swing_buy_rules.py's actual scoring logic (the
bucket weights, indicator choices, or vetoes) - that IS the live decision
function, and sweeping it here would mean silently searching over what would
become a real trading-rule change without the review this project's own
release gate requires (classify_change.py, a validation receipt, etc. - see
docs/beating_the_market_analysis_2026-07-28.md sec 2.5 and every
passes_entry_filter() docstring this session). If the results below point at
wanting a genuinely different RULE (not parameter), that's a separate,
bigger conversation - flag it, don't silently fold it into this sweep.

WHY THIS LIST, NOT A FULL GRID. A full cartesian product of every field here
is 100+ combos and mostly wasted compute/attention - many combinations are
already known-bad from this session's history (e.g. realized_vol alone
without a tier/BULL filter). This is a curated, explained list instead,
building on today's actual findings: standard-tier+exclude-BULL is the known
floor; three same-day volatility/regime signals (regime label, VIX, realized
vol) all failed to protect 2022; the untested idea is a 200-day SPY trend
filter (see sec 7d). Edit COMBOS below to add more - it's a plain list of
dicts, not a DSL, on purpose.

METHOD. Bars are fetched once and cached to disk (same trick as
scripts/sweep_exit_params.py) - a sweep costs one network round-trip per
ticker, not one per config, since only the entry filter's math changes
between configs, not the underlying price history. For each config:
  - run_replay() - the exact same replay run_backtest.py uses
  - engine/backtest_engine.py's own summarize() - overall PF/win_rate/expectancy_r
  - analyze_backtest_windows.windowed_stats() - per-year PF, specifically 2022's
  - equity_curve.simulate() - CAGR/Sharpe/MaxDD at a stated risk-per-trade,
    the number actually comparable to SPY (see scripts/equity_curve.py's
    module docstring for the "upper bound, not validated live" caveat, which
    applies here too)

Usage:
    python3 scripts/sweep_backtest_configs.py
    python3 scripts/sweep_backtest_configs.py --tickers AAPL MSFT --months 60
    python3 scripts/sweep_backtest_configs.py --risk-pct 1.0 --out sweep_results.json
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

CACHE = "/tmp/bt_sweep_bars_cache.pkl"

# The 44-ticker universe with full 5-year history, established earlier this
# session (docs/beating_the_market_analysis_2026-07-28.md sec 1's "Run D" -
# the apples-to-apples set, excluding names too recently IPO'd to have 2021
# history at all).
DEFAULT_TICKERS = [
    "PFE", "BABA", "CMCSA", "FUBO", "PLAY", "Z", "CLSK", "RKT", "QXO", "AEHR", "ZG",
    "MSFT", "JEF", "BAC", "GOOGL", "NMRK", "SHLS", "CBRE", "CWK", "C", "NCLH", "TCEHY",
    "WFC", "UMC", "GOOG", "BEKE", "F", "AEO", "PATH", "SGRY", "AGNC", "ADPT", "HIMS",
    "XPEV", "XRAY", "MARA", "FLYW", "MQ", "COMP", "BILL", "BNY", "KMX", "BSX", "PLTR",
]

# name -> entry_filter/exec overrides. Baseline = standard-tier + exclude BULL,
# established as the known floor earlier this session.
COMBOS = {
    "baseline (standard, exclude BULL)": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"]},
    },
    "+ trend>=0 (SPY at/above 200sma)": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"], "min_spy_trend_pct": 0},
    },
    "+ trend>=-5 (5% cushion below 200sma)": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"], "min_spy_trend_pct": -5},
    },
    "+ trend>=0 + stop=16": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"], "min_spy_trend_pct": 0},
        "stop_loss_swing_pct": 16,
    },
    "+ trend>=0 + realized_vol<=25 (defense in depth)": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"],
                          "min_spy_trend_pct": 0, "max_realized_vol_pct": 25},
    },
    "+ trend>=0 + r_multiple=4": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"], "min_spy_trend_pct": 0},
        "r_multiple": 4.0,
    },
    "+ trend>=0 + stop=16 + r_multiple=4 (kitchen sink)": {
        "entry_filter": {"min_tier": "standard", "exclude_regimes": ["BULL"], "min_spy_trend_pct": 0},
        "stop_loss_swing_pct": 16, "r_multiple": 4.0,
    },
    "strong-tier only, exclude BULL": {
        "entry_filter": {"min_tier": "strong", "exclude_regimes": ["BULL"]},
    },
    "strong-tier + trend>=0": {
        "entry_filter": {"min_tier": "strong", "exclude_regimes": ["BULL"], "min_spy_trend_pct": 0},
    },
}


def _install_bar_cache(total_tickers=None):
    """Wraps fetch_daily_bars with an on-disk cache AND a progress printer.
    The network fetch (one call per ticker, first time only - everything
    after reuses the pickle) is the part that can silently sit for minutes
    with zero output, which is exactly what prompted this - print as each
    ticker lands so a running sweep never looks hung."""
    import engine.backtest_engine as be
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cache = pickle.load(f)
        except Exception:
            cache = {}
    real = be.fetch_daily_bars
    state = {"fetched": 0, "hits": 0}

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
        else:
            state["hits"] += 1
        return cache[key].copy()

    be.fetch_daily_bars = cached
    return be


def _apply_overrides(cfg: dict, overrides: dict, risk_level: str) -> dict:
    cfg = dict(cfg)
    if "entry_filter" in overrides:
        bt = dict(cfg.get("backtest") or {})
        bt["entry_filter"] = dict(overrides["entry_filter"])
        cfg["backtest"] = bt
    if "stop_loss_swing_pct" in overrides:
        risk_cfg = dict(cfg.get("risk") or {})
        level_cfg = dict(risk_cfg.get(risk_level) or {})
        level_cfg["stop_loss_swing_pct"] = overrides["stop_loss_swing_pct"]
        risk_cfg[risk_level] = level_cfg
        cfg["risk"] = risk_cfg
    if "r_multiple" in overrides:
        sell_rules = dict(cfg.get("sell_rules") or {})
        tp = dict(sell_rules.get("take_profit") or {})
        tp["r_multiple"] = overrides["r_multiple"]
        sell_rules["take_profit"] = tp
        cfg["sell_rules"] = sell_rules
    return cfg


def _row_from_full_result(name: str, entry: dict) -> dict:
    """Rebuilds a leaderboard row from a full_results[name] entry - shared by
    the just-ran path and the loaded-from-checkpoint path so both produce
    identical rows."""
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
    }


def _save_checkpoint(path: str, full_results: dict):
    """Atomic write (temp file + rename) so a crash mid-write never leaves a
    truncated/corrupt checkpoint that a resume can't read."""
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
                     help="Fixed starting capital for the equity-curve columns (default: $100,000). "
                          "Positions that would exceed available cash are rejected, not resized or queued - "
                          "see scripts/equity_curve.py's module docstring.")
    ap.add_argument("--out", default="sweep_checkpoint.json",
                     help="Path to save AND resume per-combo JSON results (default: sweep_checkpoint.json). "
                          "2026-07-28: a full 9-config sweep took ~3.4 hours - this file is now written after "
                          "EVERY config finishes, not just at the very end, so a killed/crashed run can restart "
                          "and skip whatever's already in here instead of re-running from config 1. Pass "
                          "--fresh to ignore an existing one and start over.")
    ap.add_argument("--fresh", action="store_true",
                     help="Ignore any existing --out checkpoint file and re-run every config from scratch.")
    args = ap.parse_args()

    full_results = {}
    if args.out and os.path.exists(args.out) and not args.fresh:
        try:
            with open(args.out) as f:
                full_results = json.load(f)
            done = [n for n in full_results if n in COMBOS]
            print(f"Resuming from checkpoint {args.out}: {len(done)}/{len(COMBOS)} configs already complete "
                  f"({', '.join(done) if done else 'none'}).\n")
        except Exception as e:
            print(f"Could not read checkpoint {args.out} ({e}) - starting fresh.\n")
            full_results = {}

    be = _install_bar_cache(total_tickers=len(args.tickers))
    from config_loader import load_config_dict
    base_cfg = load_config_dict()
    base_cfg = dict(base_cfg)
    base_cfg["backtest"] = dict((base_cfg.get("backtest") or {}), execution_cost_bps=args.execution_cost_bps)
    risk_level = base_cfg.get("risk_level", "TURBO")

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(args.months * 30.44))).isoformat()
    print(f"Sweeping {len(COMBOS)} configs over {len(args.tickers)} tickers, {start} .. {end} "
          f"({args.execution_cost_bps}bps cost, risk_level={risk_level})")
    print("Fetching/caching bars on first use - subsequent combos reuse the same cache.\n")

    rows = []
    sweep_t0 = time.time()
    for idx, (name, overrides) in enumerate(COMBOS.items(), start=1):
        if name in full_results:
            print(f"[{idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
            rows.append(_row_from_full_result(name, full_results[name]))
            continue

        cfg_t0 = time.time()
        print(f"[{idx}/{len(COMBOS)}] {name} ...", flush=True)
        cfg = _apply_overrides(base_cfg, overrides, risk_level)

        def _ticker_progress(i, total, ticker, _cfg_t0=cfg_t0):
            print(f"    [replay {i}/{total}] {ticker} ({time.time() - _cfg_t0:.0f}s elapsed)", flush=True)

        res = be.run_replay(args.tickers, start, end, cfg, max_hold_days=20, progress_cb=_ticker_progress)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades ({elapsed:.1f}s)", flush=True)
            full_results[name] = {"no_trades": True}
            rows.append({"name": name, "n": 0})
            if args.out:
                _save_checkpoint(args.out, full_results)
            continue

        windows = abw.windowed_stats(res["trades"], 12)

        # 2026-07-28 afternoon: capital-constrained by default (Trinath: "do
        # not test for unlimited cash... go for a fixed start capital") -
        # see scripts/equity_curve.py's module docstring for the model.
        eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity)

        full_results[name] = {"config_overrides": overrides, "summary": s,
                               "windowed": windows, "equity": eq}
        if args.out:
            # Written after EVERY config, not just at the end - the whole
            # point of a checkpoint. A killed/crashed run resumes from here.
            _save_checkpoint(args.out, full_results)
        row = _row_from_full_result(name, full_results[name])
        rows.append(row)
        print(f"    -> {s['n_trades']} trades, PF {s.get('profit_factor')} ({elapsed:.1f}s, "
              f"{time.time() - sweep_t0:.1f}s total so far)", flush=True)

    print(f"\nSweep finished in {time.time() - sweep_t0:.1f}s.\n", flush=True)

    print(f"{'Config':<45} {'N':>5} {'PF':>6} {'ExpR':>7} {'PF-2022':>8} {'n22':>5} "
          f"{'LossYrs':>8} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7} {'CapRej':>7}")
    for r in rows:
        if r.get("n", 0) == 0:
            print(f"{r['name']:<45} {'NO TRADES':>5}")
            continue
        print(f"{r['name']:<45} {r['n']:>5} {r['pf']:>6} {r['expectancy_r']:>7} "
              f"{r['pf_2022'] if r['pf_2022'] is not None else '-':>8} {r['n_2022']:>5} "
              f"{r['losing_years']:>8} {r['cagr_pct']:>6}% {r['max_dd_pct']:>6}% "
              f"{r['sharpe'] if r['sharpe'] is not None else '-':>7} {r['n_rejected']:>7}")

    print()
    print("PF-2022 is the direct answer to 'would this hold against a 2022-like year' - look there first, "
          "not at the aggregate PF column, which a strong 2025 can flatter (see docs/"
          "beating_the_market_analysis_2026-07-28.md sec 1). CAGR/Sharpe/MaxDD now assume a FIXED starting "
          f"capital of ${args.starting_equity:,.0f} - CapRej shows how many candidates were turned away for "
          "lack of cash (a high count there means capital, not the filter, is the binding constraint). "
          f"Compare against SPY's own CAGR for this window via: python3 scripts/spy_benchmark.py "
          f"--start {start} --end {end}")

    if args.out:
        print(f"\nFull per-combo results saved incrementally throughout the run to {args.out} "
              f"(delete it, or pass --fresh, to force a full re-run instead of a resume next time).")


if __name__ == "__main__":
    sys.exit(main())
