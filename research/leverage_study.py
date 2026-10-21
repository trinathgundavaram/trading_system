#!/usr/bin/env python3
"""How far can monthly momentum be levered? Fixed grid, all configs reported.
Bases (all rebalance monthly, long-only, go to T-bills when SPY < 200d SMA so
leverage is never held in a downtrend):
  A  sector momentum (top 3 of 9 SPDRs, 12-1m)
  B  broad 3/6/12m composite, top 8 of 22 ETFs
  C  50/50 blend of A and B
Leverage multiplies only the RISK positions; T-bills/bonds stay 1x. Borrowing
is charged `financing_bps` on gross exposure above 100%.
    python3 -m research.leverage_study
"""
import warnings

import numpy as np
import pandas as pd

from research.data import load_prices
from research.harness import Config, SPLIT_DATE, backtest, metrics
from research.momentum_monthly import CASH, momentum
from research.strategies2 import UNIVERSE2, SECTORS

warnings.filterwarnings("ignore")
DEFENSIVE = {"IEF", CASH}


def with_market_rule(base):
    def f(prices):
        spy = prices["SPY"]
        return {CASH: 1.0} if spy.iloc[-1] < spy.iloc[-200:].mean() else base(prices)
    return f


def sector_mom(prices, n=3):
    mom = (prices[SECTORS].iloc[-22] / prices[SECTORS].iloc[-253] - 1).dropna().sort_values(ascending=False)
    out = {t: 1 / n for t in mom.head(n).index if prices[t].iloc[-1] > prices[t].iloc[-200:].mean()}
    out["IEF"] = 1 - sum(out.values())
    return out


def levered(base, lev):
    def f(prices):
        w = base(prices)
        return {t: (v if t in DEFENSIVE else v * lev) for t, v in w.items()}
    return f


def blend2(a, b):
    def f(prices):
        out = {}
        for s in (a, b):
            for t, v in s(prices).items():
                out[t] = out.get(t, 0) + v / 2
        return out
    return f


def vol_target(base, target, max_lev, window=63):
    """Scale risk positions so the strategy's trailing vol approaches target."""
    def f(prices):
        w = base(prices)
        risk = {t: v for t, v in w.items() if t not in DEFENSIVE}
        if not risk:
            return w
        r = (prices[list(risk)].pct_change().iloc[-window:] * pd.Series(risk)).sum(axis=1)
        vol = r.std() * np.sqrt(252)
        k = float(np.clip(target / vol, 1.0, max_lev)) if vol > 0 else 1.0
        return {t: (v if t in DEFENSIVE else v * k) for t, v in w.items()}
    return f


def bootstrap(r: pd.Series, years=10, sims=2000, block=6, seed=1):
    m = (1 + r).resample("ME").prod() - 1
    rng = np.random.default_rng(seed)
    n, L = len(m), years * 12
    cagr, dd = [], []
    for _ in range(sims):
        idx = np.concatenate([np.arange(s, s + block) % n for s in rng.integers(0, n, L // block + 1)])[:L]
        eq = np.cumprod(1 + m.values[idx])
        cagr.append(eq[-1] ** (1 / years) - 1); dd.append((eq / np.maximum.accumulate(eq) - 1).min())
    cagr, dd = np.array(cagr), np.array(dd)
    return np.median(cagr), np.percentile(cagr, 5), np.mean(cagr < 0), np.mean(dd < -0.35), np.median(dd)


def main():
    P = load_prices(UNIVERSE2 + [CASH], start="2007-06-01").ffill()
    spy = P["SPY"].pct_change().fillna(0.0)
    A, B = with_market_rule(sector_mom), with_market_rule(momentum(8))
    bases = {"A_sector": A, "B_broad8": B, "C_blend": blend2(A, B)}
    rows, rets = [], {}
    def run(name, fn, fin=400.0):
        c = Config(max_leverage=3.0, dd_cap=1.0, financing_bps=fin)
        r = backtest(fn, P, c)[P.index[c.warmup_days]:]
        rets[name] = r
        fu, te, tr = metrics(r), metrics(r[SPLIT_DATE:], spy), metrics(r[:SPLIT_DATE].iloc[:-1])
        rows.append((name, tr, te, fu))
        print(f"{name:26}{tr['cagr']:>7.1%}/{tr['maxdd']:>6.1%}{te['cagr']:>8.1%}/{te['maxdd']:>6.1%}{fu['cagr']:>8.1%}/{fu['maxdd']:>6.1%}"
              f"{fu['sharpe']:>6.2f}{fu['calmar']:>7.2f}", flush=True)
    print(f"{'config':26}{'TRAIN cagr/dd':>14}{'TEST cagr/dd':>15}{'FULL cagr/dd':>15}{'Shp':>6}{'Calmar':>7}")
    for bn, b in bases.items():
        for lev in (1.0, 1.25, 1.5, 1.75, 2.0):
            run(f"{bn}@{lev}x", levered(b, lev))
    for tgt in (0.12, 0.15):
        run(f"A_sector volTgt{tgt:.0%}(max2x)", vol_target(A, tgt, 2.0))
    run("A_sector@1.5x fin6%", levered(A, 1.5), fin=600.0)
    run("B_broad8@1.5x fin6%", levered(B, 1.5), fin=600.0)
    run("SPY buy&hold", lambda p: {"SPY": 1.0})
    print("\nbootstrap (10y, 6m blocks, 2000 sims): median CAGR | 5th pct CAGR | P(loss) | P(maxDD<-35%) | median maxDD")
    for name in rets:
        if "fin6" in name:
            continue
        b = bootstrap(rets[name])
        print(f"  {name:26}{b[0]:>7.1%} |{b[1]:>7.1%} |{b[2]:>6.1%} |{b[3]:>6.1%} |{b[4]:>7.1%}")
    print("\nTRAIN-only pick: highest TRAIN CAGR with TRAIN maxDD >= -20%, then its TEST result:")
    ok = [x for x in rows if x[1]["maxdd"] >= -0.20 and "SPY" not in x[0]]
    if ok:
        n, tr, te, fu = max(ok, key=lambda x: x[1]["cagr"])
        print(f"  {n}: TRAIN {tr['cagr']:.1%}/{tr['maxdd']:.1%} -> TEST {te['cagr']:.1%}/{te['maxdd']:.1%}")


if __name__ == "__main__":
    main()
