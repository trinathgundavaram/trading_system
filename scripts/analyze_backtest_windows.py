#!/usr/bin/env python3
"""Time-sliced (contiguous-window) validation of a Stage 1 backtest run
(2026-07-28, Trinath's directive after the standard-tier/exclude-BULL entry
filter run: "demand it survive time-sliced validation before you trust the
PF 1.42 headline").

WHAT THIS IS AND ISN'T. This is NOT a re-fit walk-forward optimizer -
engine/backtest_engine.py's entry_filter (min_tier/exclude_regimes) has no
fitted parameters to re-calibrate per window, so there is no train/test split
in the classic sense. What this DOES check, honestly: does a single FIXED
rule set (whatever produced the results.json you point this at) keep
performing across contiguous slices of time, or does one strong period carry
the whole aggregate while the rest is flat or negative? That is the specific
concern a filter's parameters being chosen by eyeballing full-period
aggregate stats raises (see the entry_filter's own config.yaml comment for
how min_tier=standard/exclude_regimes=[BULL] was picked) - a rule that only
works in hindsight over ONE period it was implicitly tuned to look good on is
a real overfitting risk even with just two parameters.

Usage:
    python3 scripts/analyze_backtest_windows.py output/backtest_results/<ts>/results.json
    python3 scripts/analyze_backtest_windows.py output/backtest_results/<ts>/results.json --window-months 3
    python3 scripts/analyze_backtest_windows.py output/backtest_results/<ts>/results.json --window-months 6 --compare output/backtest_results/<other_ts>/results.json
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


def _window_key(entry_date: str, window_months: int) -> str:
    """Buckets an ISO entry_date into a contiguous window label, e.g. 2024-Q1
    for window_months=3, or a custom range label for other sizes. Windows are
    anchored to calendar year/month boundaries (not to the run's own start
    date) so the same window definition is reusable across different runs/
    universes for a like-for-like comparison."""
    y, m = int(entry_date[:4]), int(entry_date[5:7])
    bucket = (m - 1) // window_months
    start_m = bucket * window_months + 1
    end_m = start_m + window_months - 1
    if window_months == 3:
        return f"{y}-Q{bucket + 1}"
    if window_months == 12:
        return str(y)
    if window_months == 6:
        return f"{y}-H{bucket + 1}"
    return f"{y}-{start_m:02d}..{min(end_m, 12):02d}"


def _stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": None, "pf": None, "avg_outcome_pct": None, "expectancy_r": None}
    outs = [t["outcome_pct"] for t in trades]
    wins = [o for o in outs if o > 0]
    losses = [o for o in outs if o <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    rs = [t["outcome_r"] for t in trades if t.get("outcome_r") is not None]
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "pf": round(gw / gl, 2) if gl else None,
        "avg_outcome_pct": round(sum(outs) / len(outs), 2),
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
    }


def windowed_stats(trades: list, window_months: int) -> dict:
    by_window = defaultdict(list)
    for t in trades:
        by_window[_window_key(t["entry_date"], window_months)].append(t)
    return {k: _stats(v) for k, v in sorted(by_window.items())}


def _verdict(windows: dict) -> str:
    """A blunt, non-statistical read - NOT a significance test. Flags the
    two failure modes that actually matter for "is one period carrying the
    whole result": any window with n>=10 trades that's net negative, and
    whether removing the single best window (by total outcome_pct) would
    flip the remaining aggregate negative."""
    populated = {k: v for k, v in windows.items() if v["n"] >= 10}
    if not populated:
        return "Too few trades in any single window (>=10) to say anything about stability - " \
               "widen --window-months or gather more history."
    negative = [k for k, v in populated.items() if v["pf"] is not None and v["pf"] < 1.0]
    if negative:
        return (f"{len(negative)} window(s) with >=10 trades were net negative ({', '.join(negative)}) - "
                "this rule set is not winning in every period, it is winning on balance.")
    return "Every window with >=10 trades was net positive (PF>=1.0) - encouraging, but this is a " \
           "small number of non-overlapping windows, not a significance test. Keep collecting more."


def _print_table(windows: dict):
    print(f"{'Window':<10} {'Trades':>7} {'WinRate':>8} {'PF':>6} {'AvgOut%':>8} {'ExpR':>7}")
    for k, v in windows.items():
        if v["n"] == 0:
            print(f"{k:<10} {0:>7}")
            continue
        print(f"{k:<10} {v['n']:>7} {v['win_rate']:>7}% {v['pf'] or '-':>6} "
              f"{v['avg_outcome_pct']:>7}% {v['expectancy_r'] if v['expectancy_r'] is not None else '-':>7}")


def sequential_split_stats(trades: list, split_date: str) -> dict:
    """A strict sequential (not shuffled/k-fold) split at split_date: 'ref'
    is everything strictly before it, 'held_out' is everything on/after it.
    HONEST FRAMING (2026-07-28, Trinath: "freeze all logic on one period and
    test on the next"): this is NOT a true walk-forward re-fit, because
    min_tier/exclude_regimes were originally picked by eyeballing the FULL
    period's aggregate stats, not decided blind on 'ref' alone - the
    selection itself already saw both sides of any split drawn after the
    fact. What this DOES honestly answer: given the fixed rule as it exists
    today, does its edge sit entirely in one side of the calendar or does it
    persist on both sides untouched? A rule whose entire edge lives in 'ref'
    and vanishes in 'held_out' is a real red flag even under this weaker
    test; a rule that holds on both sides is necessary but not sufficient
    evidence of a real edge (a genuine blind walk-forward - picking the rule
    using ONLY data before split_date, then never touching it - is the
    stronger test and requires a second, separate backtest config)."""
    ref = [t for t in trades if t["entry_date"] < split_date]
    held_out = [t for t in trades if t["entry_date"] >= split_date]
    return {"split_date": split_date, "ref": _stats(ref), "held_out": _stats(held_out)}


def _print_split(split: dict):
    r, h = split["ref"], split["held_out"]
    print(f"Sequential split at {split['split_date']} (strict - see sequential_split_stats docstring "
          f"for why this is not a full walk-forward re-fit):")
    print(f"  ref (before)      : n={r['n']:>4}  win_rate={r['win_rate']}%  PF={r['pf']}  "
          f"avg_outcome={r['avg_outcome_pct']}%  expectancy_r={r['expectancy_r']}")
    print(f"  held_out (after)  : n={h['n']:>4}  win_rate={h['win_rate']}%  PF={h['pf']}  "
          f"avg_outcome={h['avg_outcome_pct']}%  expectancy_r={h['expectancy_r']}")
    if r["pf"] is not None and h["pf"] is not None:
        if h["pf"] >= 1.0:
            print(f"  -> the edge persisted into the untouched later period (held_out PF {h['pf']} >= 1.0).")
        else:
            print(f"  -> the edge did NOT persist into the untouched later period "
                  f"(held_out PF {h['pf']} < 1.0) - treat the ref-period result with real suspicion.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results_json", help="Path to a backtest results.json (from run_backtest.py)")
    ap.add_argument("--window-months", type=int, default=3, choices=[3, 6, 12],
                    help="Contiguous window size: 3 (quarters, default), 6 (halves), or 12 (years)")
    ap.add_argument("--compare", default=None,
                    help="Optional second results.json to print side-by-side (e.g. filtered vs unfiltered)")
    ap.add_argument("--split-date", default=None,
                    help="Also print a strict sequential ref/held_out split at this ISO date "
                         "(e.g. 2025-01-01, to test whether the rule 'generalized into 2025 and held up "
                         "in 2026' the way it was eyeballed as doing in the quarterly table).")
    args = ap.parse_args()

    d = json.loads(Path(args.results_json).read_text())
    trades = d.get("trades", [])
    cfg = d.get("config", {})
    print(f"{args.results_json}")
    print(f"  entry_filter={cfg.get('entry_filter')}  execution_cost_bps={cfg.get('execution_cost_bps')}  "
          f"n_trades={len(trades)}")
    windows = windowed_stats(trades, args.window_months)
    _print_table(windows)
    print()
    print(_verdict(windows))
    if args.split_date:
        print()
        _print_split(sequential_split_stats(trades, args.split_date))

    if args.compare:
        print()
        print("=" * 70)
        d2 = json.loads(Path(args.compare).read_text())
        trades2 = d2.get("trades", [])
        cfg2 = d2.get("config", {})
        print(f"{args.compare}")
        print(f"  entry_filter={cfg2.get('entry_filter')}  execution_cost_bps={cfg2.get('execution_cost_bps')}  "
              f"n_trades={len(trades2)}")
        windows2 = windowed_stats(trades2, args.window_months)
        _print_table(windows2)
        print()
        print(_verdict(windows2))


if __name__ == "__main__":
    sys.exit(main())
