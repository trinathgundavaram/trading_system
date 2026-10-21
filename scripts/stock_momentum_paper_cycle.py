#!/usr/bin/env python3
"""Stock-momentum sleeve - daily PAPER cycle (separate ledger `stk_*`). Gated by
config.yaml's stock_momentum_sleeve.paper_enabled. Rebalances on the last
trading day of each month (and once at first run); every other day it only marks
to market the names it holds. Simulated fills only - no broker.

Data: one batched yfinance download. Month-end pulls the whole S&P 500 universe
(~500 tickers, ~1-2 min); other days pull only the held names + SPY + SHY.

    python3 scripts/stock_momentum_paper_cycle.py [--dry-run] [--force-rebalance]
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config_dict
from engine import sector_momentum_paper as paper
from rules import stock_momentum as rule
from scripts.sector_momentum_paper_cycle import is_month_end
from storage.database import Database
from storage.sleeve_ledger import SleeveLedger

SECTION = "stock_momentum_sleeve"


def fetch_closes(tickers: list, period: str = "400d") -> pd.DataFrame:
    """Batched Yahoo download. Yahoo spells share classes with a dash (BRK-B) where the
    index list uses a dot (BRK.B); translate out and back so those names are not dropped."""
    import yfinance as yf
    to_yahoo = {t: t.replace(".", "-") for t in tickers}
    back = {v: k for k, v in to_yahoo.items()}
    ylist, frames = list(to_yahoo.values()), []
    for i in range(0, len(ylist), 100):
        df = yf.download(ylist[i:i + 100], period=period, auto_adjust=True, progress=False, timeout=60, threads=True)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame(ylist[i])
        frames.append(df)
    out = pd.concat(frames, axis=1).sort_index().ffill(limit=5).rename(columns=back)
    return out.loc[:, ~out.columns.duplicated()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-rebalance", action="store_true")
    a = ap.parse_args()
    cfg = load_config_dict()
    sc = cfg.get(SECTION, {})
    if not sc.get("paper_enabled", False):
        print(f"{SECTION}.paper_enabled is false in config.yaml - not running.")
        return
    led = SleeveLedger(Database(), "stk")
    paper.ensure_seeded(led, cfg, SECTION)
    held = [p["ticker"] for p in led.get_open_sm_positions()]
    n, lev = int(sc.get("top_n", 40)), float(sc.get("leverage", 1.0))
    band = float(sc.get("band_pct", 0.0)) / 100.0
    first_run = not held
    month_end = False
    # Cheap probe first: is today the month-end? (SPY alone decides the calendar.)
    probe = fetch_closes(["SPY"], "400d")
    month_end = is_month_end(probe)
    rebalance = a.force_rebalance or month_end or first_run
    members = rule.current_members(Path(sc.get("universe_file", rule.DEFAULT_UNIVERSE_FILE)))
    tickers = sorted(set(members) | {"SPY", rule.CASH}) if rebalance else sorted(set(held) | {"SPY", rule.CASH})
    closes = fetch_closes(tickers)
    date = str(closes.index[-1])[:10]
    spy = closes["SPY"].dropna()
    prev = led.get_sm_state("risk_on")
    risk_on = rule.market_state(closes[["SPY"]].dropna(), None if prev is None else prev == "1", band)
    prices = {t: float(closes[t].dropna().iloc[-1]) for t in closes.columns if closes[t].notna().any()}
    stale = json.loads(led.get_sm_state("last_prices", "{}") or "{}")
    for t in held:                                   # a name that vanished from the feed keeps its last known price
        prices.setdefault(t, stale.get(t) or [p["entry_price"] for p in led.get_open_sm_positions() if p["ticker"] == t][0])
    if rebalance:
        targets = rule.target_weights(closes, members, risk_on, n=n, leverage=lev)
        print(f"[{date}] {'risk-on' if risk_on else 'RISK-OFF (T-bills)'}: {len(targets)} targets"
              + ("" if not risk_on else f", top: {', '.join(list(targets)[:6])} ..."))
    else:
        targets = json.loads(led.get_sm_state("last_targets", "{}") or "{}").get("weights", {})
    if a.dry_run:
        print("(--dry-run: no trades)")
        return
    if rebalance:
        led.set_sm_state("risk_on", "1" if risk_on else "0")
        trades = paper.rebalance(led, cfg, targets, prices, date=date, cost_bps=float(sc.get("cost_bps", 5.0)),
                                 max_gross=max(lev, 1.0), section=SECTION,
                                 min_trade_frac=float(sc.get("min_trade_frac", 0.002)))
        print(f"  {sum(t['side'] == 'buy' for t in trades)} buys, {sum(t['side'] == 'sell' for t in trades)} sells")
        led.set_sm_state("last_targets", json.dumps({"as_of": date, "weights": targets}))
    else:
        print("not a rebalance day - mark to market only")
    sma = float(spy.iloc[-200:].mean())
    led.set_sm_state("market", json.dumps({"as_of": date, "spy": float(spy.iloc[-1]), "sma200": sma,
                                           "band_hi": sma * (1 + band), "band_lo": sma * (1 - band), "risk_on": bool(risk_on)}))
    led.set_sm_state("last_prices", json.dumps({**stale, **prices}))
    if date not in {r["as_of_date"] for r in led.get_sm_equity_log()}:
        paper.accrue_financing(led, cfg, SECTION)
    s = paper.snapshot(led, prices)
    led.log_sm_equity(date, s["total_value"], float(spy.iloc[-1]))
    print(f"Stock sleeve: value ${s['total_value']:.2f} ({s['total_return_pct']}%), {len(s['positions'])} positions")


if __name__ == "__main__":
    sys.exit(main())
