#!/usr/bin/env python3
"""Genuine blind walk-forward for the CORE, UNFILTERED stock-picking engine
(2026-07-31) - engine/backtest_engine.py's run_replay(), the exact real
rules/hard_vetoes.py + rules/swing_buy_rules.py pipeline that actually runs
in paper trading today, with NO cfg["backtest"]["entry_filter"] override.

WHY THIS IS DIFFERENT FROM EVERY OTHER WALK-FORWARD THIS SESSION. Every
prior test - sec 7g's 9 entry-filter configs, sec 9's regime-timing
overlay, this session's intraday momentum module - tested something
STACKED ON TOP of the core engine's own picks: a filter that blocks some
entries, or an overlay that force-closes some exits, or an entirely
separate strategy. None of them asked the more basic question: does the
core engine's OWN stock selection, unfiltered, running with no extra gate
at all, have real, out-of-sample edge? scripts/sweep_backtest_configs.py's
own docstring explicitly drew this line on purpose ("None of these touch
rules/swing_buy_rules.py's actual scoring logic... that IS the live
decision function, and sweeping it here would mean silently searching over
what would become a real trading-rule change") - this script stays on the
safe side of that same line: it does NOT touch rules/swing_buy_rules.py's
scoring logic, bucket weights, or vetoes. It sweeps the two things
config.yaml already exposes as safe, already-used knobs on the exact same
unmodified scoring function: `risk_level` (which of the four real,
already-configured tiers - CONSERVATIVE/MODERATE/AGGRESSIVE/TURBO, each
with its own real buy_score_threshold_pct and stop_loss_swing_pct straight
from config.yaml, not invented values) and `sell_rules.take_profit.
r_multiple`. risk_level itself has never been swept before this session -
every prior sweep (sec 7g, sec 9) held it fixed at whatever config.yaml's
live value is (TURBO) and only layered filters on top of it.

DESIGN LOCKED BEFORE ANY RESULT IS SEEN (2026-07-31) - same discipline as
scripts/walk_forward_regime_timed.py and scripts/
walk_forward_intraday_momentum.py, for the identical reason: sec 9c/9d
already found a full-period "best" pick can hide a pattern that doesn't
survive a genuine held-out test:
  1. GRID: risk_level in [CONSERVATIVE, MODERATE, AGGRESSIVE, TURBO] x
     r_multiple in [2.0, 3.0, 4.0] - 12 combos, each risk_level using its
     REAL config.yaml threshold/stop (not a synthetic sweep value).
  2. NO entry_filter override, at all, ever, in this script - that is the
     entire point (see above). If a future run wants to compare the
     filtered vs unfiltered engine's OWN walk-forward result, that's a
     second script, not a flag on this one silently changing what "core
     engine" means run to run.
  3. UNIVERSE/WINDOW: same DEFAULT_TICKERS (the 44-ticker "Run D" set) and
     the same default --months-total 60 / --split-date 2024-01-01 as
     scripts/walk_forward_regime_timed.py, so this result sits on the
     identical TRAIN/TEST boundary as sec 9d's number and is directly
     comparable to it, not a different window that happens to also be
     called "walk-forward."
  4. SELECTION METRIC: TRAIN-period CAGR (capital-constrained,
     $100k, 1% risk/trade, current_equity sizing, reject capital mode -
     equity_curve.simulate()'s unchanged defaults, same as every other
     walk-forward this session), among combos clearing a minimum TRAIN
     trade-count floor (--min-train-trades, default 30 - sec 9's own
     scripts didn't enforce this explicitly; adding it here since a
     handful of trades winning on CAGR is noise, the same reasoning
     scripts/walk_forward_intraday_momentum.py's floor uses).
  5. FREEZE. Whichever combo wins on TRAIN runs, completely unmodified, on
     TEST. TEST is never inspected before that freeze - enforced
     structurally by this script's own control flow, same as every other
     walk-forward here.

Same infra as scripts/walk_forward_regime_timed.py: on-disk bar cache
(reuses the SAME /tmp cache file - if you've run any other sweep/
walk-forward this session, this one hits a warm cache), resumable
checkpoint, per-ticker progress printing.

Usage:
    python3 scripts/walk_forward_core_engine.py
    python3 scripts/walk_forward_core_engine.py --split-date 2024-01-01
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
from sweep_regime_timed_configs import DEFAULT_TICKERS  # noqa: E402 - SAME 44-ticker universe, no drift

CACHE = "/tmp/bt_sweep_bars_cache.pkl"  # same cache every sweep/walk-forward script this session uses

RISK_LEVELS = ["CONSERVATIVE", "MODERATE", "AGGRESSIVE", "TURBO"]
R_MULTIPLES = [2.0, 3.0, 4.0]
COMBOS = [(rl, rm) for rl in RISK_LEVELS for rm in R_MULTIPLES]


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


def _cfg_for_combo(base_cfg: dict, risk_level: str, r_multiple: float) -> dict:
    """base_cfg's real cfg['risk'] dict (all four tiers, straight from
    config.yaml) is left completely untouched - only WHICH tier
    run_replay() reads (cfg['risk_level']) and the take-profit R multiple
    are varied. No entry_filter key is ever set here - see module
    docstring."""
    cfg = dict(base_cfg)
    cfg["risk_level"] = risk_level
    # backtest_engine.run_replay reads cfg["sell_rules"]["take_profit"]["r_multiple"]
    # directly (a flat key), NOT the nested rules.take_profit config.yaml
    # itself uses for live sell-rule evaluation - see regime_timed_backtest.
    # py's identical read pattern, mirrored here so this script's override
    # actually reaches the same code path every other walk-forward's does.
    cfg["sell_rules"] = {"take_profit": {"r_multiple": r_multiple}}
    # cfg["backtest"] is NOT touched here - main() already built it as
    # exactly {"execution_cost_bps": ...} with no entry_filter key, and that
    # dict is what base_cfg carries into every combo unchanged. See module
    # docstring: absence of entry_filter is the entire point of this script.
    return cfg


def _run_one(be, tickers, start, end, cfg, cfg_t0, name):
    def _ticker_progress(i, total, ticker, _cfg_t0=cfg_t0):
        print(f"    [replay {i}/{total}] {ticker} ({time.time() - _cfg_t0:.0f}s elapsed)", flush=True)

    return be.run_replay(tickers, start, end, cfg, max_hold_days=20, progress_cb=_ticker_progress)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    ap.add_argument("--months-total", type=int, default=60)
    ap.add_argument("--split-date", default="2024-01-01")
    ap.add_argument("--execution-cost-bps", type=float, default=25.0)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--min-train-trades", type=int, default=30)
    ap.add_argument("--out", default="walk_forward_core_engine_checkpoint.json")
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
    from config_loader import load_config_dict
    raw_cfg = dict(load_config_dict())
    raw_cfg["backtest"] = {"execution_cost_bps": args.execution_cost_bps}  # cost model ON, no entry_filter key

    print(f"BLIND WALK-FORWARD, CORE UNFILTERED ENGINE. TRAIN={train_start}..{train_end}  "
          f"TEST={test_start}..{test_end}")
    print("No entry_filter override at any point in this script - this is run_replay() exactly as "
          "paper trading runs it, just swept across risk_level x r_multiple.")
    print(f"Selection metric (fixed BEFORE any results are seen): TRAIN-period CAGR, "
          f"capital-constrained at ${args.starting_equity:,.0f}, {args.risk_pct}% risk/trade, "
          f"min {args.min_train_trades} trades to be eligible.\n")

    sweep_t0 = time.time()
    for idx, (risk_level, r_multiple) in enumerate(COMBOS, start=1):
        name = f"risk_level={risk_level} r_multiple={r_multiple}"
        if name in data["train"]:
            print(f"[TRAIN {idx}/{len(COMBOS)}] {name} ... already in checkpoint, skipping", flush=True)
            continue
        cfg_t0 = time.time()
        print(f"[TRAIN {idx}/{len(COMBOS)}] {name} ...", flush=True)
        cfg = _cfg_for_combo(raw_cfg, risk_level, r_multiple)
        res = _run_one(be, args.tickers, train_start, train_end, cfg, cfg_t0, name)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades ({elapsed:.1f}s)", flush=True)
            data["train"][name] = {"risk_level": risk_level, "r_multiple": r_multiple, "no_trades": True}
            _save_checkpoint(args.out, data)
            continue
        eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity)
        data["train"][name] = {
            "risk_level": risk_level, "r_multiple": r_multiple,
            "n_trades": s["n_trades"], "pf": s.get("profit_factor"), "expectancy_r": s.get("expectancy_r"),
            "cagr_pct": eq.get("cagr_pct"), "max_dd_pct": eq.get("max_drawdown_pct"), "sharpe": eq.get("sharpe"),
        }
        _save_checkpoint(args.out, data)
        print(f"    -> {s['n_trades']} trades, PF {s.get('profit_factor')}, CAGR {eq.get('cagr_pct')}% "
              f"({elapsed:.1f}s, {time.time() - sweep_t0:.1f}s total so far)", flush=True)

    print(f"\nTRAIN phase finished in {time.time() - sweep_t0:.1f}s.\n")

    eligible = [v for v in data["train"].values()
                if not v.get("no_trades") and v.get("cagr_pct") is not None
                and v.get("n_trades", 0) >= args.min_train_trades]
    print(f"{'Config':<38} {'N':>5} {'PF':>6} {'ExpR':>7} {'CAGR':>7} {'MaxDD':>7} {'Sharpe':>7}")
    for v in sorted(data["train"].values(), key=lambda v: (v.get("cagr_pct") is None, -(v.get("cagr_pct") or -999))):
        name = f"risk_level={v['risk_level']} r_multiple={v['r_multiple']}"
        if v.get("no_trades"):
            print(f"{name:<38} NO TRADES")
            continue
        flag = "" if v.get("n_trades", 0) >= args.min_train_trades else "  [INELIGIBLE - below min trade floor]"
        print(f"{name:<38} {v['n_trades']:>5} {v['pf']:>6} {v['expectancy_r']:>7} "
              f"{v['cagr_pct']:>6}% {v['max_dd_pct']:>6}% {v['sharpe']:>7}{flag}")

    if not eligible:
        print("\nNo TRAIN config cleared the minimum trade floor - cannot select a winner. Stopping.")
        return 1

    winner = max(eligible, key=lambda v: v["cagr_pct"])
    winner_name = f"risk_level={winner['risk_level']} r_multiple={winner['r_multiple']}"
    print(f"\nBLIND SELECTION (TRAIN-only CAGR, {args.min_train_trades}+ trades): {winner_name} "
          f"(TRAIN CAGR {winner['cagr_pct']}%, PF {winner['pf']})")
    if winner["risk_level"] == "TURBO":
        print("This MATCHES config.yaml's live risk_level (TURBO) - a point in favor of the currently "
              "deployed setting, not just a backtest artifact of trying other tiers.")
    else:
        print(f"This does NOT match config.yaml's live risk_level (TURBO) - direct evidence the "
              f"currently deployed tier is not what a blind selection on this window/universe would "
              f"have picked.")
    data["winner"] = {"risk_level": winner["risk_level"], "r_multiple": winner["r_multiple"]}
    _save_checkpoint(args.out, data)

    if data.get("test") is not None:
        print(f"\n[TEST] {winner_name} ... already in checkpoint, skipping")
    else:
        print(f"\n[TEST] {winner_name} on {test_start}..{test_end} (never seen during selection) ...",
              flush=True)
        cfg_t0 = time.time()
        cfg = _cfg_for_combo(raw_cfg, winner["risk_level"], winner["r_multiple"])
        res = _run_one(be, args.tickers, test_start, test_end, cfg, cfg_t0, winner_name)
        s = res["summary"]
        elapsed = time.time() - cfg_t0
        if not s.get("n_trades"):
            print(f"    -> no trades on TEST ({elapsed:.1f}s)")
            data["test"] = {"no_trades": True}
        else:
            eq = ec.simulate(res["trades"], args.risk_pct, starting_equity=args.starting_equity)
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
