#!/usr/bin/env python3
"""SPY buy-and-hold benchmark (2026-07-28) - the missing comparison from the
2026-07-28 backtest review. Every number produced by engine/backtest_engine.py
this far (profit factor, win rate, expectancy_r) measures trade QUALITY, not
whether the strategy actually beat just buying and holding the index over the
same window. This script answers that second, separate question.

Not wired into run_backtest.py's output on purpose - this is a point-in-time
comparison a human should read next to a specific backtest run's dates, not
something to auto-embed in every results.json.

Usage:
    python3 scripts/spy_benchmark.py --start 2023-07-29 --end 2026-07-28
    python3 scripts/spy_benchmark.py --results-json output/backtest_results/<ts>/results.json
"""
import argparse
import json
import sys
from pathlib import Path


def spy_return(start: str, end: str) -> dict:
    import yfinance as yf

    df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True, timeout=30)
    if df.empty:
        raise RuntimeError(f"No SPY data returned for {start}..{end}")
    first_close = float(df["Close"].iloc[0])
    last_close = float(df["Close"].iloc[-1])
    total_return_pct = (last_close / first_close - 1) * 100
    years = (df.index[-1] - df.index[0]).days / 365.25
    cagr_pct = ((1 + total_return_pct / 100) ** (1 / years) - 1) * 100 if years > 0 else None
    return {
        "start": start, "end": end, "years": round(years, 2),
        "first_close": round(first_close, 2), "last_close": round(last_close, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2) if cagr_pct is not None else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=None, help="Window start (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="Window end (YYYY-MM-DD)")
    ap.add_argument("--results-json", default=None,
                     help="Infer --start/--end from a backtest results.json's trade dates instead of "
                          "passing them explicitly.")
    args = ap.parse_args()

    start, end = args.start, args.end
    strategy_summary = None
    if args.results_json:
        d = json.loads(Path(args.results_json).read_text())
        trades = d.get("trades", [])
        if trades:
            dates = sorted(t["entry_date"] for t in trades)
            start = start or dates[0]
            end = end or dates[-1]
        strategy_summary = d.get("summary")

    if not start or not end:
        print("Need --start/--end, or --results-json with at least one trade to infer them.")
        return 1

    bench = spy_return(start, end)
    print(f"SPY buy-and-hold, {bench['start']} -> {bench['end']} ({bench['years']} years):")
    print(f"  {bench['first_close']} -> {bench['last_close']}  "
          f"total return {bench['total_return_pct']}%  CAGR {bench['cagr_pct']}%")

    if strategy_summary:
        print()
        print("Strategy summary from the same results.json (per-trade quality, NOT a comparable "
              "portfolio return - see this script's module docstring and the analysis doc's section 5 "
              "on why expectancy_r and CAGR answer different questions):")
        print(f"  n_trades={strategy_summary.get('n_trades')}  "
              f"win_rate={strategy_summary.get('win_rate')}%  "
              f"profit_factor={strategy_summary.get('profit_factor')}  "
              f"expectancy_r={strategy_summary.get('expectancy_r')}")
        print("  A real vs.-SPY comparison needs a compounded equity curve under a real position-sizing "
              "rule, which this backtest does not yet produce - see the analysis doc's recommendation #5.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
