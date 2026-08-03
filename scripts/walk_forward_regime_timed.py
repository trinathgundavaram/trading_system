#!/usr/bin/env python3
"""Genuine blind walk-forward for engine/regime_timed_backtest.py (2026-07-28)
- the test recommended four separate times this session (sec 2.5, sec 6 item
4, sec 9b, sec 9c of docs/beating_the_market_analysis_2026-07-28.md) and
never actually run, until now.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER SWEEP THIS SESSION. Every prior
config comparison (sec 7g's 9 configs, sec 9b's 6 regime-timed configs)
picked a "winner" by looking at results computed over the FULL 2021-2026
window - including the very data any later validation would be tested
against. Sec 9c's sequential split showed why that matters: sma=250/
buffer=5% (sec 9b's full-period winner) had a NET LOSING first half
(2021-2023, PF 0.91) and its apparent edge was concentrated almost entirely
in the back half (2024-2026, PF 1.24) - a config search over 2021-2023 ALONE
might have picked something else entirely.

This script actually does that: it selects the best (sma_period, buffer_pct)
from the SAME curated 6-combo list sec 9b used, using ONLY TRAIN-window
(2021-2023) results - the TEST window (2024-2026) is never looked at during
selection. The chosen combo is then frozen and run UNMODIFIED on the TEST
window, fresh $100k capital. If the TRAIN-selected winner matches sec 9b's
full-period winner AND its TEST performance holds up, that is real evidence
of a time-stable edge. If the TRAIN winner is a DIFFERENT combo, or its TEST
performance disappoints, that is direct evidence the full-period pick was
fit to a favorable stretch rather than a generalizing parameter choice -
either answer is a real, useful finding, not a failure of this script.

SELECTION METRIC, stated up front and fixed before any results are seen:
TRAIN-period CAGR (capital-constrained, $100k, 1% risk/trade - the same
equity_curve.simulate() every other sweep this session uses). CAGR is the
metric that actually answers "beat the market," which is the entire
question this whole analysis chain exists to answer - not PF, not Sharpe,
not a multi-metric eyeball call (which is how sec 9b's winner was actually
chosen, and part of why sec 9c's split raised a real caution).

Same infra as the other sweep scripts: on-disk bar cache, a resumable
two-phase checkpoint (TRAIN sweep, then a single TEST run for whichever
combo TRAIN selects), per-ticker progress printing.

Usage:
    python3 scripts/walk_forward_regime_timed.py
    python3 scripts/walk_forward_regime_timed.py --split-date 2024-01-01
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

import equity_curve as ec  # noqa: E402
from sweep_regime_timed_configs import COMBOS, DEFAULT_TICKERS  # noqa: E402 - SAME 6 combos, no drift

CACHE = "/tmp/bt_sweep_bars_cache.pkl"  # same cache every sweep script in this session uses


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


def _save_checkpoint(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _run_one(rtb, be, tickers, start, end, base_cfg, sma_period, buffer_pct, cfg_t0, name):
    def _ticker_progress(i, total, ticker, _cfg_t0=cfg_t0):
        print(f"    [replay {i}/{total}] {ticker} ({time.time() - _cfg_t0:.0f}s elapsed)", flush=True)

    res = rtb.run_regime_timed_replay(tickers, start, end, base_cfg,
                                       sma_period=sma_period, buffer_pct=buffer_pct,
                                       max_hold_days=20, progress_cb=_ticker_progress)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    ap.add_argument("--months-total", type=int, default=60,
                     help="Full window length ending today (default: 60 = 5yr, same as sec 9b's sweep)")
    ap.add_argument("--split-date", default="2024-01-01",
                     help="TRAIN is [window_start, split_date); TEST is [split_date, today]. "
                          "Same boundary sec 9c's sequential split used, for continuity.")
    ap.add_argument("--execution-cost-bps", type=float, default=25.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--sizing-mode", choices=["current_equity", "high_watermark"],
                     default="current_equity",
                     help="Position-sizing base - see scripts/equity_curve.py's module docstring, "
                          "sec 9e (2026-07-28). Default matches every prior run in this session.")
    ap.add_argument("--out", default="walk_forward_checkpoint.json")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    window_start = (date.today() - timedelta(days=int(args.months_total * 30.44))).isoformat()
    window_end = date.today().isoformat()
    train_start, train_end = window_start, args.split_date
    test_start, test_end = args.split_date, window_end

    data = {"train": {}, "test": None, "winner": None}
    if args.out and os.path.exists(args.out) and not args.fresh:
        try:
            with open(args.out) as f:
                data = json.load(f)
            print(f"Resuming from checkpoint {args.out}: "
                  f"{len(data.get('train', {}))}/{len(COMBOS)} TRAIN configs done, "
                  f"TEST {'done' if data.get('test') else 'not run yet'}.\n")
        except Exception as e:
            print(f"Could not read checkpoint {args.out} ({e}) - starting fresh.\n")
            data = {"train": {}, "test": None, "winner": None}

    be = _install_bar_cache(total_tickers=len(args.tickers))
    import engine.regime_timed_backtest as rtb
    from config_loader import load_config_dict
    base_cfg = dict(load_config_dict())
    base_cfg["backtest"] = dict(base_cfg.get("backtest") or {}, execution_cost_bps=args.execution_cost_bps)
    base_cfg["backtest"]["entry_filter"] = {"min_tier": "standard", "exclude_regimes": ["BULL"]}

    print(f"BLIND WALK-FORWARD. TRAIN={train_start}..{train_end}  TEST={test_start}..{test_end}")
    print(f"Selection metric (fixed BEFORE any results are seen): TRAIN-period CAGR, "
          f"capital-constrained at ${args.starting_equity:,.0f}, {args.risk_pct}% risk/trade.\n")

    # ---- Phase 1: TRAIN sweep over the same 6 combos sec 9b used --------
    sweep_t0 = time.time()
    for idx, (sma_period, buffer_pct) in enumerate(COMBOS, start=1):
        name = f"sma={sma_period} buffer={buffer_pct}%"
        if name in data["train"]:
            print(f"[TRAIN {idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
            continue
        cfg_t0 = time.time()
        print(f"[TRAIN {idx}/{len(COMBOS)}] {name} ...", flush=True)
        res = _run_one(rtb, be, args.tickers, train_start, train_end, base_cfg,
                       sma_period, buffer_pct, cfg_t0, name)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades ({elapsed:.1f}s)", flush=True)
            data["train"][name] = {"sma_period": sma_period, "buffer_pct": buffer_pct, "no_trades": True}
            _save_checkpoint(args.out, data)
            continue
        eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity,
                          sizing_mode=args.sizing_mode)
        data["train"][name] = {
            "sma_period": sma_period, "buffer_pct": buffer_pct,
            "n_trades": s["n_trades"], "pf": s.get("profit_factor"), "expectancy_r": s.get("expectancy_r"),
            "cagr_pct": eq.get("cagr_pct"), "max_dd_pct": eq.get("max_drawdown_pct"), "sharpe": eq.get("sharpe"),
        }
        _save_checkpoint(args.out, data)
        print(f"    -> {s['n_trades']} trades, PF {s.get('profit_factor')}, "
              f"CAGR {eq.get('cagr_pct')}% ({elapsed:.1f}s, {time.time() - sweep_t0:.1f}s total so far)",
              flush=True)

    print(f"\nTRAIN phase finished in {time.time() - sweep_t0:.1f}s.\n")

    # ---- Rank TRAIN results by the pre-committed metric: CAGR -----------
    ranked = sorted(
        (v for v in data["train"].values() if not v.get("no_trades") and v.get("cagr_pct") is not None),
        key=lambda v: v["cagr_pct"], reverse=True,
    )
    print(f"{'Config':<20} {'N':>5} {'PF':>6} {'ExpR':>7} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7}")
    for v in ranked:
        name = f"sma={v['sma_period']} buffer={v['buffer_pct']}%"
        print(f"{name:<20} {v['n_trades']:>5} {v['pf']:>6} {v['expectancy_r']:>7} "
              f"{v['cagr_pct']:>6}% {v['max_dd_pct']:>6}% {v['sharpe']:>7}")
    for v in data["train"].values():
        if v.get("no_trades"):
            print(f"sma={v['sma_period']} buffer={v['buffer_pct']}%  NO TRADES on TRAIN")

    if not ranked:
        print("\nNo TRAIN config produced any trades - cannot select a winner. Stopping.")
        return 1

    winner = ranked[0]
    winner_name = f"sma={winner['sma_period']} buffer={winner['buffer_pct']}%"
    print(f"\nBLIND SELECTION (TRAIN-only CAGR): {winner_name} "
          f"(TRAIN CAGR {winner['cagr_pct']}%, PF {winner['pf']})")
    if (winner["sma_period"], winner["buffer_pct"]) == (250, 5.0):
        print("This MATCHES sec 9b's full-period winner (sma=250, buffer=5%) - a real point in favor "
              "of that choice generalizing, not just fitting the back half.")
    else:
        print(f"This does NOT match sec 9b's full-period winner (sma=250, buffer=5%) - direct evidence "
              f"the full-period pick was fit to the 2024-2026 stretch rather than a config that would "
              f"have been chosen blind.")
    data["winner"] = {"sma_period": winner["sma_period"], "buffer_pct": winner["buffer_pct"]}
    _save_checkpoint(args.out, data)

    # ---- Phase 2: run the FROZEN winner on the untouched TEST window ----
    if data.get("test") is not None:
        print(f"\n[TEST] {winner_name} ... already in checkpoint, skipping")
    else:
        print(f"\n[TEST] {winner_name} on {test_start}..{test_end} (never seen during selection) ...",
              flush=True)
        cfg_t0 = time.time()
        res = _run_one(rtb, be, args.tickers, test_start, test_end, base_cfg,
                       winner["sma_period"], winner["buffer_pct"], cfg_t0, winner_name)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades on TEST ({elapsed:.1f}s)")
            data["test"] = {"no_trades": True}
        else:
            eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity,
                              sizing_mode=args.sizing_mode)
            data["test"] = {
                "n_trades": s["n_trades"], "pf": s.get("profit_factor"), "expectancy_r": s.get("expectancy_r"),
                "cagr_pct": eq.get("cagr_pct"), "max_dd_pct": eq.get("max_drawdown_pct"), "sharpe": eq.get("sharpe"),
            }
            print(f"    -> {s['n_trades']} trades, PF {s.get('profit_factor')}, "
                  f"CAGR {eq.get('cagr_pct')}%, MaxDD {eq.get('max_drawdown_pct')}%, "
                  f"Sharpe {eq.get('sharpe')} ({elapsed:.1f}s)")
        _save_checkpoint(args.out, data)

    print(f"\n{'=' * 70}")
    print(f"BLIND WALK-FORWARD RESULT for {winner_name} (selected on TRAIN {train_start}..{train_end}, "
          f"never touching TEST):")
    t = data.get("test") or {}
    if t.get("no_trades"):
        print("  TEST: no trades - cannot evaluate.")
    elif t:
        print(f"  TEST ({test_start}..{test_end}): n={t['n_trades']}  PF={t['pf']}  "
              f"CAGR={t['cagr_pct']}%  MaxDD={t['max_dd_pct']}%  Sharpe={t['sharpe']}")
        print(f"  Compare against SPY's own TEST-period CAGR via:")
        print(f"    python3 scripts/spy_benchmark.py --start {test_start} --end {test_end}")
    print(f"{'=' * 70}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
