#!/usr/bin/env python3
"""Does the sector-momentum result depend on the particular tickers I chose?
Fast monthly simulator (month-end signal -> held to next month-end, 5bps on
turnover). Tests: (1) placebo - random top-3 picks with the same trend filter;
(2) the same rule on a completely different universe (country ETFs);
(3) the same rule on random 9-asset subsets of a wide ETF pool.
    python3 -m research.universe_check
"""
import warnings
import numpy as np
import pandas as pd
from research.data import load_prices
from research.strategies2 import SECTORS, UNIVERSE2

warnings.filterwarnings("ignore")
COUNTRY = ["EWJ", "EWG", "EWU", "EWC", "EWA", "EWZ", "EWY", "EWT", "EWH", "EWS", "EWW", "EWQ"]


def prep(P, cols):
    m = P.groupby(P.index.to_period("M")).tail(1).index
    pos = [P.index.get_loc(d) for d in m]
    px = P[cols].values
    sma = P[cols].rolling(200).mean().values
    return m, pos, px, sma


def sim(P, cols, defensive="IEF", n=3, rng=None, cost=5e-4):
    m, pos, px, sma = prep(P, cols + [defensive]) if defensive not in cols else prep(P, cols)
    k = len(cols)
    rets, prev, dates = [], np.zeros(k + 1), []
    for a, b in zip(pos[:-1], pos[1:]):
        if a < 253:
            continue
        mom = px[a - 21, :k] / px[a - 252, :k] - 1
        order = rng.permutation(k) if rng is not None else np.argsort(-mom)
        w = np.zeros(k + 1)
        for i in order[:n]:
            if px[a, i] > sma[a, i]:
                w[i] = 1 / n
        w[k] = 1 - w[:k].sum()
        r = px[b] / px[a] - 1
        rets.append(float(w @ r) - cost * np.abs(w - prev).sum()); prev = w; dates.append(m[pos.index(b)])
    s = pd.Series(rets, index=dates)
    yrs = len(s) / 12
    eq = (1 + s).cumprod()
    return eq.iloc[-1] ** (1 / yrs) - 1, (eq / eq.cummax() - 1).min(), s


def main():
    P = load_prices(UNIVERSE2 + COUNTRY, start="2007-06-01").dropna(subset=["SPY", "IEF"])
    c, d, s = sim(P, SECTORS)
    print(f"(0) real rule, sector ETFs (fast sim): CAGR {c:.1%}  maxDD(monthly) {d:.1%}")
    rng = np.random.default_rng(0)
    pl = np.array([sim(P, SECTORS, rng=rng)[0] for _ in range(500)])
    print(f"(1) PLACEBO random top-3 + same trend filter, 500 draws: median {np.median(pl):.1%}, "
          f"5-95% [{np.percentile(pl, 5):.1%}, {np.percentile(pl, 95):.1%}]; real beats {np.mean(pl < c):.0%} of draws")
    cc = [t for t in COUNTRY if P[t].notna().sum() > 2500]
    c2, d2, _ = sim(P.ffill(), cc)
    pl2 = np.array([sim(P.ffill(), cc, rng=rng)[0] for _ in range(300)])
    print(f"(2) DIFFERENT TICKERS: {len(cc)} country ETFs, same rule: CAGR {c2:.1%} maxDD {d2:.1%}; "
          f"placebo median {np.median(pl2):.1%}; real beats {np.mean(pl2 < c2):.0%}")
    pool = [t for t in UNIVERSE2 if t not in ("IEF",) and P[t].notna().sum() > 2500]
    out = []
    for _ in range(200):
        cols = list(rng.choice(pool, 9, replace=False))
        out.append(sim(P.ffill(), cols)[0])
    out = np.array(out)
    print(f"(3) RANDOM 9-asset universes from a {len(pool)}-ETF pool, 200 draws: median {np.median(out):.1%}, "
          f"5-95% [{np.percentile(out, 5):.1%}, {np.percentile(out, 95):.1%}]")
    spy = P["SPY"].resample("M").last().pct_change().dropna()
    spy = spy[s.index[0]:]
    print(f"    SPY same months: CAGR {(1 + spy).prod() ** (12 / len(spy)) - 1:.1%}")


if __name__ == "__main__":
    main()
