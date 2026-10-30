#!/usr/bin/env python3
"""Single-factor and two-factor tests on S&P 500 stocks (point-in-time membership; free prices cover only
~63% of historical members, so ABSOLUTE returns are optimistic - the comparison that matters is each
factor against the equal-weight universe of the same eligible stocks, which shares the same bias).

Factors from prices only (fundamental factors such as value/quality need point-in-time filings and are
not tested here): 12-1 momentum, 3/6/12m composite momentum, low 63-day volatility, 1-month reversal
(worst last-month return), 52-week-high proximity, and two combinations (average of percentile ranks).
Top N=40 equal-weight, month-end rebalance, 5 bps per unit turnover, no leverage, no SPY rule (that
overlay is tested separately). All variants listed here are reported.
    python3 -m research.stock_factors
"""
import warnings

import numpy as np
import pandas as pd

from research import stock_data as sd
from research.harness import SPLIT_DATE, metrics
from research.stock_pullback import member_mask

warnings.filterwarnings("ignore")
N, COST = 40, 5e-4
SPLIT = pd.Timestamp(SPLIT_DATE)


def scores(px: pd.DataFrame) -> dict:
    ret = px.pct_change()
    s = {
        "mom_12_1": px.shift(21) / px.shift(252) - 1,
        "mom_composite": (px / px.shift(63) + px / px.shift(126) + px / px.shift(252)) / 3 - 1,
        "low_vol_63d": -ret.rolling(63).std(),
        "reversal_1m": -(px / px.shift(21) - 1),
        "near_52w_high": px / px.rolling(252).max(),
    }
    return s


def ranked(score: pd.DataFrame, mem: pd.DataFrame) -> pd.DataFrame:
    return score.where(mem).rank(axis=1, pct=True)


def simulate(px, mem, rank, me, n=N):
    P = px.reindex(me)
    prev, out, dates, eq_all = pd.Series(dtype=float), [], [], []
    for i in range(13, len(me) - 1):
        t, t1 = me[i], me[i + 1]
        r = rank.loc[t].dropna()
        elig = mem.loc[t][mem.loc[t]].index.intersection(P.columns)
        nxt = (P.iloc[i + 1] / P.iloc[i] - 1)
        if len(r) < n:
            continue
        top = r.nlargest(n).index
        w = pd.Series(1.0 / n, index=top)
        turn = w.sub(prev, fill_value=0).abs().sum()
        out.append(float(w @ nxt.reindex(top).fillna(0)) - turn * COST)
        eq_all.append(float(nxt.reindex(elig).dropna().mean()))        # equal-weight eligible universe
        dates.append(t1); prev = w
    return pd.Series(out, index=dates), pd.Series(eq_all, index=dates)


def stat(s):
    if len(s) < 6:
        return dict(cagr=np.nan, dd=np.nan, sharpe=np.nan)
    eq = (1 + s).cumprod(); yrs = len(s) / 12
    return dict(cagr=eq.iloc[-1] ** (1 / yrs) - 1, dd=float((eq / eq.cummax() - 1).min()),
                sharpe=s.mean() * 12 / (s.std() * np.sqrt(12)) if s.std() else 0.0)


def main():
    m = sd.load_membership()
    px = sd.load_prices().ffill(limit=3)
    px = px[px.index >= "2006-01-01"]
    me = px.groupby(px.index.to_period("M")).tail(1).index
    mem = member_mask(m, px)
    sc = scores(px)
    rk = {k: ranked(v, mem) for k, v in sc.items()}
    variants = dict(rk)
    variants["mom_12_1 + low_vol"] = ((rk["mom_12_1"] + rk["low_vol_63d"]) / 2)
    variants["mom_12_1 + near_52w_high"] = ((rk["mom_12_1"] + rk["near_52w_high"]) / 2)
    variants["mom_12_1 + low_vol + 52w_high"] = ((rk["mom_12_1"] + rk["low_vol_63d"] + rk["near_52w_high"]) / 3)
    from research.data import load_prices
    spy_m = load_prices(["SPY"], start="2005-06-01")["SPY"].reindex(me).pct_change()
    print(f"{px.shape[1]} tickers; N={N}; monthly; 5bps\n")
    print(f"{'factor':32}{'TRAIN':>7}{'TEST':>8}{'TESTdd':>8}{'FULL':>8}{'FULLdd':>8}{'Shp':>6}{'vs univ':>9}{'hit%':>6}")
    base = None
    for name, rank in variants.items():
        r, u = simulate(px, mem, rank, me)
        base = u
        f, te, tr = stat(r), stat(r[SPLIT_DATE:]), stat(r[:SPLIT - pd.Timedelta(days=1)])
        excess = (r - u).mean() * 12
        hit = ((r - u) > 0).mean() * 100
        print(f"{name:32}{tr['cagr']:>7.1%}{te['cagr']:>8.1%}{te['dd']:>8.1%}{f['cagr']:>8.1%}{f['dd']:>8.1%}{f['sharpe']:>6.2f}{excess:>+9.1%}{hit:>6.0f}")
    f, te, tr = stat(base), stat(base[SPLIT_DATE:]), stat(base[:SPLIT - pd.Timedelta(days=1)])
    print(f"{'EQUAL-WEIGHT eligible universe':32}{tr['cagr']:>7.1%}{te['cagr']:>8.1%}{te['dd']:>8.1%}{f['cagr']:>8.1%}{f['dd']:>8.1%}{f['sharpe']:>6.2f}")
    s = spy_m.reindex(base.index)
    f, te, tr = stat(s), stat(s[SPLIT_DATE:]), stat(s[:SPLIT - pd.Timedelta(days=1)])
    print(f"{'SPY buy&hold':32}{tr['cagr']:>7.1%}{te['cagr']:>8.1%}{te['dd']:>8.1%}{f['cagr']:>8.1%}{f['dd']:>8.1%}{f['sharpe']:>6.2f}")
    print("\n'vs univ' = annualised average monthly excess over the equal-weight eligible universe; hit% = months beating it.")


if __name__ == "__main__":
    main()
