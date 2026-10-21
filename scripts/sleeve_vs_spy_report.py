#!/usr/bin/env python3
"""Monthly side-by-side report: sector-momentum sleeve vs the original swing
engine (paper) vs SPY. Sleeve and SPY come from the daily sm_equity_log; the
swing engine from its own paper_equity_history (a separate ledger - the two
strategies share no cash or positions). Prints month-by-month returns,
cumulative return and max drawdown for each, over the sleeve's window, and says
plainly when the sample is too short to conclude anything.
    python3 scripts/sleeve_vs_spy_report.py [--out output/sleeve_vs_spy.md]
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database

MIN_MONTHS = 12


def summarize(rows: list, swing_rows: list = None) -> dict:
    """rows: sm_equity_log rows. swing_rows: paper_equity_history rows
    ({timestamp, total_value}); its last value on/before each sleeve date is used."""
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df.pop("as_of_date"))
    df = df.sort_index().rename(columns={"total_value": "sleeve", "spy_close": "spy"})
    if swing_rows:
        sw = pd.DataFrame(swing_rows)
        sw.index = pd.to_datetime(sw["timestamp"]).dt.normalize()
        sw = sw["total_value"].groupby(level=0).last().sort_index()
        df["swing"] = sw.reindex(sw.index.union(df.index)).ffill().reindex(df.index)
    m = pd.concat([df.iloc[[0]], df.resample("ME").last()]).drop_duplicates()
    ret = m.pct_change().dropna()
    dd = lambda s: float((s / s.cummax() - 1).min())
    out = {"months": ret, "days": len(df), "start": str(df.index[0].date()), "end": str(df.index[-1].date()),
           "cols": list(df.columns)}
    for c in df.columns:
        out[f"{c}_total"] = float(df[c].iloc[-1] / df[c].iloc[0] - 1)
        out[f"{c}_dd"] = dd(df[c])
    return out


def render(r: dict) -> str:
    names = {"sleeve": "Sector momentum", "swing": "Swing engine", "spy": "SPY"}
    cols = [c for c in ("sleeve", "swing", "spy") if c in r["cols"]]
    out = [f"# Strategies side by side ({r['start']} .. {r['end']}, {r['days']} daily marks)\n",
           "| Month | " + " | ".join(names[c] for c in cols) + " |", "|---|" + "---|" * len(cols)]
    for d, row in r["months"].iterrows():
        out.append(f"| {d:%Y-%m} | " + " | ".join(f"{row[c]:+.2%}" for c in cols) + " |")
    out += ["", "| | " + " | ".join(names[c] for c in cols) + " |", "|---|" + "---|" * len(cols),
            "| Cumulative | " + " | ".join(f"{r[c + '_total']:+.2%}" for c in cols) + " |",
            "| Max drawdown | " + " | ".join(f"{r[c + '_dd']:.1%}" for c in cols) + " |"]
    n = len(r["months"])
    if n < MIN_MONTHS:
        out.append(f"\n> Only {n} month(s) of data. Research expects the sector sleeve to trail SPY in most "
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
    db = Database()
    text = render(summarize(rows, db.get_paper_equity_history(limit=100000)))
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")


if __name__ == "__main__":
    main()
