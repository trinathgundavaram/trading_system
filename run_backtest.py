#!/usr/bin/env python3
"""CLI runner for engine/backtest_engine.py's Stage 1 historical replay -
the #1 priority both external-review rounds landed on (2026-07-23): "the
bottleneck isn't the model, it's the data-generation mechanism."

Runs the REAL production rules/hard_vetoes.py + rules/swing_buy_rules.py
against historical daily bars (yfinance), instead of live/paper trading one
signal every 15 minutes for months to accumulate enough closed trades for
the walk-forward/feature-importance/champion-challenger machinery that
already exists in learning/ and analytics/.

This is one of THREE callers of engine/backtest_engine.py's run_and_persist()
- the others are engine/backtest_loop.py (weekly automatic trigger from
scheduler.py) and server.py's POST /api/backtest/run (the Learning tab's
"Run Backtest Now" button). All three share the exact same run+persist code
path, so a manual CLI run, the weekly automatic run, and a UI-triggered run
are never subtly different implementations.

Default smoke-test scope (per Trinath's "small smoke test" choice,
2026-07-23): the 6-ticker manual watchlist (config.yaml) plus 7 liquid
mega-caps for a broader read, 12 months, SWING mode. Override via --tickers/
--start/--end/--max-hold-days for a bigger run once this looks sane.

Usage:
    python run_backtest.py
    python run_backtest.py --tickers AAPL MSFT NVDA --start 2023-01-01 --end 2024-01-01
    python run_backtest.py --months 6 --max-hold-days 15
    python run_backtest.py --no-db   # skip backtest_runs logging (file output only)

Output: JSON (full trade list + summary) and a short Markdown report, always
written to output/backtest_results/<timestamp>/ - PLUS a row in the
backtest_runs table (so the Learning tab's history/latest-run panels pick
this run up too) unless --no-db or the database is unreachable.
"""
import argparse
import sys
from datetime import date, timedelta

from config_loader import load_config_dict
from engine.backtest_engine import run_and_persist

DEFAULT_TICKERS = [
    # config.yaml's hand-curated watchlist
    "VRT", "ORCL", "MU", "FIX", "ASTS", "NFLX",
    # + liquid mega-caps for a broader Stage 1 read (real bar history back
    # decades, so warmup/data-availability is never the limiting factor)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS,
                    help=f"Tickers to replay (default: {len(DEFAULT_TICKERS)}-ticker smoke-test list). "
                         "Ignored if --ticker-source screener_discovered is given.")
    p.add_argument("--ticker-source", choices=["explicit", "screener_discovered"], default="explicit",
                    help="'explicit' (default) uses --tickers as given. 'screener_discovered' ignores "
                         "--tickers and instead pulls the live-discovered universe from the "
                         "screener_candidates table, ordered by discovery frequency (never by past "
                         "score - see engine/backtest_loop.py's resolve_backtest_tickers docstring for "
                         "why score-based selection would be look-ahead bias, not a real result).")
    p.add_argument("--months", type=int, default=12, help="Replay window length in months, ending today (default: 12)")
    p.add_argument("--start", default=None, help="Explicit start date YYYY-MM-DD (overrides --months)")
    p.add_argument("--end", default=None, help="Explicit end date YYYY-MM-DD (default: today)")
    p.add_argument("--warmup-days", type=int, default=260,
                    help="Trading days of history fetched before --start for SMA200/weekly-trend warmup (default: 260)")
    p.add_argument("--max-hold-days", type=int, default=20, help="Max simulated hold before a time-based exit (default: 20)")
    p.add_argument("--out-dir", default=None, help="Output directory (default: output/backtest_results/<timestamp>)")
    p.add_argument("--no-db", action="store_true", help="Skip backtest_runs DB logging - file output only")
    p.add_argument("--triggered-by", default="manual",
                   help="Recorded on the backtest_runs row - 'manual' (this CLI or the Learning tab button) "
                        "or 'weekly_auto' (scheduler.py's engine/backtest_loop.py trigger). Default: manual")
    args = p.parse_args()

    end = args.end or date.today().isoformat()
    start = args.start or (date.today() - timedelta(days=int(args.months * 30.44))).isoformat()

    # run_replay/rules.hard_vetoes/rules.swing_buy_rules all expect a plain
    # dict (.get() throughout) - config_loader.load_config_dict() is the
    # dict-shaped loader (load_config() returns a dot-access SimpleNamespace
    # instead, used by the legacy engine/executor.py path only).
    cfg_dict = load_config_dict()

    db = None
    if not args.no_db:
        try:
            from storage.database import Database
            db = Database()
        except Exception as e:
            print(f"Note: couldn't reach the database ({e}) - continuing with file output only.")

    tickers = args.tickers
    if args.ticker_source == "screener_discovered":
        # Resolve against a DB handle regardless of --no-db above (that flag
        # only skips backtest_runs LOGGING, not ticker resolution) - falls
        # back to args.tickers if this DB attempt or the query itself fails.
        try:
            from storage.database import Database
            from engine.backtest_loop import resolve_backtest_tickers
            tickers_db = db or Database()
            auto_cfg = dict(cfg_dict)
            auto_cfg["backtest"] = dict((auto_cfg.get("backtest") or {}),
                                         ticker_source="screener_discovered")
            tickers = resolve_backtest_tickers(auto_cfg, tickers_db) or args.tickers
        except Exception as e:
            print(f"Note: screener_discovered ticker resolution failed ({e}) - using --tickers instead.")

    print(f"Stage 1 historical replay: {len(tickers)} tickers, {start} .. {end}, "
          f"risk_level={cfg_dict.get('risk_level')}, mode=swing (see engine/backtest_engine.py docstring for scope)")
    print(f"Tickers: {', '.join(tickers)}")

    result = run_and_persist(tickers, start, end, cfg_dict, db=db, triggered_by=args.triggered_by,
                              out_root=args.out_dir, warmup_days=args.warmup_days,
                              max_hold_days=args.max_hold_days)

    s = result["summary"]
    print()
    print(f"Scored {result['n_scored']} candidate-days across {len(tickers)} tickers.")
    print(f"Vetoes: {result['veto_counts']}")
    if s.get("n_trades"):
        print(f"Trades: {s['n_trades']}  win_rate={s['win_rate']}%  avg_outcome={s['avg_outcome_pct']}%  "
              f"profit_factor={s.get('profit_factor')}  avg_hold={s['avg_hold_days']}d")
        print(f"Exit reasons: {s['exit_reason_counts']}")
    else:
        print("No trades fired in this window - see summary.md for what to check "
              "(threshold too high for this ticker/period, or too few candidate-days).")
    print()
    print(f"Full results: {result['output_dir']}/results.json")
    print(f"Report: {result['output_dir']}/summary.md")
    if result.get("run_id") is not None:
        print(f"Logged as backtest_runs id={result['run_id']} - visible on the Learning tab.")


if __name__ == "__main__":
    sys.exit(main())
