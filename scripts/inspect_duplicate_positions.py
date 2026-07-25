#!/usr/bin/env python3
"""Show the duplicate open positions blocking §14's unique index. READ-ONLY.

migrations/006 cannot create uq_open_position_per_ticker_book while the book
holds two open rows for the same (ticker, book). That failure is the audit,
not an obstacle to work around: it means the race §14 describes has already
happened on this database.

This prints every duplicate group side by side so you can decide which row is
real. It does NOT delete anything, and there is no --fix flag, deliberately -
see the guidance it prints.

    python3 scripts/inspect_duplicate_positions.py

Once the book is reconciled, the index creates itself on the next Database()
construction (storage/database._ensure_open_position_uniqueness), or apply
migrations/006 by hand.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIELDS = ("id", "ticker", "entry_price", "entry_time", "shares", "dollar_amount",
          "trade_mode", "pattern_id", "current_stop_price", "trail_high",
          "entry_signal_score", "risk_per_share")


def main() -> int:
    from storage.database import Database
    db = Database()

    with db._conn() as conn:
        groups = conn.execute(
            """SELECT ticker, COALESCE(simulated, 0) AS book, COUNT(*) AS n
                 FROM positions WHERE status = 'open'
                GROUP BY 1, 2 HAVING COUNT(*) > 1
                ORDER BY 1""").fetchall()

    if not groups:
        print("No duplicate open positions. The §14 index should create "
              "cleanly - restart any running process, or apply "
              "migrations/006_unique_open_position.sql.")
        return 0

    print(f"{len(groups)} duplicated (ticker, book) group(s):\n")
    import sqlite3
    for ticker, book, n in groups:
        label = "PAPER" if book else "LIVE"
        print("=" * 74)
        print(f"{ticker}  [{label}]  - {n} open rows")
        print("=" * 74)
        with db._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(
                """SELECT * FROM positions
                    WHERE ticker = ? AND status = 'open'
                      AND COALESCE(simulated, 0) = ?
                    ORDER BY id""", (ticker, book)).fetchall()]
        for f in FIELDS:
            vals = [row.get(f) for row in rows]
            if all(v is None for v in vals):
                continue
            differs = len({repr(v) for v in vals}) > 1
            marker = " <-- differs" if differs else ""
            print(f"  {f:<20}" + "".join(f"{str(v):<24}" for v in vals) + marker)

        # The ledger is the tie-breaker: a position with no matching buy line
        # is the one that should not exist.
        with db._conn() as conn:
            conn.row_factory = sqlite3.Row
            buys = [dict(r) for r in conn.execute(
                """SELECT id, created_at, price, shares, dollar_amount, reason
                     FROM paper_trades
                    WHERE ticker = ? AND side = 'buy'
                    ORDER BY id DESC LIMIT 5""", (ticker,)).fetchall()]
        print(f"\n  recent paper_trades BUY lines for {ticker}: {len(buys)}")
        for b in buys:
            print(f"    #{b['id']} {b['created_at']} {b['shares']} sh @ "
                  f"${b['price']} (${b['dollar_amount']}) {b['reason']}")
        print()

    print("=" * 74)
    print("""
HOW TO RESOLVE

Decide which row is real, then DELETE the other outright:

    DELETE FROM positions WHERE id = <the duplicate>;

Do NOT resolve these by closing the extra row at an invented price. A close
writes a fictional P&L into paper_trades, which flows into daily_stats, into
the equity curve that §11's drawdown reads, and into the learning tables that
§15 just spent a migration cleaning. A fabricated fix is harder to find later
than the duplicate it replaced.

Useful signals for choosing, in rough order of reliability:

  * the ledger. A position with no matching paper_trades BUY line was never
    paid for - the purse was debited once, so one of these rows is unfunded.
    Check the count above against the number of open rows.
  * pattern_id. A row linked to a pattern is the one the learning path knows
    about; an unlinked duplicate is the loser of the race.
  * entry_context. A row with risk_per_share and entry_signal_score populated
    went through the §16 seeding; a row with NULLs did not.
  * lowest id. Under ON CONFLICT DO NOTHING the FIRST insert is the one that
    would have survived had the index existed.

After deleting, run:

    python3 scripts/reconcile.py        # the purse must still match the ledger
    python3 scripts/verify_phase2.py    # the index should now exist
""".strip())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
