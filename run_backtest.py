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
    p.add_argument("--execution-cost-bps", type=float, default=None,
                    help="Round-trip slippage+spread haircut in bps, split across entry/exit fills "
                         "(default: config.yaml's backtest.execution_cost_bps, itself 0/off unless set). "
                         "See engine/execution_costs.py. Pass e.g. 20 or 30 for a realistic-friction run.")
    p.add_argument("--min-entry-tier", choices=["weak", "standard", "strong"], default=None,
                    help="BACKTEST-ONLY research filter: reject threshold-clearing candidates scored below "
                         "this tier instead of taking the trade (default: config.yaml's "
                         "backtest.entry_filter.min_tier, itself off unless set). Does not touch live/paper "
                         "trading - see engine/backtest_engine.py's passes_entry_filter().")
    p.add_argument("--exclude-regimes", nargs="+", default=None,
                    help="BACKTEST-ONLY research filter: reject candidates scored during these regimes "
                         "(e.g. --exclude-regimes BULL, or BULL BEAR). Default: config.yaml's "
                         "backtest.entry_filter.exclude_regimes, itself empty unless set.")
    p.add_argument("--max-vix", type=float, default=None,
                    help="BACKTEST-ONLY research filter: reject candidates on a day where vix_close exceeds "
                         "this level, independent of dominant_regime (see docs/"
                         "beating_the_market_analysis_2026-07-28.md sec 2.4 - regime alone did not protect "
                         "against 2022's bear market). Default: config.yaml's backtest.entry_filter.max_vix, "
                         "itself off unless set. e.g. --max-vix 25.")
    p.add_argument("--max-realized-vol", type=float, default=None,
                    help="BACKTEST-ONLY research filter: reject candidates on a day where SPY's trailing "
                         "annualized realized volatility exceeds this level - computed DIRECTLY from price "
                         "data, independent of dominant_regime/VIX (see docs/"
                         "beating_the_market_analysis_2026-07-28.md sec 7 - exclude_regimes:[BEAR]+max_vix "
                         "still failed to catch 2022 because the regime classifier's fake F&G/AD-ratio "
                         "inputs biased it toward CHOPPY). Default: config.yaml's "
                         "backtest.entry_filter.max_realized_vol_pct, itself off unless set. e.g. 25.")
    p.add_argument("--min-spy-trend-pct", type=float, default=None,
                    help="BACKTEST-ONLY research filter: reject candidates when SPY is more than this %% "
                         "below its own 200-day SMA (0 = require SPY at/above its 200-SMA; negative allows "
                         "some cushion). A slow-moving, multi-month trend read, deliberately NOT another "
                         "same-day threshold - see docs/beating_the_market_analysis_2026-07-28.md sec 7d "
                         "(three same-day signals all failed to protect against 2022). Default: config.yaml's "
                         "backtest.entry_filter.min_spy_trend_pct, itself off unless set. e.g. 0 or -5.")
    p.add_argument("--stop-loss-swing-pct", type=float, default=None,
                    help="BACKTEST-ONLY override of risk.<risk_level>.stop_loss_swing_pct for this run only - "
                         "does NOT touch config.yaml, so live/paper trading's real stop width is unaffected. "
                         "For re-testing docs/backtest_eval_2026-07-26.md's finding that the current 8%% TURBO "
                         "cap was binding on higher-ATR names (proven then, never applied) - try 14 or 16.")
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
    if args.execution_cost_bps is not None:
        # CLI override for a quick sensitivity run without editing
        # config.yaml - copy the backtest sub-dict rather than mutating the
        # loader's cached dict in place (load_config_dict() may return a
        # shared object reused by other callers in-process).
        cfg_dict = dict(cfg_dict)
        cfg_dict["backtest"] = dict((cfg_dict.get("backtest") or {}),
                                     execution_cost_bps=args.execution_cost_bps)
    if (args.min_entry_tier is not None or args.exclude_regimes is not None
            or args.max_vix is not None or args.max_realized_vol is not None
            or args.min_spy_trend_pct is not None):
        cfg_dict = dict(cfg_dict)
        bt = dict(cfg_dict.get("backtest") or {})
        existing_ef = dict(bt.get("entry_filter") or {})
        if args.min_entry_tier is not None:
            existing_ef["min_tier"] = args.min_entry_tier
        if args.exclude_regimes is not None:
            existing_ef["exclude_regimes"] = args.exclude_regimes
        if args.max_vix is not None:
            existing_ef["max_vix"] = args.max_vix
        if args.max_realized_vol is not None:
            existing_ef["max_realized_vol_pct"] = args.max_realized_vol
        if args.min_spy_trend_pct is not None:
            existing_ef["min_spy_trend_pct"] = args.min_spy_trend_pct
        bt["entry_filter"] = existing_ef
        cfg_dict["backtest"] = bt
    if args.stop_loss_swing_pct is not None:
        # Overlay ONLY the active risk_level's stop_loss_swing_pct, in a
        # copy - config.yaml's own value (and every other risk level) is
        # untouched, and this cfg_dict only ever feeds this one backtest run.
        cfg_dict = dict(cfg_dict)
        risk_level = cfg_dict.get("risk_level", "MODERATE")
        risk_cfg = dict(cfg_dict.get("risk") or {})
        level_cfg = dict(risk_cfg.get(risk_level) or {})
        level_cfg["stop_loss_swing_pct"] = args.stop_loss_swing_pct
        risk_cfg[risk_level] = level_cfg
        cfg_dict["risk"] = risk_cfg

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

    from engine.execution_costs import get_round_trip_bps
    from engine.backtest_engine import entry_filter_from_cfg
    cost_bps = get_round_trip_bps(cfg_dict)
    ef = entry_filter_from_cfg(cfg_dict)
    print(f"Stage 1 historical replay: {len(tickers)} tickers, {start} .. {end}, "
          f"risk_level={cfg_dict.get('risk_level')}, mode=swing (see engine/backtest_engine.py docstring for scope)")
    print(f"Execution cost model: {f'{cost_bps} bps round-trip' if cost_bps else 'OFF (frictionless fills)'}")
    if (ef["min_tier"] or ef["exclude_regimes"] or ef.get("max_vix") or ef.get("max_realized_vol_pct")
            or ef.get("min_spy_trend_pct") is not None):
        print(f"Entry filter (backtest-only): min_tier={ef['min_tier'] or 'any'}, "
              f"exclude_regimes={sorted(ef['exclude_regimes']) or 'none'}, "
              f"max_vix={ef.get('max_vix') if ef.get('max_vix') is not None else 'none'}, "
              f"max_realized_vol_pct="
              f"{ef.get('max_realized_vol_pct') if ef.get('max_realized_vol_pct') is not None else 'none'}, "
              f"min_spy_trend_pct="
              f"{ef.get('min_spy_trend_pct') if ef.get('min_spy_trend_pct') is not None else 'none'}")
    else:
        print("Entry filter (backtest-only): OFF (all tiers/regimes/VIX/realized-vol/trend levels accepted)")
    risk_level = cfg_dict.get("risk_level", "MODERATE")
    effective_stop_pct = (cfg_dict.get("risk", {}).get(risk_level, {}) or {}).get("stop_loss_swing_pct")
    if args.stop_loss_swing_pct is not None:
        print(f"Stop-loss width (backtest-only override): {effective_stop_pct}% "
              f"(config.yaml's live default is unchanged)")
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
