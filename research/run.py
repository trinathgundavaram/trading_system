#!/usr/bin/env python3
"""Run every registered strategy through the fixed harness and print a table.
    python3 -m research.run [--cost-bps 5] [--dd-cap 0.25] [--refresh]
"""
import argparse

from research.data import load_prices
from research.harness import Config, evaluate
from research.strategies import STRATEGIES


def pct(x):
    return f"{x * 100:6.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--dd-cap", type=float, default=0.25)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    cfg = Config(cost_bps=a.cost_bps, dd_cap=a.dd_cap)
    prices = load_prices(refresh=a.refresh)
    print(f"data {prices.index[0].date()} .. {prices.index[-1].date()}  cost={cfg.cost_bps}bps  dd_cap={cfg.dd_cap:.0%}\n")
    hdr = f"{'strategy':18}{'TRAIN cagr':>11}{'TEST cagr':>10}{'TEST shp':>9}{'TEST dd':>9}{'2008 dd':>9}{'2022 dd':>9}  verdict"
    print(hdr)
    for name, fn in STRATEGIES.items():
        r = evaluate(fn, prices, cfg)
        t = r["test"]
        d08 = pct(r["2008"]["maxdd"]) if r["2008"] else "   n/a"
        d22 = pct(r["2022"]["maxdd"]) if r["2022"] else "   n/a"
        failed = [k for k, v in r["accept"].items() if k != "passed" and not v]
        print(f"{name:18}{pct(r['train']['cagr']):>11}{pct(t['cagr']):>10}{t['sharpe']:9.2f}{pct(t['maxdd']):>9}{d08:>9}{d22:>9}  "
              f"{'PASS' if r['accept']['passed'] else 'fail: ' + ','.join(failed)}")
    spy = evaluate(STRATEGIES['spy_buy_hold'], prices, cfg)
    print(f"\nSPY TEST: cagr {pct(spy['spy_test']['cagr'])} sharpe {spy['spy_test']['sharpe']:.2f} maxdd {pct(spy['spy_test']['maxdd'])}")


if __name__ == "__main__":
    main()
