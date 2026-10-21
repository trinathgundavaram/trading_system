#!/usr/bin/env python3
"""Sector-momentum sleeve - daily PAPER cycle. Gated by config.yaml's
sector_momentum_sleeve.paper_enabled (default false). Rebalances on the last
trading day of each month (and once at first run to establish positions);
every other day it only marks to market. Simulated fills only.

    python3 scripts/sector_momentum_paper_cycle.py [--dry-run] [--force-rebalance]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config_dict
from engine import sector_momentum_paper as smp
from mcp_clients.market_data import MarketDataRouter
from rules import sector_momentum as rule
from storage.database import Database


def fetch_closes(universe) -> pd.DataFrame:
    router = MarketDataRouter()
    cols = {}
    for t in universe:
        res = router.get_daily_bars(t)
        if not res:
            raise RuntimeError(f"no daily bars for {t} - check provider keys; not trading on partial data")
        bars, _ = res
        df = pd.DataFrame(bars)
        cols[t] = df.set_index("date")["close"].astype(float)
    return pd.DataFrame(cols).sort_index().ffill().dropna()


def _spy_close() -> float:
    bars, _ = MarketDataRouter().get_daily_bars("SPY")
    return float(pd.DataFrame(bars).sort_values("date")["close"].iloc[-1])


def is_month_end(closes: pd.DataFrame) -> bool:
    """True if the latest bar is the final trading day of its month (the next
    business day falls in a different month)."""
    last = pd.Timestamp(closes.index[-1])
    return (last + pd.offsets.BDay(1)).month != last.month


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-rebalance", action="store_true")
    a = ap.parse_args()
    cfg = load_config_dict()
    sc = cfg.get("sector_momentum_sleeve", {})
    if not sc.get("paper_enabled", False):
        print("sector_momentum_sleeve.paper_enabled is false in config.yaml - not running.")
        return
    lev = float(sc.get("leverage", 1.0))
    blend = sc.get("strategy", "sector") == "blend"
    closes = fetch_closes(rule.UNIVERSE_BLEND if blend else rule.UNIVERSE)
    band = float(sc.get("band_pct", 0.0)) / 100.0
    db = Database()
    prev = db.get_sm_state("risk_on")
    prev_on = None if prev is None else prev == "1"
    new_on = rule.market_state(closes, prev_on, band) if blend else None   # persisted only on a rebalance
    targets = (rule.target_weights_blend(closes, leverage=lev, risk_on=new_on) if blend
               else rule.target_weights(closes, n=int(sc.get("top_n", 3)), leverage=lev))
    prices = {t: float(closes[t].iloc[-1]) for t in closes}
    date = str(closes.index[-1])[:10]
    print(f"[{date}] target weights: " + ", ".join(f"{t} {w:.0%}" for t, w in targets.items()))
    if a.dry_run:
        print("(--dry-run: no trades)")
        return
    smp.ensure_seeded(db, cfg)
    if a.force_rebalance or is_month_end(closes) or not db.get_open_sm_positions():
        if new_on is not None:
            db.set_sm_state("risk_on", "1" if new_on else "0")
        for t in smp.rebalance(db, cfg, targets, prices, date=date, cost_bps=float(sc.get("cost_bps", 5.0)),
                             max_gross=max(lev, 1.0)):
            print(f"  {t['side'].upper()} {t['shares']:.4f} {t['ticker']} @ {t['price']:.2f}")
    else:
        print("not a rebalance day - mark to market only")
    if date not in {r["as_of_date"] for r in db.get_sm_equity_log()}:     # once per trading day
        smp.accrue_financing(db, cfg)
    s = smp.snapshot(db, prices)
    db.log_sm_equity(date, s["total_value"], prices["SPY"] if "SPY" in prices else _spy_close())
    if is_month_end(closes):                          # refresh the monthly report on month-end
        from scripts.sleeve_vs_spy_report import render, summarize
        rows = db.get_sm_equity_log()
        if len(rows) >= 2:
            out = Path(__file__).resolve().parent.parent / "output" / "sleeve_vs_spy.md"
            out.write_text(render(summarize(rows)) + "\n")
            print(f"report written: {out}")
    print(f"Sleeve: value ${s['total_value']:.2f} ({s['total_return_pct']}%) positions={s['positions']}")


if __name__ == "__main__":
    sys.exit(main())
