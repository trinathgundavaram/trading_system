#!/usr/bin/env python3
"""One-time cleanup for SEED positions created by the pre-2026-07-27 bug in
engine/paper_trader.py's ensure_seeded(): it cloned real positions into the
paper book (reason='seeded_from_real_portfolio') WITHOUT debiting
paper_account.cash for their cost, so they inflated the paper total for
free instead of counting against the $10,000 basis.

This is deliberately NOT the same operation as remove_seed_positions()
(the "Clear synced positions" button) - that one CREDITS cash back on the
assumption the cost was originally debited, which is true for
robinhood_sync.py's seed-paper CLI but false for these rows. Running that
button on these rows would double the drift instead of fixing it. This
script does the opposite: it deletes the position and its matching
'seeded_from_real_portfolio' ledger row and leaves cash untouched, because
cash was never reduced for them in the first place.

    python3 scripts/fix_uncosted_seed_positions.py            # dry run
    python3 scripts/fix_uncosted_seed_positions.py --apply    # writes

Verify afterwards with: python3 scripts/reconcile.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from storage.database import Database  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the change (default is a dry run)")
    args = ap.parse_args()

    db = Database()
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        pos_rows = conn.execute(
            "SELECT id, ticker, dollar_amount FROM positions "
            "WHERE COALESCE(simulated, 0) = 1 AND status = 'open' "
            "AND UPPER(COALESCE(trade_mode, '')) = 'SEED'"
        ).fetchall()
        pos_rows = [dict(r) for r in pos_rows]

        trade_rows = conn.execute(
            "SELECT id, ticker, dollar_amount FROM paper_trades "
            "WHERE reason = 'seeded_from_real_portfolio'"
        ).fetchall()
        trade_rows = [dict(r) for r in trade_rows]

    if not pos_rows and not trade_rows:
        print("Nothing to clean up - no uncosted SEED rows found.")
        return 0

    total = sum(r["dollar_amount"] or 0 for r in pos_rows)
    print(f"Open SEED positions never debited from cash ({len(pos_rows)}, ${total:,.2f} total):")
    for r in pos_rows:
        print(f"  position id={r['id']:<6} {r['ticker']:<8} ${r['dollar_amount']:,.2f}")
    print(f"\nMatching 'seeded_from_real_portfolio' ledger rows ({len(trade_rows)}):")
    for r in trade_rows:
        print(f"  paper_trades id={r['id']:<6} {r['ticker']:<8} ${r['dollar_amount']:,.2f}")

    print("\nPlan: DELETE these position rows and these ledger rows. "
          "paper_account.cash is NOT touched (it was never reduced for these).")

    if not args.apply:
        print("\nDRY RUN - re-run with --apply to write.")
        return 0

    with db._conn() as conn:
        conn.execute(
            "DELETE FROM positions WHERE COALESCE(simulated, 0) = 1 "
            "AND status = 'open' AND UPPER(COALESCE(trade_mode, '')) = 'SEED'"
        )
        conn.execute(
            "DELETE FROM paper_trades WHERE reason = 'seeded_from_real_portfolio'"
        )

    print(f"\nDONE: removed {len(pos_rows)} position row(s) and {len(trade_rows)} ledger row(s).")
    print("Verify with: python3 scripts/reconcile.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
