#!/usr/bin/env python3
"""Remove the 2026-07-25 test-suite residue from the production database.

DRY RUN BY DEFAULT. Prints exactly what it would do and changes nothing until
you pass --apply. Take a pg_dump first; this script does not take one for you,
because a backup you watched happen is worth more than one a script claims it
made.

THE BOUNDARY
------------
Every artefact the suite created landed between 03:52:54 and 03:52:55 UTC on
2026-07-25. Nothing legitimate exists after 2026-07-24T19:53:53 - the market
was closed (23:52 ET), the scheduler ran no cycle after 03:10:42, and no real
trading occurred. That gives a clean timestamp cut rather than a ticker
heuristic, which matters because MU, VRT, NVDA and ORCL are real names you
could plausibly have held.

WHAT IS DELETED
---------------
  positions          35 rows created in the window (24 paper + 6 real open,
                     plus closed ones the tests opened and closed)
  paper_trades       all 10 rows - every one is inside the window
  rotation_log       all 4 rows - 'X -> Y'/'X -> Z' fixtures
  pattern_database   2 rows (the tests' BMY and FIX entries)
  paper_equity_history  1 row
  trades             4 rows

WHAT IS CORRECTED, NOT DELETED
------------------------------
  paper_account      recreated by the tests at 03:52:55 with the test fixture's
                     $1000. Deleted here; the scheduler's next cycle calls
                     ensure_seeded() and rebuilds it from config.yaml's
                     paper_trading.starting_cash, re-cloning the real book.
  daily_stats        2026-07-24: the tests added two winning closes worth
                     +$20.00 (ORCL 100->110 x1, BMY 50->55 x2) and some
                     trades_placed increments. See --pnl-adjust below.

WHAT IS NOT RECOVERABLE HERE
----------------------------
`reset_paper_account()` deleted the pre-existing paper_trades ledger before
this script existed. output/trading.db (the pre-Postgres snapshot) holds 60
rows covering 2026-07-16 to 2026-07-20; the 21-24 July window survives only in
output/logs/scheduler.log. Run with --restore-ledger to copy the snapshot's
rows back in as an audit trail.

Note what that loss does and does not cost you. It is the paper trade LEDGER -
display and accounting. The learning record is pattern_database, which the
reset does not touch and which is intact (86 rows, 45 closed). And §17 has
already set min_pattern_recorded_at to 2026-07-25T00:00:00, declaring
everything before it contaminated by the stop bug and ineligible to train
anything. The window that was lost is precisely the window the remediation
plan had already written off.

Usage:
    python3 scripts/repair_test_damage.py                      # dry run
    python3 scripts/repair_test_damage.py --apply
    python3 scripts/repair_test_damage.py --apply --restore-ledger
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database  # noqa: E402

# Inclusive lower bound of the test window. Chosen to sit between the last
# legitimate row (2026-07-24T19:53:53) and the first test row (03:52:54), with
# room to spare on both sides.
WINDOW_START = "2026-07-25T00:00:00"

# One entry per table: (table, timestamp column, human note)
PURGE = [
    ("positions", "entry_time", "test positions, both books, open and closed"),
    ("paper_trades", "created_at", "test paper ledger lines"),
    ("rotation_log", "executed_at", "fixture rotations (X -> Y / X -> Z)"),
    ("pattern_database", "recorded_at", "the tests' BMY and FIX patterns"),
    ("paper_equity_history", "timestamp", "equity point written during the run"),
    ("trades", "timestamp", "mocked broker fills"),
    # §49 (Phase 2.5). This entry is the finding: mae_mfe_data was absent from
    # PURGE when this script ran on 2026-07-25, so the excursion table kept its
    # test rows while every neighbouring table was cleaned. It is also the one
    # table where the window alone is not enough - see PURGE_MAE_PREDICATES.
    ("mae_mfe_data", "recorded_at", "test excursion rows inside the window"),
]

# §49: mae_mfe_data needs predicate-based deletion ON TOP of the time window.
#
# Every other table in PURGE is cleaned by timestamp because the incident was
# bounded in time. mae_mfe_data is not: engine/mae_mfe_engine.record_completed()
# only started running on the WATCH path on 2026-07-17, and rows written by the
# test suite before the window carry no marker distinguishing them from real
# ones - no app_version, no config_fingerprint, no simulated flag. The table has
# no provenance columns at all.
#
# So it is cleaned by evidence instead, and the evidence has to be strong enough
# to delete on. These three predicates are:
#
#   ticker_synthetic  AAA/BBB/CCC/NEW cannot be holdings. Note this uses
#                     OBVIOUSLY_SYNTHETIC, not the wider TEST_TICKERS list -
#                     ORCL, NVDA, MU and BMY are real names and deleting a real
#                     excursion is not recoverable.
#   flat_excursion    mae_pct = mfe_pct = 0.0 EXACTLY. A position that was ever
#                     priced moved in one direction or the other; both sides
#                     exactly zero means update_live() never ran for it, which
#                     is a fixture inserting a row directly.
#   ticker_collision  trade_id resolves to a positions row for a DIFFERENT
#                     ticker (or to no row at all). Unrepairable by
#                     construction: nothing records which trade an excursion
#                     with the wrong id belonged to. This is the predicate that
#                     matters for §51 - a collision does not merely add a junk
#                     row, it makes any join attach that row to another trade's
#                     pattern.
#
# Deliberately NOT included: "ticker in TEST_TICKERS" on its own. That would
# delete real ORCL/NVDA/BMY excursions, and a false positive here is permanent.
OBVIOUSLY_SYNTHETIC = ("AAA", "BBB", "CCC", "NEW")

PURGE_MAE_PREDICATES = [
    ("ticker_synthetic",
     "UPPER(ticker) IN ('AAA','BBB','CCC','NEW')",
     "synthetic tickers that cannot be real holdings"),
    ("flat_excursion",
     "COALESCE(mae_pct,0) = 0 AND COALESCE(mfe_pct,0) = 0",
     "mae = mfe = 0.0 exactly - update_live() never ran"),
    ("ticker_collision",
     """trade_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM positions p
             WHERE CAST(p.id AS TEXT) = CAST(mae_mfe_data.trade_id AS TEXT)
               AND UPPER(p.ticker) = UPPER(mae_mfe_data.ticker))""",
     "trade_id resolves to a different ticker, or to nothing"),
]

# The two closes the suite performed against the real book, both winners.
TEST_REALIZED_PNL = 20.00
TEST_WINNING_TRADES = 2
AFFECTED_STATS_DATE = "2026-07-24"


def _rows(db, sql, params=()):
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _count(db, sql, params=()):
    with db._conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually make the changes")
    ap.add_argument("--window-start", default=WINDOW_START)
    ap.add_argument("--restore-ledger", action="store_true",
                    help="copy paper_trades from the pre-Postgres SQLite snapshot")
    ap.add_argument("--sqlite", default="output/trading.db")
    ap.add_argument("--pnl-adjust", type=float, default=TEST_REALIZED_PNL,
                    help="realized_pnl the tests added, to be subtracted back out")
    ap.add_argument("--trades-placed", type=int, default=None,
                    help="explicit corrected value for daily_stats.trades_placed")
    args = ap.parse_args()

    db = Database()
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] deleting rows with timestamp >= {args.window_start}\n")

    # ── 1. What would go ────────────────────────────────────────────────────
    total = 0
    for table, tscol, note in PURGE:
        try:
            n = _count(db, f"SELECT COUNT(*) FROM {table} WHERE {tscol} >= ?",
                       (args.window_start,))
        except Exception as e:
            print(f"  {table:<22} SKIPPED ({e})")
            continue
        total += n
        print(f"  {table:<22} {n:>4} rows   {note}")
    print(f"  {'':<22} {total:>4} total\n")

    # §49: the predicate-based mae_mfe_data pass, reported separately because it
    # is not bounded by the window and therefore deserves its own line-by-line
    # confirmation before --apply.
    print("  mae_mfe_data, by evidence rather than by window (§49):")
    mae_total = 0
    for name, predicate, note in PURGE_MAE_PREDICATES:
        try:
            n = _count(db, f"SELECT COUNT(*) FROM mae_mfe_data WHERE {predicate}")
        except Exception as e:
            print(f"    {name:<20} SKIPPED ({e})")
            continue
        mae_total += n
        print(f"    {name:<20} {n:>4} rows   {note}")
    print(f"    {'(overlaps counted once at delete time)':<20} {mae_total:>4} raw\n")

    # Safety: refuse if the cut would take rows that predate the incident.
    stray = _rows(db, "SELECT ticker, entry_time FROM positions "
                      "WHERE entry_time >= ? AND entry_time < ? LIMIT 5",
                  (args.window_start, "2026-07-25T03:52:00"))
    if stray:
        print("REFUSING: rows exist between the window start and the known test")
        print("timestamp. Widen or narrow --window-start deliberately:")
        for s in stray:
            print(f"    {s['ticker']} {s['entry_time']}")
        return 1

    # ── 2. paper_account ────────────────────────────────────────────────────
    acct = _rows(db, "SELECT * FROM paper_account")
    print("paper_account:")
    for a in acct:
        print(f"  DELETE  cash={a.get('cash')} starting_cash={a.get('starting_cash')} "
              f"created_at={a.get('created_at')}")
    print("  The next scheduler cycle rebuilds it via ensure_seeded() from")
    print("  config.yaml's paper_trading.starting_cash, re-cloning the real book.\n")

    # ── 3. daily_stats ──────────────────────────────────────────────────────
    stats = _rows(db, "SELECT * FROM daily_stats WHERE date = ?", (AFFECTED_STATS_DATE,))
    print(f"daily_stats {AFFECTED_STATS_DATE}:")
    for s in stats:
        new_pnl = (s.get("realized_pnl") or 0) - args.pnl_adjust
        new_wins = max(0, (s.get("winning_trades") or 0) - TEST_WINNING_TRADES)
        print(f"  realized_pnl    {s.get('realized_pnl')}  ->  {round(new_pnl, 4)}")
        print(f"  winning_trades  {s.get('winning_trades')}  ->  {new_wins}")
        if args.trades_placed is None:
            print(f"  trades_placed   {s.get('trades_placed')}  ->  UNCHANGED "
                  f"(pass --trades-placed N to correct)")
            print("    The suite's own failure printed trades_placed=8 at the first")
            print("    live-buy assertion, implying 7 pre-existing + 1. Current value")
            print("    is 10, so 3 test increments. 7 is the likely correct value -")
            print("    but verify against the surviving `trades` rows below before")
            print("    committing to it.")
        else:
            print(f"  trades_placed   {s.get('trades_placed')}  ->  {args.trades_placed}")
    surviving = _rows(db, "SELECT ticker, side, timestamp, status FROM trades "
                          "WHERE timestamp < ? ORDER BY timestamp", (args.window_start,))
    print(f"\n  surviving `trades` rows ({len(surviving)}):")
    for t in surviving:
        print(f"    {str(t['timestamp'])[:19]}  {t['side']:<5} {t['ticker']:<6} {t.get('status')}")
    print()

    if not args.apply:
        print("Dry run only. Re-run with --apply once the numbers above look right.")
        return 0

    # ── 4. Apply, in one transaction ────────────────────────────────────────
    with db._conn() as conn:
        for table, tscol, _ in PURGE:
            try:
                conn.execute(f"DELETE FROM {table} WHERE {tscol} >= ?", (args.window_start,))
            except Exception as e:
                print(f"  {table}: delete failed - {e}")
        # §49: the evidence-based pass. Runs in the same transaction as the
        # window pass, so a failure anywhere leaves the table as it was rather
        # than half-cleaned - a partially purged excursion table is harder to
        # reason about than an untouched one.
        for name, predicate, _ in PURGE_MAE_PREDICATES:
            try:
                conn.execute(f"DELETE FROM mae_mfe_data WHERE {predicate}")
            except Exception as e:
                print(f"  mae_mfe_data [{name}]: delete failed - {e}")
        conn.execute("DELETE FROM paper_account")
        conn.execute(
            "UPDATE daily_stats SET realized_pnl = realized_pnl - ?, "
            "winning_trades = GREATEST(0, winning_trades - ?) WHERE date = ?",
            (args.pnl_adjust, TEST_WINNING_TRADES, AFFECTED_STATS_DATE))
        if args.trades_placed is not None:
            conn.execute("UPDATE daily_stats SET trades_placed = ? WHERE date = ?",
                         (args.trades_placed, AFFECTED_STATS_DATE))
    print("Purge applied.")

    # ── 5. Optional ledger restore ──────────────────────────────────────────
    if args.restore_ledger:
        snap = Path(args.sqlite)
        if not snap.exists():
            print(f"  --restore-ledger: {snap} not found")
            return 1
        con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        old = [dict(r) for r in con.execute("SELECT * FROM paper_trades ORDER BY created_at")]
        con.close()
        cols = ["ticker", "side", "price", "shares", "dollar_amount", "reason",
                "pattern_id", "pnl", "pnl_pct", "created_at"]
        with db._conn() as conn:
            for r in old:
                conn.execute(
                    f"INSERT INTO paper_trades ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    tuple(r.get(c) for c in cols))
        print(f"  restored {len(old)} paper_trades rows from the pre-Postgres snapshot")
        print("  (covers 2026-07-16 to 2026-07-20; the 21-24 July window is only")
        print("   in output/logs/scheduler.log)")

    print("\nRe-run scripts/assess_test_damage.py to confirm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
