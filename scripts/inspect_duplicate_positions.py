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


def _score(row: dict, buys: list) -> tuple:
    """Rank one row of a duplicate group. Higher is more likely to be the real
    position. Returns (score, [reasons]).

    The signals, in the order I would trust them:

      A MATCHING LEDGER LINE is the strongest. paper_trades records what the
      purse was actually debited for. A position with no buy line behind it
      was never paid for, which makes it the artefact rather than the trade.

      A pattern_id means the learning path knows about this row - it was
      opened through the normal signal flow and something downstream is
      already linked to it.

      ENTRY CONTEXT (risk_per_share, entry_signal_score) means §16's seeding
      ran, which again indicates the normal flow rather than a stray insert.

    Deliberately advisory. This prints a suggestion; it does not act on one.
    """
    score, why = 0, []

    match = next((b for b in buys
                  if abs(float(b.get("dollar_amount") or 0)
                         - float(row.get("dollar_amount") or 0)) < 0.01), None)
    if match:
        score += 4
        why.append(f"matches paper_trades buy #{match['id']} "
                   f"(${match['dollar_amount']})")
    else:
        why.append("NO matching buy line - this position was never paid for")

    if row.get("pattern_id"):
        score += 2
        why.append(f"linked to pattern #{row['pattern_id']}")
    if row.get("risk_per_share") is not None:
        score += 1
        why.append("has risk_per_share (§16 seeding ran)")
    if row.get("entry_signal_score") is not None:
        score += 1
        why.append("has entry_signal_score")
    return score, why


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suggest", action="store_true",
                    help="also print DELETE statements for the rows that look "
                         "like artefacts. Prints only - nothing is executed.")
    ap.add_argument("--out", metavar="FILE",
                    help="write the suggested SQL to FILE, ready for "
                         "./scripts/apply_migration.sh. Implies --suggest.")
    args = ap.parse_args()
    if args.out:
        args.suggest = True

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
    deletes = []
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

        # ── Verdict ────────────────────────────────────────────────────────
        scored = sorted(((_score(r, buys), r) for r in rows),
                        key=lambda t: (-t[0][0], t[1]["id"]))
        print("\n  assessment:")
        for (sc, why), r in scored:
            print(f"    id={r['id']:<6} score {sc}")
            for w in why:
                print(f"             - {w}")

        top = scored[0][0][0]
        tied = [r for (sc, _), r in scored if sc == top]
        if len(tied) > 1:
            print(f"\n  NO SUGGESTION: {len(tied)} rows score identically "
                  f"({top}). Nothing here distinguishes them, so this one is "
                  f"yours to decide - look at entry_time and the ledger "
                  f"timestamps above.")
        else:
            keep = scored[0][1]
            drop = [r for (_, _), r in scored[1:]]
            print(f"\n  suggestion: KEEP id={keep['id']}, "
                  f"delete {', '.join('id=' + str(r['id']) for r in drop)}")
            deletes.extend((ticker, r) for r in drop)

            # If BOTH rows were funded, deleting one leaves the purse having
            # paid for a position that no longer exists. reconcile.py compares
            # cash against the LEDGER, so it would still pass - the ledger and
            # the purse agree, and only the positions table disagrees with
            # both. Worth naming, because it is the one case where deleting is
            # not the whole fix.
            funded = sum(1 for (_, why), r in scored
                         if any("matches paper_trades" in w for w in why))
            if funded > 1:
                print(f"    NOTE: {funded} of these rows have their own buy "
                      f"line, so the purse was debited {funded} times. "
                      f"Deleting the row does NOT return that cash, and "
                      f"reconcile.py will still pass because cash and the "
                      f"ledger agree with each other. Decide separately "
                      f"whether the extra debit was real.")
        print()

    if args.suggest and deletes:
        print("=" * 74)
        print("SUGGESTED SQL - read it, then run it yourself. Nothing here has\n"
              "been executed. Take a backup first: ./scripts/tp backup\n")
        print("BEGIN;")
        for ticker, r in deletes:
            # ticker + status in the WHERE, not id alone. An id copied from
            # the wrong place - stale output, a worked example in a chat
            # window, another machine's database - would otherwise delete
            # whatever row happens to hold that id now. With the ticker
            # pinned, a wrong id deletes NOTHING and psql says "DELETE 0",
            # which is a question rather than a silent loss.
            print(f"  DELETE FROM positions WHERE id = {r['id']} "
                  f"AND ticker = '{ticker}' AND status = 'open';"
                  f"   -- ${r.get('dollar_amount')}, "
                  f"entry {r.get('entry_time')}")
        print("  -- verify before committing:")
        print("  SELECT ticker, COALESCE(simulated,0) AS book, COUNT(*)")
        print("    FROM positions WHERE status='open'")
        print("   GROUP BY 1,2 HAVING COUNT(*) > 1;   -- expect zero rows")
        print("COMMIT;")
        print()
        print("Inside a transaction on purpose: if the SELECT still returns\n"
              "rows, ROLLBACK and look again rather than committing a partial\n"
              "cleanup.\n")
        print("Run it either way:\n"
              "  psql trading_platform          # paste the block, then \\q\n"
              "  python3 scripts/inspect_duplicate_positions.py "
              "--out /tmp/dedupe.sql\n"
              "  ./scripts/apply_migration.sh /tmp/dedupe.sql\n")

    if args.out and deletes:
        # NO BEGIN/COMMIT in the file. apply_migration.sh runs psql with
        # --single-transaction, so it supplies the transaction itself; a
        # literal COMMIT in the middle would end that transaction early and
        # defeat the all-or-nothing guarantee the runner exists to give.
        lines = [
            "-- Generated by scripts/inspect_duplicate_positions.py --out",
            "-- §14: remove duplicate OPEN positions so migration 006's unique",
            "-- index can create. REVIEW BEFORE APPLYING - this deletes rows.",
            "--",
            "-- No BEGIN/COMMIT here on purpose: apply_migration.sh runs psql",
            "-- with --single-transaction and supplies them, and a literal",
            "-- COMMIT mid-file would end that transaction early.",
            "--",
            "-- rollback_safe: false - a deleted position row is not",
            "-- recoverable from this file. Take a backup first:",
            "--     ./scripts/tp backup",
            "",
        ]
        for ticker, r in deletes:
            lines.append(f"-- {ticker}: ${r.get('dollar_amount')}, "
                         f"entry {r.get('entry_time')}, "
                         f"pattern_id={r.get('pattern_id')}")
            # See the printed block above for why ticker and status are in the
            # WHERE clause and not just the id.
            lines.append(f"DELETE FROM positions WHERE id = {r['id']} "
                         f"AND ticker = '{ticker}' AND status = 'open';")
            lines.append("")
        path = Path(args.out)
        path.write_text("\n".join(lines))
        print("=" * 74)
        print(f"wrote {len(deletes)} DELETE statement(s) to {path}\n")
        print("Read it, then apply it through the runner that resolves the")
        print("database the same way the code does:\n")
        print(f"    ./scripts/tp backup")
        print(f"    less {path}")
        print(f"    ./scripts/apply_migration.sh {path}\n")

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
