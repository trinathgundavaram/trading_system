#!/usr/bin/env python3
"""Replay the sector-momentum paper sleeve over history using the PRODUCTION
code path (rules/sector_momentum.py + engine/sector_momentum_paper.py) on an
in-memory ledger - nothing touches the real database. Month-end rebalances,
daily marks, financing on borrowed cash, slippage; compared with SPY.

    python3 scripts/sector_momentum_replay.py                 # last 12 months
    python3 scripts/sector_momentum_replay.py --months 24
    python3 scripts/sector_momentum_replay.py --start 2022-01-03 --end 2022-12-30
    python3 scripts/sector_momentum_replay.py --rolling       # every 12m window since 2008 (research harness)

One replay is ONE sample of one market. --rolling shows how much 12-month
results vary across history, which is the honest way to read it.
"""
import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

from engine import sector_momentum_paper as smp
from rules import sector_momentum as rule


class MemLedger:
    """Implements exactly the db methods engine.sector_momentum_paper uses."""
    def __init__(self): self.acct = None; self.pos = []; self._id = 0
    def get_sm_account(self): return self.acct
    def init_sm_account(self, c): self.acct = {"starting_cash": c, "cash": c, "realized_pnl": 0.0}; return self.acct
    def adjust_sm_cash(self, d, realized_pnl_delta=0.0): self.acct["cash"] += d; self.acct["realized_pnl"] += realized_pnl_delta
    def get_open_sm_positions(self): return [dict(p) for p in self.pos if p["status"] == "open"]
    def open_sm_position(self, t, d, px, sh):
        self._id += 1; self.pos.append({"id": self._id, "ticker": t, "status": "open", "entry_price": px, "shares": sh}); return self._id
    def _get(self, i): return next(p for p in self.pos if p["id"] == i)
    def update_sm_position_shares(self, i, sh): self._get(i)["shares"] = sh
    def update_sm_position_entry_price(self, i, px): self._get(i)["entry_price"] = px
    def close_sm_position(self, i, d, px, pnl): self._get(i)["status"] = "closed"


