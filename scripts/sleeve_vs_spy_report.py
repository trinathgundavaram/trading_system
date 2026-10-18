#!/usr/bin/env python3
"""Monthly sector-momentum sleeve vs SPY report, from the daily sm_equity_log.
Prints month-by-month returns, cumulative return, max drawdown and the gap to
SPY, and says plainly when the sample is too short to conclude anything.
    python3 scripts/sleeve_vs_spy_report.py [--out output/sleeve_vs_spy.md]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database

MIN_MONTHS = 12


def summarize(rows: list) -> dict:
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("as_of_date"))
    df = df.sort_index()
    m = df.resample("ME").last()
    first = df.iloc[[0]]
    m = pd.concat([first, m]).drop_duplicates()
    ret = m.pct_change().dropna()
    dd = lambda s: float((s / s.cummax() - 1).min())
    return {
        "months": ret,
        "sleeve_total": float(df["total_value"].iloc[-1] / df["total_value"].iloc[0] - 1),
        "spy_total": float(df["spy_close"].iloc[-1] / df["spy_close"].iloc[0] - 1),
        "sleeve_dd": dd(df["total_value"]), "spy_dd": dd(df["spy_close"]),
        "days": len(df), "start": str(df.index[0].date()), "end": str(df.index[-1].date()),
    }


def render(r: dict) -> str:
    out = [f"# Sector momentum sleeve vs SPY ({r['start']} .. {r['end']}, {r['days']} daily marks)\n",
           "| Month | Sleeve | SPY | Gap |", "|---|---|---|---|"]
    for d, row in r["months"].iterrows():
        g = row["total_value"] - row["spy_close"]
        out.append(f"| {d:%Y-%m} | {row['total_value']:+.2%} | {row['spy_close']:+.2%} | {g:+.2%} |")
    out += ["", f"- Cumulative: sleeve **{r['sleeve_total']:+.2%}**, SPY **{r['spy_total']:+.2%}** "
                f"(gap {r['sleeve_total'] - r['spy_total']:+.2%})",
            f"- Max drawdown: sleeve **{r['sleeve_dd']:.1%}**, SPY **{r['spy_dd']:.1%}**"]
    n = len(r["months"])
    if n < MIN_MONTHS:
        out.append(f"\n> Only {n} month(s) of data. Research expects the sleeve to trail SPY in most "
                   f"bull months and win mainly in selloffs, so judge it over {MIN_MONTHS}+ months and "
                   f"include at least one drawdown in SPY before concluding anything.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = Database().get_sm_equity_log()
    if len(rows) < 2:
        print("Not enough data yet: enable sector_momentum_sleeve.paper_enabled and let the daily cycle log marks.")
        return
    text = render(summarize(rows))
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
