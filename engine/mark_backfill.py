"""Fill daily equity marks a sleeve missed (Mac asleep, scheduler down).

A sleeve logs one mark per trading day it runs. A gap leaves the drift monitor with too few days and the
growth chart with straight lines. When no trade happened inside a gap, the book is exactly reconstructable:
the open positions' shares x that day's close, plus the cash balance. That is all this does, and it refuses
to guess otherwise - if any open position was opened after the gap started, the gap is left alone.
"""
from __future__ import annotations

import pandas as pd

MAX_GAP_DAYS = 14   # older gaps are left alone: too long to trust "no trades happened in between"


def backfill_missing_marks(led, closes: pd.DataFrame, today: str | None = None) -> list[str]:
    """led: SleeveLedger-like (get_sm_equity_log, get_open_sm_positions, get_sm_account, log_sm_equity).
    closes: daily closes, columns = tickers (must include SPY and every open position).
    today: the date the caller is about to mark itself (never filled here). Returns the dates added."""
    log = led.get_sm_equity_log()
    acct = led.get_sm_account()
    pos = led.get_open_sm_positions()
    if not log or not acct or not pos or "SPY" not in closes.columns:
        return []
    marked = {r["as_of_date"] for r in log}
    last_mark = max(marked)
    entry = max((str(p["entry_date"])[:10] for p in pos if p.get("entry_date")), default="")
    if any(p["ticker"] not in closes.columns for p in pos):
        return []
    added = []
    for ts in closes.index:
        d = str(ts)[:10]
        if d <= min(marked) or d in marked or (today and d >= today):
            continue
        if (pd.Timestamp(d) - pd.Timestamp(last_mark)).days > MAX_GAP_DAYS and d > last_mark:
            continue
        prev_mark = max(m for m in marked if m < d)
        if entry > prev_mark:
            continue                    # a position was opened after the mark before this day: book not reconstructable
        row = closes.loc[ts]
        px = {p["ticker"]: row.get(p["ticker"]) for p in pos}
        if any(pd.isna(v) for v in px.values()) or pd.isna(row.get("SPY")):
            continue
        # Only gaps BETWEEN two existing marks are filled: after the last mark the caller's own mark covers it.
        if d > last_mark:
            continue
        total = float(acct["cash"]) + sum(float(p["shares"]) * float(px[p["ticker"]]) for p in pos)
        led.log_sm_equity(d, total, float(row["SPY"]))
        added.append(d)
    return added
