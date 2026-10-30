#!/usr/bin/env python3
"""Backfill missed daily marks for every paper sleeve (see engine/mark_backfill.py). Safe to re-run.

    python3 scripts/backfill_marks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf

from engine.mark_backfill import backfill_missing_marks
from storage.database import Database
from storage.sleeve_ledger import SleeveLedger


def main():
    db = Database()
    for prefix in ("sm", "stk", "stl"):
        led = SleeveLedger(db, prefix)
        pos = led.get_open_sm_positions()
        if not pos or not led.get_sm_equity_log():
            print(f"{prefix}: nothing to do")
            continue
        tickers = sorted({p["ticker"] for p in pos} | {"SPY"})
        raw = yf.download([t.replace(".", "-") for t in tickers], period="60d", auto_adjust=True, progress=False)["Close"]
        raw.columns = [c.replace("-", ".") if c.replace("-", ".") in tickers else c for c in raw.columns]
        added = backfill_missing_marks(led, raw)
        print(f"{prefix}: added {len(added)} marks {added}")


if __name__ == "__main__":
    main()
