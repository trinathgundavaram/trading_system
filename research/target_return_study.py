#!/usr/bin/env python3
"""What would a 25% CAGR actually require? Sweeps leverage on the blended
momentum base (SPY<200d -> T-bills) well past 2x and also tests three fixed,
pre-listed 'enhancements' of the base. Reports CAGR, max drawdown, worst year,
10y bootstrap probability of a >50% drawdown. Nothing is tuned: every row is shown.
    python3 -m research.target_return_study
"""
import warnings

import numpy as np
import pandas as pd

from research.data import load_prices
from research.harness import Config, backtest, metrics
from research.leverage_study import (CASH, DEFENSIVE, bootstrap, blend2, levered, momentum, sector_mom,
                                     with_market_rule)
from research.strategies2 import UNIVERSE2

warnings.filterwarnings("ignore")


def buffered_rule(base, buf=0.02):
    """Enter risk-on above SMA200*(1+buf), exit below SMA200*(1-buf) (less whipsaw)."""
    state = {"on": True}
    def f(prices):
        spy, sma = prices["SPY"].iloc[-1], prices["SPY"].iloc[-200:].mean()
        if spy > sma * (1 + buf): state["on"] = True
        elif spy < sma * (1 - buf): state["on"] = False
        return base(prices) if state["on"] else {CASH: 1.0}
    return f


def main():
    P = load_prices(UNIVERSE2 + [CASH], start="2007-06-01").ffill()
    A, B = sector_mom, momentum(8)
    base = blend2(with_market_rule(A), with_market_rule(B))
    cands = {f"base@{l}x": levered(base, l) for l in (1.25, 2.0, 2.5, 3.0, 3.5, 4.0)}
    cands["momTop3_mkt@2.5x"] = levered(with_market_rule(momentum(3)), 2.5)           # enhancement 1: concentration
    cands["buffered2%_base@2.5x"] = levered(blend2(buffered_rule(A), buffered_rule(B)), 2.5)   # enhancement 2: less whipsaw
    cands["sector_only@2.5x"] = levered(with_market_rule(A), 2.5)                       # enhancement 3: drop broad leg
    print(f"{'config':24}{'CAGR':>8}{'maxDD':>8}{'worst yr':>9}{'Sharpe':>7} | 10y boot: median / 5th pct CAGR | P(DD<-50%)")
    for n, fn in cands.items():
        c = Config(max_leverage=5.0, dd_cap=1.0, financing_bps=400.0)
        r = backtest(fn, P, c)[P.index[c.warmup_days]:]
        m = metrics(r)
        yr = ((1 + r).resample("YE").prod() - 1).min()
        mo = (1 + r).resample("ME").prod() - 1
        rng = np.random.default_rng(1); dd = []
        for _ in range(2000):
            idx = np.concatenate([np.arange(s, s + 6) % len(mo) for s in rng.integers(0, len(mo), 21)])[:120]
            eq = np.cumprod(1 + mo.values[idx]); dd.append((eq / np.maximum.accumulate(eq) - 1).min())
        b = bootstrap(r)
        print(f"{n:24}{m['cagr']:>8.1%}{m['maxdd']:>8.1%}{yr:>9.1%}{m['sharpe']:>7.2f} |{b[0]:>12.1%} /{b[1]:>6.1%}      |{np.mean(np.array(dd) < -0.5):>8.0%}", flush=True)


if __name__ == "__main__":
    main()
