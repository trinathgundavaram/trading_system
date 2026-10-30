#!/usr/bin/env python3
"""Pullback-in-an-uptrend on S&P 500 stocks (point-in-time membership; survivorship caveat as in
research/stock_momentum.py). Motivation: the swing engine's entries (top gainers / gaps / volume
surges) pop for a day and then fade over 3-10 days - the short-term-reversal effect - so the opposite
construction is tested: buy stocks in an intermediate uptrend that are TEMPORARILY oversold.

Rule (fixed before looking at results): a stock qualifies on day t if it is an index member, close >
SMA200, RSI(2) < 10 and (variant B) its 12-1 month momentum is in the top 30% of members. Buy at the
NEXT close, equal weight 1/K of equity, max K positions (lowest RSI2 first); exit at the next close
after RSI(2) > 70 or after 10 trading days. 10 bps per side. No leverage; idle cash earns 0.
    python3 -m research.stock_pullback
"""
import warnings

import numpy as np
import pandas as pd

from research import stock_data as sd
from research.harness import SPLIT_DATE, metrics

warnings.filterwarnings("ignore")


def rsi2(px: pd.DataFrame) -> pd.DataFrame:
    d = px.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = up.ewm(alpha=0.5, adjust=False).mean(), dn.ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, np.nan))


def member_mask(m: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    mask = pd.DataFrame(False, index=px.index, columns=px.columns)
    for t, g in m.groupby("ticker"):
        if t not in mask.columns:
            continue
        for _, r in g.iterrows():
            mask.loc[(mask.index >= r.start_date) & (mask.index <= r.end_date), t] = True
    return mask


def run(px, mem, rs, sma, mom_rank, K=20, mom_min=None, max_hold=10, cost=0.001, start="2007-01-01"):
    idx = px.index
    cash, pos, eq, dates = 1.0, {}, [], []
    cols = list(px.columns)
    pend_buy, pend_sell = [], []
    arr = px.values
    cidx = {c: i for i, c in enumerate(cols)}
    for i in range(1, len(idx) - 1):
        d = idx[i]
        # executions decided yesterday fill at today's close
        for t in pend_sell:
            if t in pos:
                shares, _ = pos.pop(t)
                px_t = arr[i, cidx[t]]
                if np.isfinite(px_t):
                    cash += shares * px_t * (1 - cost)
        total = cash + sum(sh * arr[i, cidx[t]] for t, (sh, _) in pos.items() if np.isfinite(arr[i, cidx[t]]))
        for t in pend_buy:
            if t in pos or len(pos) >= K or not np.isfinite(arr[i, cidx[t]]):
                continue
            alloc = min(total / K, cash)
            if alloc <= 0:
                continue
            pos[t] = (alloc * (1 - cost) / arr[i, cidx[t]], i)
            cash -= alloc
        pend_buy, pend_sell = [], []
        total = cash + sum(sh * arr[i, cidx[t]] for t, (sh, _) in pos.items() if np.isfinite(arr[i, cidx[t]]))
        if d >= pd.Timestamp(start):
            eq.append(total); dates.append(d)
        # today's signals -> fill tomorrow
        for t, (sh, i0) in pos.items():
            r = rs.iloc[i][t]
            if (np.isfinite(r) and r > 70) or i - i0 >= max_hold:
                pend_sell.append(t)
        free = K - (len(pos) - len(pend_sell))
        if free > 0:
            ok = mem.iloc[i] & (px.iloc[i] > sma.iloc[i]) & (rs.iloc[i] < 10)
            if mom_min is not None:
                ok &= mom_rank.iloc[i] >= mom_min
            cand = rs.iloc[i][ok].sort_values().index.tolist()
            pend_buy = [t for t in cand if t not in pos][:free]
    s = pd.Series(eq, index=dates)
    return s.pct_change().fillna(0.0)


def main():
    m = sd.load_membership()
    px = sd.load_prices().ffill(limit=3)
    px = px[px.index >= "2006-01-01"]
    from research.data import load_prices
    spy = load_prices(["SPY"], start="2005-06-01")["SPY"].pct_change().fillna(0)
    print(f"{px.shape[1]} tickers, {px.index[0].date()}..{px.index[-1].date()}; building signals ...", flush=True)
    mem = member_mask(m, px)
    rs, sma = rsi2(px), px.rolling(200).mean()
    mom = px.shift(21) / px.shift(252) - 1
    mom_rank = mom.where(mem).rank(axis=1, pct=True)
    res = {
        "A  pullback, no momentum filter, K=20": run(px, mem, rs, sma, mom_rank, K=20),
        "B  pullback + top-30% 12-1 momentum, K=20": run(px, mem, rs, sma, mom_rank, K=20, mom_min=0.7),
        "B' same, K=10": run(px, mem, rs, sma, mom_rank, K=10, mom_min=0.7),
    }
    print(f"\n{'variant':44}{'TRAIN':>8}{'TEST':>8}{'TESTdd':>8}{'FULL':>8}{'FULLdd':>8}{'Shp':>6}")
    for n, r in res.items():
        a, b, f = metrics(r[:SPLIT_DATE].iloc[:-1]), metrics(r[SPLIT_DATE:], spy), metrics(r)
        print(f"{n:44}{a['cagr']:>8.1%}{b['cagr']:>8.1%}{b['maxdd']:>8.1%}{f['cagr']:>8.1%}{f['maxdd']:>8.1%}{f['sharpe']:>6.2f}")
    r0 = res["A  pullback, no momentum filter, K=20"]
    s = spy[r0.index[0]:]
    a, b, f = metrics(s[:SPLIT_DATE].iloc[:-1]), metrics(s[SPLIT_DATE:]), metrics(s)
    print(f"{'SPY buy&hold':44}{a['cagr']:>8.1%}{b['cagr']:>8.1%}{b['maxdd']:>8.1%}{f['cagr']:>8.1%}{f['maxdd']:>8.1%}{f['sharpe']:>6.2f}")
    print("\nsub-periods CAGR (B vs SPY):", end=" ")
    rb = res["B  pullback + top-30% 12-1 momentum, K=20"]
    for a_, b_ in (("2008", "2011"), ("2012", "2015"), ("2016", "2019"), ("2020", "2023"), ("2024", "2026")):
        print(f"{a_}-{b_}: {metrics(rb[a_:b_])['cagr']:.1%} vs {metrics(spy[a_:b_])['cagr']:.1%} |", end=" ")
    print()


if __name__ == "__main__":
    main()
