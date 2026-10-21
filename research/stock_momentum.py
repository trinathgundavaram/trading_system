#!/usr/bin/env python3
"""Stock momentum on point-in-time S&P 500 membership.

Rule (fixed before looking at results): at each month-end, among stocks that were
index members THEN and have 12 months of price history, rank by the average of
3/6/12-month returns, hold the top N equal-weight until next month-end. Optional
SPY 200-day rule with 2% band (to T-bills = 0% here, a slight understatement) and
leverage (financing 4% on the borrowed part). Costs: bps per unit one-way turnover.

Reported: results by split, coverage of the price data (the survivorship caveat),
a random-pick placebo with the same universe and rule, sensitivity to N, rule,
leverage and cost.
    python3 -m research.stock_momentum
"""
import warnings

import numpy as np
import pandas as pd

from research import stock_data as sd

warnings.filterwarnings("ignore")
SPLIT = pd.Timestamp("2016-01-01")


def monthly_panel(px: pd.DataFrame):
    me = px.groupby(px.index.to_period("M")).tail(1).index
    return me


def spy_series():
    from research.data import load_prices
    return load_prices(["SPY"], start="2005-06-01")["SPY"]


def run(px, m, spy, n=40, rule=True, band=0.02, lev=1.0, cost_bps=5.0, rng=None, fin_bps=400.0):
    me = monthly_panel(px)
    pxf = px.ffill(limit=5)
    P = pxf.reindex(me)
    sma = spy.rolling(200).mean().reindex(me)
    spy_me = spy.reindex(me)
    on, prev_w, rets, dates = True, pd.Series(dtype=float), [], []
    for i in range(253 // 21 + 12, len(me) - 1):
        t, t1 = me[i], me[i + 1]
        if pd.isna(sma.iloc[i]):
            continue
        if rule:
            hi, lo = sma.iloc[i] * (1 + band), sma.iloc[i] * (1 - band)
            on = True if spy_me.iloc[i] > hi else False if spy_me.iloc[i] < lo else on
        w = pd.Series(dtype=float)
        if on:
            mem = sd.members_on(m, t)
            cols = [c for c in P.columns if c in mem]
            ix = {k: P.index.get_loc(me[i - j]) for k, j in (("m3", 3), ("m6", 6), ("m12", 12))}
            cur = P.iloc[i][cols]
            r = pd.concat([cur / P.iloc[ix[k]][cols] - 1 for k in ("m3", "m6", "m12")], axis=1).mean(axis=1, skipna=False).dropna()
            if len(r) >= n:
                top = r.sample(n, random_state=int(rng.integers(1 << 30))) if rng is not None else r.nlargest(n)
                w = pd.Series(lev / n, index=top.index)
        nxt = P.iloc[i + 1].reindex(w.index) / P.iloc[i].reindex(w.index) - 1
        gross = float((w * nxt.fillna(0)).sum())
        allw = prev_w.reindex(w.index.union(prev_w.index)).fillna(0)
        turn = float((w.reindex(allw.index).fillna(0) - allw).abs().sum())
        borrow = max(w.sum() - 1, 0)
        rets.append(gross - turn * cost_bps / 1e4 - borrow * fin_bps / 1e4 / 12)
        dates.append(t1); prev_w = w
    return pd.Series(rets, index=dates)


def stats(s: pd.Series):
    if len(s) < 6:
        return dict(cagr=np.nan, dd=np.nan, sharpe=np.nan)
    eq = (1 + s).cumprod()
    yrs = len(s) / 12
    return dict(cagr=eq.iloc[-1] ** (1 / yrs) - 1, dd=float((eq / eq.cummax() - 1).min()),
                sharpe=s.mean() * 12 / (s.std() * np.sqrt(12)) if s.std() else 0.0)


def line(name, s, spy_m):
    f, te, tr = stats(s), stats(s[SPLIT:]), stats(s[:SPLIT - pd.Timedelta(days=1)])
    return (f"{name:34}{tr['cagr']:>7.1%}{te['cagr']:>8.1%}{te['dd']:>8.1%}{f['cagr']:>8.1%}{f['dd']:>8.1%}{f['sharpe']:>6.2f}")


def main():
    m = sd.load_membership()
    px = sd.load_prices()
    spy = spy_series()
    me = monthly_panel(px)
    cov = sd.coverage(m, px, me[::12])
    print(f"prices: {px.shape[1]} tickers with data of {m.ticker.nunique()} ever in the index")
    print("coverage of true index members that have prices (every 12th month-end):")
    print("  " + "  ".join(f"{d.year}:{v:.0%}" for d, v in cov.items()))
    spy_m = spy.reindex(me).pct_change().dropna()
    base = run(px, m, spy)
    print(f"\n{'strategy':34}{'TRAIN':>7}{'TEST':>8}{'TESTdd':>8}{'FULL':>8}{'FULLdd':>8}{'Shp':>6}")
    print(line("SPY buy&hold (month-end)", spy_m[base.index[0]:], spy_m))
    print(line("MAIN: top40, SPY rule, 1.0x", base, spy_m))
    for name, kw in [("top20", dict(n=20)), ("top80", dict(n=80)), ("no SPY rule", dict(rule=False)),
                     ("1.25x", dict(lev=1.25)), ("cost 20bps", dict(cost_bps=20.0)), ("cost 50bps", dict(cost_bps=50.0))]:
        print(line(f"  variant: {name}", run(px, m, spy, **kw), spy_m))
    rng = np.random.default_rng(0)
    pl = np.array([stats(run(px, m, spy, rng=rng))["cagr"] for _ in range(60)])
    real = stats(base)["cagr"]
    print(f"\nPLACEBO (random 40 from the same eligible stocks, same rule), 60 draws: median {np.median(pl):.1%}, "
          f"5-95% [{np.percentile(pl, 5):.1%}, {np.percentile(pl, 95):.1%}]; real {real:.1%} beats {np.mean(pl < real):.0%} of draws")
    print("\nsub-periods (CAGR): ", end="")
    for a, b in (("2008", "2011"), ("2012", "2015"), ("2016", "2019"), ("2020", "2023"), ("2024", "2026")):
        s, bm = base[a:b], spy_m[a:b]
        print(f"{a}-{b}: {stats(s)['cagr']:.1%} vs SPY {stats(bm)['cagr']:.1%} |", end=" ")
    print()


if __name__ == "__main__":
    main()
