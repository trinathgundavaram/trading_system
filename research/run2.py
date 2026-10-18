#!/usr/bin/env python3
"""Wide-universe sleeves through the same fixed harness. python3 -m research.run2"""
import warnings

from research.data import load_prices
from research.harness import Config, SPLIT_DATE, backtest, metrics
from research.strategies2 import STRATEGIES2, UNIVERSE2

warnings.filterwarnings("ignore")


def main():
    prices = load_prices(UNIVERSE2, start="2007-06-01")
    spy = prices["SPY"].pct_change().fillna(0.0)
    print(f"data {prices.index[0].date()}..{prices.index[-1].date()}  cost 5bps, financing 4%, max lev 2.0\n")
    print(f"{'strategy':22}{'TRAIN cagr/dd':>16}{'TEST cagr':>10}{'TEST dd':>9}{'TEST shp':>9}{'2008 dd':>9}{'2022 dd':>9}{'FULL cagr':>10}{'FULL dd':>9}")
    for n, fn in STRATEGIES2.items():
        c = Config(max_leverage=2.0, dd_cap=1.0)
        r = backtest(fn, prices, c)[prices.index[c.warmup_days]:]
        tr, te, fu = metrics(r[:SPLIT_DATE].iloc[:-1]), metrics(r[SPLIT_DATE:], spy), metrics(r)
        d08 = metrics(r["2008-01-01":"2009-03-31"])
        d22 = metrics(r["2022-01-01":"2022-12-31"])
        print(f"{n:22}{tr['cagr']:>9.1%}/{tr['maxdd']:>5.0%}{te['cagr']:>10.1%}{te['maxdd']:>9.1%}{te['sharpe']:>9.2f}"
              f"{(d08.get('maxdd') or 0):>9.1%}{d22['maxdd']:>9.1%}{fu['cagr']:>10.1%}{fu['maxdd']:>9.1%}")


if __name__ == "__main__":
    main()
