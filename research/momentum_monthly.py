#!/usr/bin/env python3
"""Monthly multi-horizon momentum (3/6/12m composite, top-N, monthly rebalance,
optional vol-adjustment and cash rules) vs the short-term sleeves, through the
fixed harness. Variants are fixed in advance (7 trials, all reported).
    python3 -m research.momentum_monthly
"""
import warnings

import numpy as np
import pandas as pd

from research.data import load_prices
from research.harness import Config, SPLIT_DATE, backtest, metrics
from research.strategies import Rsi2Basket
from research.strategies2 import UNIVERSE2, sector_momentum

warnings.filterwarnings("ignore")
ASSETS = [t for t in UNIVERSE2 if t != "IEF"] + ["IEF"]      # 22 ETFs incl. bonds, sectors
CASH = "SHY"                                                   # T-bill-like proxy


def momentum(top_n=5, vol_adj=False, abs_filter=False, market_rule=False):
    def f(prices):
        px = prices[ASSETS]
        r = pd.concat([px.iloc[-1] / px.iloc[-1 - 21 * m] - 1 for m in (3, 6, 12)], axis=1).mean(axis=1)
        if vol_adj:
            r = r / px.pct_change().iloc[-63:].std()
        if market_rule:
            spy = prices["SPY"]
            if spy.iloc[-1] < spy.iloc[-200:].mean():
                return {CASH: 1.0}
        picks = r.dropna().sort_values(ascending=False).head(top_n)
        raw = (px.iloc[-1] / px.iloc[-1 - 21 * 6] - 1) if abs_filter else None
        w = {}
        for t in picks.index:
            if abs_filter and raw[t] <= 0:
                w[CASH] = w.get(CASH, 0) + 1 / top_n
            else:
                w[t] = w.get(t, 0) + 1 / top_n
        return w
    return f


VARIANTS = {
    "mom_top5": momentum(5),
    "mom_top3": momentum(3),
    "mom_top8": momentum(8),
    "mom_top5_volAdj": momentum(5, vol_adj=True),
    "mom_top5_absFilter": momentum(5, abs_filter=True),
    "mom_top5_mktRule": momentum(5, market_rule=True),
    "mom_top5_vol+abs+mkt": momentum(5, vol_adj=True, abs_filter=True, market_rule=True),
}


def turnover(fn, P):
    me = P.groupby(P.index.to_period("M")).tail(1).index
    prev, tot, n = pd.Series(dtype=float), 0.0, 0
    for d in me:
        i = P.index.get_loc(d)
        if i < 260:
            continue
        w = pd.Series(fn(P.iloc[: i + 1]), dtype=float)
        tot += w.sub(prev, fill_value=0).abs().sum() / 2; n += 1; prev = w
    return tot / n * 12            # one-way turnover per year


def line(name, r, spy, to=None):
    fu, te, tr = metrics(r), metrics(r[SPLIT_DATE:], spy), metrics(r[:SPLIT_DATE].iloc[:-1])
    d08, d22 = metrics(r["2008-01-01":"2009-03-31"]), metrics(r["2022-01-01":"2022-12-31"])
    return (f"{name:24}{tr['cagr']:>7.1%}{te['cagr']:>8.1%}{te['sharpe']:>6.2f}{te['maxdd']:>8.1%}"
            f"{(d08.get('maxdd') or 0):>8.1%}{d22['maxdd']:>8.1%}{fu['cagr']:>8.1%}{fu['maxdd']:>8.1%}"
            f"{(f'{to:>7.0%}' if to is not None else '      -')}")


def main():
    P = load_prices(UNIVERSE2 + [CASH], start="2007-06-01").ffill()
    spy = P["SPY"].pct_change().fillna(0.0)
    c = Config(max_leverage=1.0, dd_cap=1.0)
    print(f"data {P.index[0].date()}..{P.index[-1].date()}  5bps costs, monthly rebalance, long-only\n")
    print(f"{'strategy':24}{'TRAIN':>7}{'TEST':>8}{'TESTshp':>6}{'TESTdd':>8}{'2008dd':>8}{'2022dd':>8}{'FULL':>8}{'FULLdd':>8}{'turnov/yr':>9}")
    rs = {}
    for n, fn in VARIANTS.items():
        r = backtest(fn, P, c)[P.index[c.warmup_days]:]; rs[n] = r
        print(line(n, r, spy, turnover(fn, P)))
    print("-- comparison sleeves --")
    for n, fn in [("SPY buy&hold", lambda p: {"SPY": 1.0}), ("sector_momentum(3 of 9)", sector_momentum),
                  ("SHORT-TERM rsi2_basket", Rsi2Basket())]:
        r = backtest(fn, P, c)[P.index[c.warmup_days]:]
        print(line(n, r, spy))
    print("\ncost sensitivity, mom_top5 (FULL CAGR / maxdd):")
    for bps in (5, 20, 50, 100):
        r = backtest(VARIANTS["mom_top5"], P, Config(max_leverage=1.0, dd_cap=1.0, cost_bps=bps))[P.index[c.warmup_days]:]
        m = metrics(r); print(f"  {bps:>3} bps: {m['cagr']:.1%} / {m['maxdd']:.1%}")
    print("\nsub-periods, mom_top5 vs SPY (CAGR / maxdd):")
    for a, b in (("2008-01", "2011-12"), ("2012-01", "2015-12"), ("2016-01", "2019-12"), ("2020-01", "2023-12"), ("2024-01", "2026-10")):
        m, s = metrics(rs["mom_top5"][a:b]), metrics(spy[a:b])
        print(f"  {a}..{b}  {m['cagr']:6.1%}/{m['maxdd']:6.1%}   SPY {s['cagr']:6.1%}/{s['maxdd']:6.1%}")


if __name__ == "__main__":
    main()
