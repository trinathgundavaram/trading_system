#!/usr/bin/env python3
"""Does the TradingAgents rating add anything? For each logged decision at
least --horizon days old, compare the ticker's forward return with SPY's over
the same window, grouped by approve / neutral / veto. Needs ~30+ decisions per
group before the averages mean anything; the script says so.
    python3 scripts/tradingagents_report.py [--horizon 20]
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.tradingagents_advisor import bucket
from storage.database import Database


def forward_excess(rows, horizon, closes):
    """rows: [{as_of_date, ticker, rating}] ; closes: DataFrame incl. SPY.
    Returns {bucket: [excess_return, ...]} for decisions with a full horizon."""
    out = defaultdict(list)
    for r in rows:
        if r["ticker"] not in closes:
            continue
        s, b = closes[r["ticker"]].dropna(), closes["SPY"].dropna()
        i = s.index.searchsorted(pd.Timestamp(r["as_of_date"]))
        if i + horizon >= len(s):
            continue                                  # horizon not elapsed yet
        d0, d1 = s.index[i], s.index[i + horizon]
        ex = (s.iloc[i + horizon] / s.iloc[i] - 1) - (b.loc[d1] / b.loc[d0] - 1)
        out[bucket(r["rating"])].append(float(ex))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=20)
    a = ap.parse_args()
    rows = Database().get_ta_decisions()
    if not rows:
        print("no logged decisions yet - enable tradingagents_advisor and let it run.")
        return
    import yfinance as yf
    tickers = sorted({r["ticker"] for r in rows} | {"SPY"})
    closes = yf.download(tickers, start=min(r["as_of_date"] for r in rows), auto_adjust=True,
                         progress=False)["Close"]
    res = forward_excess(rows, a.horizon, closes)
    print(f"{len(rows)} decisions logged; {a.horizon}-trading-day excess return vs SPY\n")
    for b in ("approve", "neutral", "veto"):
        v = res.get(b, [])
        mean = f"{sum(v) / len(v):+.2%}" if v else "n/a"
        print(f"  {b:8} n={len(v):3}  mean excess {mean}")
    ap_, ve = res.get("approve", []), res.get("veto", [])
    if min(len(ap_), len(ve)) < 30:
        print("\nToo few matured decisions (<30 per side) to conclude anything - keep logging.")
    elif sum(ap_) / len(ap_) <= sum(ve) / len(ve):
        print("\nApproved names did NOT beat vetoed names - the rating adds no evidence of edge.")


if __name__ == "__main__":
    main()
