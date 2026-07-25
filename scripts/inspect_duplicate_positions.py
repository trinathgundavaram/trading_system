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


IDENTITY_FIELDS = ("ticker", "entry_price", "entry_time", "shares",
                   "dollar_amount", "trade_mode", "pattern_id", "simulated",
                   "current_stop_price", "current_target_price", "stop_state",
                   "entry_signal_score", "risk_per_share", "trail_high")


def _score(row: dict, unclaimed: list) -> tuple:
    """Rank one row of a duplicate group. Higher is more likely to be real.

    `unclaimed` is a MUTABLE pool of ledger lines not yet attributed to a
    row, and a match REMOVES the line from it. That one-to-one pairing is the
    correction to the first version of this, which let every row in a group
    match the same buy line: with two open positions and one buy line it
    reported both as funded, when the arithmetic plainly says one of them was
    never paid for. A scorer that answers "both are fine" to a question about
    which one is wrong is worse than not scoring at all.

    Greedy and order-dependent (rows are considered by ascending id), which is
    fine because it is used to describe a group, not to break a tie: when two
    rows are otherwise identical, whichever one claims the line first, the
    OTHER is flagged as unfunded and the group is reported as ambiguous
    anyway.

    The signals, in the order I would trust them:

      A MATCHING LEDGER LINE. paper_trades records what the purse was
      actually debited for.

      A pattern_id means the learning path knows about this row.

      ENTRY CONTEXT (risk_per_share, entry_signal_score) means §16's seeding
      ran, indicating the normal flow rather than a stray insert.
    """
    score, why = 0, []

    match = next((b for b in unclaimed
                  if abs(float(b.get("dollar_amount") or 0)
                         - float(row.get("dollar_amount") or 0)) < 0.01), None)
    if match:
        unclaimed.remove(match)          # one ledger line, one position
        score += 4
        why.append(f"claims paper_trades buy #{match['id']} "
                   f"(${match['dollar_amount']})")
    else:
        why.append("NO unclaimed buy line - nothing in the ledger paid for this")

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


def _identical(rows: list) -> bool:
    """True when the rows differ ONLY by id (and by columns that are pure
    bookkeeping, like the excursion trackers Loop B updates in place).

    This is the case where a tie-break is safe rather than arbitrary. If every
    field that describes the POSITION is equal, the two rows are not two
    candidate answers to "which is real" - they are one position written
    twice, and nothing is lost by keeping either. Choosing the lower id then
    follows the convention the index itself would have enforced: under
    ON CONFLICT DO NOTHING the first insert is the one that survives.
    """
    if len(rows) < 2:
        return False
    first = rows[0]
    return all(all(r.get(f) == first.get(f) for f in IDENTITY_FIELDS)
               for r in rows[1:])


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

        if len(buys) < len(rows):
            print(f"  -> {len(rows)} open rows but only {len(buys)} BUY "
                  f"line(s). At least one of these was never recorded in the "
                  f"ledger.")

        # ── Verdict ────────────────────────────────────────────────────────
        # One shared pool, consumed as rows claim lines - see _score().
        unclaimed = list(buys)
        scored = sorted(((_score(r, unclaimed), r) for r in rows),
                        key=lambda t: (-t[0][0], t[1]["id"]))
        print("\n  assessment:")
        for (sc, why), r in scored:
            print(f"    id={r['id']:<6} score {sc}")
            for w in why:
                print(f"             - {w}")

        top = scored[0][0][0]
        tied = [r for (sc, _), r in scored if sc == top]

        if _identical(rows):
            # Every field describing the position is equal, so these are not
            # two candidates for "which is real" - they are one position
            # written twice. Keeping the lowest id matches what the unique
            # index would have done under ON CONFLICT DO NOTHING.
            keep = min(rows, key=lambda r: r["id"])
            drop = [r for r in rows if r["id"] != keep["id"]]
            print(f"\n  IDENTICAL ROWS: every field except id is equal, so "
                  f"there is nothing to choose between them - this is one "
                  f"position written {len(rows)} times, not {len(rows)} "
                  f"candidate answers.")
            print(f"  suggestion: KEEP id={keep['id']} (lowest - what the "
                  f"index would have kept), delete "
                  f"{', '.join('id=' + str(r['id']) for r in drop)}")
            if str(rows[0].get("trade_mode") or "").upper() in ("SEED", "SYNC"):
                print(f"  These are {rows[0]['trade_mode']} rows: an "
                      f"informational mirror of the real account, not "
                      f"positions this engine chose to enter. Deleting the "
                      f"copy removes a double-count, it does not close a "
                      f"trade.")
            deletes.extend((ticker, r) for r in drop)
        elif len(tied) > 1:
            print(f"\n  NO SUGGESTION: {len(tied)} rows score identically "
                  f"({top}) but their fields DIFFER. Something distinguishes "
                  f"them that this scoring does not capture, so it is yours "
                  f"to decide - start from the fields marked '<-- differs' "
                  f"above.")
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

    if args.out and not deletes:
        # Was: write nothing, say nothing, and let `less` report "No such file
        # or directory" - which reads as a broken script rather than as the
        # script declining to guess.
        print("=" * 74)
        print(f"NOT written: {args.out}\n")
        print("There is nothing to suggest. Every group above is either")
        print("ambiguous or was left to you deliberately, and writing an empty")
        print("file would look like a cleanup that had already been done.\n")

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
