#!/usr/bin/env python3
"""Drawdown-cap frontier. For each cap, the winner is chosen on TRAIN ONLY
(max TRAIN CAGR with TRAIN max drawdown <= cap, optionally with the drawdown
brake) and then reported on the frozen TEST window. Every strategy x brake
combination counts as one trial; the trial count is printed because the more
trials, the more a best-of-N result is partly luck.
    python3 -m research.frontier
"""
import warnings

from research.data import load_prices
from research.harness import Config, SPLIT_DATE, backtest, metrics
from research.strategies import STRATEGIES

warnings.filterwarnings("ignore")
CAPS = [0.10, 0.15, 0.20, 0.25, 0.35]
BRAKES = [None, (0.05, 0.15), (0.08, 0.20)]     # (start, full) shadow-drawdown brake


def main():
    prices = load_prices()
    spy = prices["SPY"].pct_change().fillna(0.0)
    rows = []
    for name, fn in STRATEGIES.items():
        for br in BRAKES:
            cfg = Config(max_leverage=2.0, dd_cap=1.0,
                         brake_start=br[0] if br else None, brake_full=br[1] if br else 0.2)
            r = backtest(fn, prices, cfg)[prices.index[cfg.warmup_days]:]
            tr, te = metrics(r[:SPLIT_DATE].iloc[:-1]), metrics(r[SPLIT_DATE:], spy)
            full = metrics(r)
            rows.append((name, br, tr, te, full))
    print(f"trials: {len(rows)}   (TEST SPY: cagr {metrics(spy[SPLIT_DATE:])['cagr']:.1%}, "
          f"dd {metrics(spy[SPLIT_DATE:])['maxdd']:.1%})\n")
    print(f"{'cap':>5}  {'pick (chosen on TRAIN)':34}{'TRAIN cagr/dd':>16}{'TEST cagr':>10}{'TEST dd':>9}{'TEST shp':>9}{'FULL dd':>9}")
    for cap in CAPS:
        ok = [x for x in rows if abs(x[2]["maxdd"]) <= cap]
        if not ok:
            print(f"{cap:>5.0%}  nothing satisfies the cap on TRAIN"); continue
        n, br, tr, te, full = max(ok, key=lambda x: x[2]["cagr"])
        tag = f"{n}{'+brake' + str(br) if br else ''}"
        print(f"{cap:>5.0%}  {tag:34}{tr['cagr']:>9.1%}/{tr['maxdd']:>5.0%}{te['cagr']:>10.1%}{te['maxdd']:>9.1%}{te['sharpe']:>9.2f}{full['maxdd']:>9.1%}")
    print("\nAll trials, TEST window (sorted by TEST Sharpe):")
    for n, br, tr, te, full in sorted(rows, key=lambda x: -x[3]["sharpe"])[:12]:
        print(f"  {n + ('+brake' + str(br) if br else ''):40} cagr {te['cagr']:6.1%} dd {te['maxdd']:6.1%} shp {te['sharpe']:.2f}  full dd {full['maxdd']:6.1%}")


if __name__ == "__main__":
    main()
