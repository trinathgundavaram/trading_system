#!/usr/bin/env python3
"""Break down the "paper cash disagrees with the trade ledger" reconcile.py
finding by `reason` and `trade_mode`, and surface individual rows close in
size to the drift. READ-ONLY - no writes, no --fix flag, deliberately (same
posture as inspect_duplicate_positions.py and reconcile.py itself): the goal
is to show you which bucket the mismatch lives in so a real cause can be
found, not to paper over it with an invented adjustment.

    python3 scripts/diagnose_cash_drift.py

Reads the live Postgres database (storage/database.py).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from storage.database import Database  # noqa: E402


def main() -> int:
    db = Database()

    with db._conn() as conn:
        conn.row_factory = sqlite3.Row

        acct = conn.execute("SELECT cash, starting_cash FROM paper_account WHERE id = 1").fetchone()
        if not acct:
            print("No paper account found.")
            return 1
        cash, starting_cash = float(acct["cash"]), float(acct["starting_cash"])

        rows = conn.execute(
            "SELECT id, ticker, side, price, shares, dollar_amount, reason, "
            "trade_mode, created_at FROM paper_trades ORDER BY id"
        ).fetchall()
        rows = [dict(r) for r in rows]

    net_buys = sum((r["dollar_amount"] or 0) if r["side"] == "buy" else -(r["dollar_amount"] or 0)
                   for r in rows)
    expected_cash = starting_cash - net_buys
    drift = cash - expected_cash

    print(f"cash            ${cash:,.2f}")
    print(f"starting_cash   ${starting_cash:,.2f}")
    print(f"net_buys        ${net_buys:,.2f}  ({len(rows)} ledger rows)")
    print(f"expected_cash   ${expected_cash:,.2f}")
    print(f"DRIFT           ${drift:+,.2f}   (positive = cash has more than the ledger explains)")

    def _bucket(key_fn, label):
        buckets: dict = {}
        for r in rows:
            k = key_fn(r) or "(none)"
            b = buckets.setdefault(k, {"buy": 0.0, "sell": 0.0, "n": 0})
            b[r["side"]] += r["dollar_amount"] or 0
            b["n"] += 1
        print(f"\n=== by {label} ===")
        print(f"{'key':<28}{'buys':>12}{'sells':>12}{'net':>12}{'n':>6}")
        for k, b in sorted(buckets.items(), key=lambda kv: -(kv[1]["buy"] - kv[1]["sell"])):
            net = b["buy"] - b["sell"]
            print(f"{k:<28}{b['buy']:>12,.2f}{b['sell']:>12,.2f}{net:>12,.2f}{b['n']:>6}")

    _bucket(lambda r: r["reason"], "reason")
    _bucket(lambda r: (r["trade_mode"] or "").upper(), "trade_mode")

    print(f"\n=== ledger rows within $0.01 of the drift magnitude (${abs(drift):,.2f}) ===")
    hits = [r for r in rows if abs((r["dollar_amount"] or 0) - abs(drift)) < 0.01]
    if not hits:
        print("(none - the drift isn't a single missing/duplicated row)")
    for r in hits:
        print(f"  id={r['id']:<6} {r['created_at']:<26} {r['side']:<5} {r['ticker']:<8} "
              f"${r['dollar_amount']:,.2f}  reason={r['reason']}  trade_mode={r['trade_mode']}")

    print(f"\n=== last 25 ledger rows (most recent first) ===")
    for r in rows[-25:][::-1]:
        print(f"  id={r['id']:<6} {r['created_at']:<26} {r['side']:<5} {r['ticker']:<8} "
              f"${(r['dollar_amount'] or 0):>10,.2f}  reason={r['reason']}  trade_mode={r['trade_mode']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