def replay(closes: pd.DataFrame, start, end, cfg: dict):
    sc = cfg["sector_momentum_sleeve"]
    lev = float(sc.get("leverage", 1.0)); blend = sc.get("strategy", "sector") == "blend"
    db = MemLedger(); smp.ensure_seeded(db, cfg)
    days = [d for d in closes.index if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    month_end = {d for d in days if (d + pd.offsets.BDay(1)).month != d.month}
    curve, log = {}, []
    band, risk_on = float(sc.get("band_pct", 0.0)) / 100.0, None
    for k, d in enumerate(days):
        prices = {t: float(closes.loc[d, t]) for t in closes.columns}
        if k > 0:
            smp.accrue_financing(db, cfg)
        if k == 0 or d in month_end:
            hist = closes.loc[:d]
            if blend:
                risk_on = rule.market_state(hist, risk_on, band)
            tw = rule.target_weights_blend(hist, leverage=lev, risk_on=risk_on) if blend else rule.target_weights(hist, n=int(sc.get("top_n", 3)), leverage=lev)
            tr = smp.rebalance(db, cfg, tw, prices, date=str(d.date()), cost_bps=float(sc.get("cost_bps", 5.0)), max_gross=max(lev, 1.0))
            log.append((d.date(), "T-bills" if set(tw) == {"SHY"} else "risk-on", len(tr), round(sum(tw.values()), 2)))
        curve[d] = smp.equity(db, prices)
    return pd.Series(curve), log


def stats(s: pd.Series) -> dict:
    r = s.pct_change().dropna()
    yrs = len(r) / 252
    return {"total": s.iloc[-1] / s.iloc[0] - 1, "cagr": (s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1 if yrs > 0 else np.nan,
            "maxdd": float((s / s.cummax() - 1).min()), "vol": r.std() * np.sqrt(252),
            "sharpe": r.mean() * 252 / (r.std() * np.sqrt(252)) if r.std() else 0.0}


def rolling_report():
    from research.data import load_prices
    from research.harness import Config, backtest
    from research.leverage_study import blend2, levered, momentum, sector_mom, with_market_rule
    from research.strategies2 import UNIVERSE2
    P = load_prices(UNIVERSE2 + ["SHY"], start="2007-06-01").ffill()
    from config_loader import load_config_dict
    from research.target_return_study import buffered_rule
    sc = load_config_dict()["sector_momentum_sleeve"]
    lev, band = float(sc.get("leverage", 1.0)), float(sc.get("band_pct", 0.0)) / 100.0
    rule_fn = (lambda b: buffered_rule(b, band)) if band else with_market_rule
    fn = levered(blend2(rule_fn(sector_mom), rule_fn(momentum(8))), lev)
    print(f"rolling windows for blend @ {lev}x, band {band:.0%}")
    c = Config(max_leverage=3.0, dd_cap=1.0)
    r = backtest(fn, P, c)[P.index[c.warmup_days]:]
    spy = P["SPY"].pct_change().fillna(0.0)[r.index]
    eq, seq = (1 + r).cumprod(), (1 + spy).cumprod()
    w = 252
    ret, sret = eq / eq.shift(w) - 1, seq / seq.shift(w) - 1
    dd = eq.rolling(w).apply(lambda x: (x / np.maximum.accumulate(x)).min() - 1, raw=True)
    sdd = seq.rolling(w).apply(lambda x: (x / np.maximum.accumulate(x)).min() - 1, raw=True)
    ok = ret.dropna().index
    print(f"{len(ok)} overlapping 12-month windows, {ok[0].date()}..{ok[-1].date()} (configured strategy vs SPY)\n")
    q = lambda s, p: s.loc[ok].quantile(p)
    print(f"{'':22}{'strategy':>10}{'SPY':>9}")
    for lab, a, b in (("median 12m return", q(ret, .5), q(sret, .5)), ("worst 5% of windows", q(ret, .05), q(sret, .05)),
                      ("best 5% of windows", q(ret, .95), q(sret, .95)), ("worst window", ret.loc[ok].min(), sret.loc[ok].min()),
                      ("median worst drawdown", dd.loc[ok].median(), sdd.loc[ok].median()),
                      ("worst drawdown", dd.loc[ok].min(), sdd.loc[ok].min())):
        print(f"{lab:22}{a:>10.1%}{b:>9.1%}")
    print(f"\nstrategy beat SPY in {(ret.loc[ok] > sret.loc[ok]).mean():.0%} of windows; lost money in {(ret.loc[ok] < 0).mean():.0%} (SPY: {(sret.loc[ok] < 0).mean():.0%})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--rolling", action="store_true")
    a = ap.parse_args()
    if a.rolling:
        return rolling_report()
    from config_loader import load_config_dict
    from research.data import load_prices
    cfg = load_config_dict()
    sc = cfg["sector_momentum_sleeve"]
    uni = rule.UNIVERSE_BLEND if sc.get("strategy") == "blend" else rule.UNIVERSE
    closes = load_prices(sorted(set(uni) | {"SPY"}), start="2007-06-01", refresh=True).ffill().dropna()
    end = pd.Timestamp(a.end) if a.end else closes.index[-1]
    start = pd.Timestamp(a.start) if a.start else end - pd.DateOffset(months=a.months)
    s, log = replay(closes, start, end, cfg)
    spy = closes["SPY"].loc[s.index]
    m, ms = stats(s), stats(spy)
    print(f"Replay {s.index[0].date()} .. {s.index[-1].date()}  strategy={sc.get('strategy')} leverage={sc.get('leverage')}x  start ${s.iloc[0]:,.0f}\n")
    print("rebalances:", *[f"{d} {mode} (gross {g}, {n} trades)" for d, mode, n, g in log], sep="\n  ")
    print(f"\n{'':14}{'strategy':>10}{'SPY':>9}")
    for lab, k in (("total return", "total"), ("CAGR", "cagr"), ("max drawdown", "maxdd"), ("volatility", "vol")):
        print(f"{lab:14}{m[k]:>10.1%}{ms[k]:>9.1%}")
    print(f"{'Sharpe':14}{m['sharpe']:>10.2f}{ms['sharpe']:>9.2f}")
    mo = s.resample("ME").last().pct_change().dropna(); mso = spy.resample("ME").last().pct_change().dropna()
    print("\nmonth        strategy     SPY")
    for d in mo.index:
        print(f"{d:%Y-%m}   {mo[d]:>9.1%} {mso.get(d, float('nan')):>7.1%}")
    print("\nNote: this is one 12-month sample. Run with --rolling to see the spread across history.")


if __name__ == "__main__":
    main()
