#!/usr/bin/env python3
"""Robustness report for sector momentum. Nothing here picks a winner: it shows
how much the result moves when the (fixed) default is perturbed.
    python3 -m research.robust_sector
"""
import warnings

import numpy as np

from research.data import load_prices
from research.harness import Config, SPLIT_DATE, backtest, metrics
from research.strategies2 import SECTORS, UNIVERSE2

warnings.filterwarnings("ignore")


def sector_mom(n=3, months=12, skip=1, lev=1.0):
    def f(prices):
        a, b = 21 * skip, 21 * months
        mom = (prices[SECTORS].iloc[-1 - a] / prices[SECTORS].iloc[-1 - b] - 1).dropna().sort_values(ascending=False)
        out = {t: lev / n for t in mom.head(n).index if prices[t].iloc[-1] > prices[t].iloc[-200:].mean()}
        out["IEF"] = lev - sum(out.values())
        return out
    return f


def run(fn, prices, **kw):
    c = Config(max_leverage=2.0, dd_cap=1.0, **kw)
    r = backtest(fn, prices, c)[prices.index[c.warmup_days]:]
    return r, metrics(r), metrics(r[SPLIT_DATE:]), metrics(r[:SPLIT_DATE].iloc[:-1])


def main():
    P = load_prices(UNIVERSE2, start="2007-06-01")
    spy = P["SPY"].pct_change().fillna(0.0)
    print("A) n x lookback (1x, default skip 1m)   FULL cagr / maxdd / TEST cagr")
    for months in (6, 9, 12):
        row = []
        for n in (2, 3, 4, 5):
            _, f, t, _ = run(sector_mom(n, months), P)
            row.append(f"n={n}: {f['cagr']:5.1%}/{f['maxdd']:5.0%}/{t['cagr']:5.1%}")
        print(f"  {months:>2}m  " + "   ".join(row))
    print("\nB) leverage x brake (n=3, 12m)          FULL cagr / maxdd / sharpe | TEST cagr / maxdd")
    for lev in (1.0, 1.25, 1.5):
        for br in (None, (0.08, 0.20)):
            _, f, t, _ = run(sector_mom(lev=lev), P, brake_start=br[0] if br else None, brake_full=br[1] if br else 0.2)
            print(f"  {lev:.2f}x brake={str(br):12} {f['cagr']:5.1%} / {f['maxdd']:5.1%} / {f['sharpe']:.2f} | {t['cagr']:5.1%} / {t['maxdd']:5.1%}")
    print("\nC) trading cost (default)")
    for bps in (5, 20, 50):
        _, f, t, _ = run(sector_mom(), P, cost_bps=bps)
        print(f"  {bps:>2} bps  FULL {f['cagr']:5.1%}/{f['maxdd']:5.1%}  TEST {t['cagr']:5.1%}")
    print("\nD) sub-periods, default vs SPY (CAGR / maxdd)")
    r, *_ = run(sector_mom(), P)
    for a, b in (("2008-01", "2011-12"), ("2012-01", "2015-12"), ("2016-01", "2019-12"), ("2020-01", "2023-12"), ("2024-01", "2026-10")):
        m, s = metrics(r[a:b]), metrics(spy[a:b])
        print(f"  {a}..{b}  strat {m['cagr']:6.1%}/{m['maxdd']:6.1%}   SPY {s['cagr']:6.1%}/{s['maxdd']:6.1%}")


if __name__ == "__main__":
    main()
