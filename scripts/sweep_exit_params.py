#!/usr/bin/env python3
"""Parameter sweep over the Stage 1 replay (2026-07-26).

Entries in engine/backtest_engine.py are PATH-DEPENDENT (`i = exit_idx + 1`),
so changing an exit parameter changes which later days are even evaluated.
That rules out re-scoring a saved trade list under new parameters - every
config must be a full re-run. This script does that, with one concession to
practicality: daily bars are fetched once and cached, so a sweep costs one
network round-trip instead of one per config.

The cache is the ONLY thing shared between configs. Scoring, vetoes,
thresholds and exits are all recomputed per run through the real production
code, exactly as run_backtest.py does it.

Usage:
    python3 scripts/sweep_exit_params.py --preset baseline
    python3 scripts/sweep_exit_params.py --stop-pct 5 --r-multiple 3
    python3 scripts/sweep_exit_params.py --trail-mult 1.0 --label tight-trail
"""
import argparse
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

CACHE = "/tmp/bt_bars_cache.pkl"


def _install_bar_cache():
    """Memoize fetch_daily_bars to disk so a sweep is not N x the downloads."""
    import engine.backtest_engine as be
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "rb") as f:
                cache = pickle.load(f)
        except Exception:
            cache = {}
    real = be.fetch_daily_bars

    def cached(ticker, start, end):
        key = (ticker, start, end)
        if key not in cache:
            cache[key] = real(ticker, start, end)
            with open(CACHE, "wb") as f:
                pickle.dump(cache, f)
        return cache[key].copy()

    be.fetch_daily_bars = cached
    return be


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+",
                   default=["BABA", "CLSK", "HOOD", "MARA", "WFC", "NU"])
    p.add_argument("--start", default="2023-07-27")
    p.add_argument("--end", default="2026-07-26")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="risk.<level>.stop_loss_swing_pct (the cap on initial risk)")
    p.add_argument("--r-multiple", type=float, default=None,
                   help="sell_rules.take_profit.r_multiple")
    p.add_argument("--trail-mult", type=float, default=None,
                   help="sets BOTH profit_protect_trail_atr_mult and trend_trail_atr_mult")
    p.add_argument("--breakeven-r", type=float, default=None)
    p.add_argument("--threshold", type=float, default=None,
                   help="risk.<level>.buy_score_threshold_pct")
    p.add_argument("--label", default=None)
    a = p.parse_args()

    be = _install_bar_cache()
    from config_loader import load_config_dict
    cfg = load_config_dict()

    lvl = cfg.get("risk_level", "TURBO")
    if a.stop_pct is not None:
        cfg["risk"][lvl]["stop_loss_swing_pct"] = a.stop_pct
    if a.threshold is not None:
        cfg["risk"][lvl]["buy_score_threshold_pct"] = a.threshold
    if a.r_multiple is not None:
        cfg.setdefault("sell_rules", {}).setdefault("take_profit", {})["r_multiple"] = a.r_multiple
    if a.trail_mult is not None:
        sm = cfg.setdefault("stop_machine", {}).setdefault("SWING", {})
        sm["profit_protect_trail_atr_mult"] = a.trail_mult
        sm["trend_trail_atr_mult"] = a.trail_mult
    if a.breakeven_r is not None:
        cfg.setdefault("stop_machine", {}).setdefault("SWING", {})["breakeven_r"] = a.breakeven_r

    res = be.run_replay(a.tickers, a.start, a.end, cfg, max_hold_days=20)
    s = res["summary"]
    label = a.label or (f"stop={cfg['risk'][lvl]['stop_loss_swing_pct']} "
                        f"r={cfg.get('sell_rules', {}).get('take_profit', {}).get('r_multiple', 3.0)} "
                        f"trail={cfg.get('stop_machine', {}).get('SWING', {}).get('trend_trail_atr_mult', 1.5)} "
                        f"thr={cfg['risk'][lvl].get('buy_score_threshold_pct')}")
    if not s.get("n_trades"):
        print(f"{label:58s} | NO TRADES")
        return 0
    print(f"{label:44s} | n={s['n_trades']:4d} win={s['win_rate']:5.1f}% "
          f"avg={s['avg_outcome_pct']:+5.2f}% PF={s['profit_factor']:5.2f} "
          f"| expR={s.get('expectancy_r')} PF_R={s.get('profit_factor_r')} "
          f"risk={s.get('median_risk_pct')}% | hold={s['avg_hold_days']:4.1f}d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
