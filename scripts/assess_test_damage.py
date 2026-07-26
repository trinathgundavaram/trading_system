#!/usr/bin/env python3
"""What did the 2026-07-24 test-suite run do to the production database?

READ-ONLY. Writes nothing, changes nothing. Run it before deciding on any
repair, and again afterwards to confirm the repair did what you expected.

Background: tests/conftest.py's docstring has the full story. In short, the
four legacy test modules construct `Database(path=tmp_path/...)` believing that
isolates them; since the Postgres migration `path` is a dead parameter, so they
ran against `trading_platform`. The destructive calls the suite makes, in the
order pytest collects them:

  test_account_sync   apply_remote_positions(db, [])   -> writes `alerts` rows
  test_live_trader    execute_buy_live / sell_live     -> real `positions`,
                                                          `trades`, `daily_stats`
  test_paper_trading  ensure_seeded, execute_buy,
                      close_position, RESET_PAPER_ACCOUNT
  test_rotation       ensure_seeded, log_rotation, a full rotation

`reset_paper_account()` is the serious one. It runs three unconditional
DELETEs: the whole `paper_account`, the whole `paper_trades` ledger, and every
`positions` row with `simulated=1` - open AND closed. Anything the later
test_rotation module then created is a fresh artefact sitting where your real
paper history used to be.

What is NOT touched by that reset, and therefore still authoritative:
`pattern_database` (the learning record, including closed outcomes), the real
`trades` ledger, `signals`, and `cycles`.

Usage:
    python3 scripts/assess_test_damage.py [--minutes 90] [--sqlite output/trading.db]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database  # noqa: E402

# Tickers the four legacy modules insert. Several are real names you may
# genuinely hold, so ticker alone is never sufficient evidence - always check
# it against the timestamp window.
TEST_TICKERS = ["AAA", "BBB", "CCC", "NEW", "FIX", "ORCL", "NVDA", "MU",
                "VRT", "BMY", "ASTS"]
OBVIOUSLY_SYNTHETIC = ["AAA", "BBB", "CCC", "NEW"]


def _scalar(db, sql, params=()):
    with db._conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


def _rows(db, sql, params=()):
    import sqlite3
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def section(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=90,
                    help="how far back the suspected test-run window extends")
    ap.add_argument("--sqlite", default="output/trading.db",
                    help="pre-Postgres snapshot to compare against")
    args = ap.parse_args()

    db = Database()
    cutoff = (datetime.utcnow() - timedelta(minutes=args.minutes)).isoformat()

    section(f"CURRENT STATE  (test window = last {args.minutes} min, since {cutoff}Z)")

    tables = [
        ("paper_trades", "created_at"),
        ("paper_equity_history", "timestamp"),
        ("trades", "timestamp"),
        ("positions", "entry_time"),
        ("pattern_database", "recorded_at"),
        # §49 (Phase 2.5). Added late, and that is the finding: the 2026-07-25
        # cleanup ran against a version of this list that did not include the
        # excursion table, so it was never inspected and never repaired. See
        # the dedicated section further down for what was still in there.
        ("mae_mfe_data", "recorded_at"),
        ("rotation_log", "executed_at"),
        ("monitoring_alerts", "triggered_at"),   # NOT created_at - the column has never existed, so this row read ERR and the alerts section below silently reported nothing (2026-07-25)
        ("signals", "timestamp"),
    ]
    print(f"{'table':<20} {'rows':>8} {'in window':>10}   earliest -> latest")
    for table, tscol in tables:
        try:
            total = _scalar(db, f"SELECT COUNT(*) FROM {table}")
            recent = _scalar(db, f"SELECT COUNT(*) FROM {table} WHERE {tscol} >= ?", (cutoff,))
            lo = _scalar(db, f"SELECT MIN({tscol}) FROM {table}")
            hi = _scalar(db, f"SELECT MAX({tscol}) FROM {table}")
            flag = "  <-- ALL rows are inside the test window" if (
                total and recent == total and total > 0) else ""
            print(f"{table:<20} {total:>8} {recent:>10}   {str(lo)[:19]} -> {str(hi)[:19]}{flag}")
        except Exception as e:
            print(f"{table:<20} {'ERR':>8}   {e}")

    section("PAPER ACCOUNT  (reset_paper_account wipes this table entirely)")
    acct = _rows(db, "SELECT * FROM paper_account")
    if not acct:
        print("EMPTY - the purse was deleted and not recreated.")
    for a in acct:
        print(f"  cash={a.get('cash')}  starting_cash={a.get('starting_cash')}  "
              f"realized_pnl={a.get('realized_pnl')}  created_at={a.get('created_at')}")
        if float(a.get("starting_cash") or 0) in (500.0, 1000.0):
            print("  ^ starting_cash matches a TEST fixture value (500 = test_paper_trading,")
            print("    1000 = test_rotation). Your config.yaml says "
                  f"{'1000' } - check which created this row via created_at.")

    section("POSITIONS  (open)")
    for label, sim in (("PAPER (simulated=1)", True), ("REAL (simulated=0)", False)):
        pos = db.get_all_positions(simulated=sim)
        print(f"\n{label}: {len(pos)} open")
        for p in sorted(pos, key=lambda r: str(r.get("entry_time") or "")):
            recent = str(p.get("entry_time") or "") >= cutoff
            mark = "  <-- CREATED IN TEST WINDOW" if recent else ""
            if p["ticker"] in OBVIOUSLY_SYNTHETIC:
                mark += "  [SYNTHETIC TICKER - test artefact]"
            print(f"  {p['ticker']:<6} entry={float(p.get('entry_price') or 0):<10.4f} "
                  f"sh={float(p.get('shares') or 0):<8.4f} "
                  f"mode={str(p.get('trade_mode') or '-'):<6} "
                  f"at={str(p.get('entry_time'))[:19]}{mark}")

    section("TODAY'S daily_stats  (execute_buy_live/sell_live wrote to this)")
    stats = db.get_daily_stats() or {}
    for k in ("date", "trades_placed", "realized_pnl", "winning_trades", "total_trades"):
        if k in stats:
            print(f"  {k:<16} {stats[k]}")
    print("\n  The failing test output showed trades_placed=8 and realized_pnl=-259.9111")
    print("  at the moment the suite ran. If these still look like that, they are")
    print("  test residue: the suite placed 8 mocked 'live' buys and closed BMY/ORCL.")

    section("MAE/MFE EXCURSIONS  (§49 - the table the 2026-07-25 cleanup missed)")
    print("  reset_paper_account() does NOT touch mae_mfe_data either, and unlike")
    print("  pattern_database that is not a mercy: this table has no provenance")
    print("  columns, no app_version, no config_fingerprint. Contaminated rows are")
    print("  indistinguishable from real ones except by the three tests below.\n")
    try:
        total_mae = _scalar(db, "SELECT COUNT(*) FROM mae_mfe_data") or 0
        print(f"  {total_mae} row(s) total")

        # 1. Test fixture tickers.
        placeholders = ",".join("?" * len(TEST_TICKERS))
        by_ticker = _rows(db, f"SELECT ticker, COUNT(*) n FROM mae_mfe_data "
                              f"WHERE UPPER(ticker) IN ({placeholders}) "
                              f"GROUP BY ticker ORDER BY n DESC", tuple(TEST_TICKERS))
        if by_ticker:
            print(f"\n  [1] TEST-FIXTURE TICKERS - {sum(r['n'] for r in by_ticker)} row(s)")
            for r in by_ticker:
                synth = "  [SYNTHETIC - cannot be a real holding]" if r["ticker"] in OBVIOUSLY_SYNTHETIC else ""
                print(f"        {r['ticker']:<6} {r['n']:>4}{synth}")
            print("      Ticker alone is not proof - ORCL/NVDA/MU/BMY are real names.")
            print("      Cross-check against test [2] and [3] below and the window above.")

        # 2. Both excursions exactly zero.
        #
        # A real trade moves. MAE and MFE are both measured from entry across
        # the life of the position, so both being exactly 0.0 means no price
        # was ever recorded against it - which is what a fixture that inserts a
        # row without running update_live() looks like.
        zeroed = _scalar(db, "SELECT COUNT(*) FROM mae_mfe_data "
                             "WHERE COALESCE(mae_pct,0) = 0 AND COALESCE(mfe_pct,0) = 0") or 0
        if zeroed:
            print(f"\n  [2] mae_pct = mfe_pct = 0.0 EXACTLY - {zeroed} row(s)")
            print("      A position that was ever priced has a non-zero excursion on at")
            print("      least one side. These are rows inserted without update_live()")
            print("      ever running - i.e. inserted by a test, not by a trade.")

        # 3. trade_id does not resolve, or resolves to a different ticker.
        #
        # The one that matters most for §51: a colliding trade_id does not
        # merely add a junk row, it makes a JOIN attach that row to somebody
        # else's pattern. Unrepairable by construction - there is no way to
        # learn which trade an excursion row with the wrong id belonged to.
        mismatched = _rows(db, """
            SELECT m.trade_id, m.ticker AS mae_ticker, p.ticker AS position_ticker,
                   COUNT(*) AS n
              FROM mae_mfe_data m
              LEFT JOIN positions p ON CAST(p.id AS TEXT) = CAST(m.trade_id AS TEXT)
             WHERE m.trade_id IS NOT NULL
               AND (p.id IS NULL OR UPPER(p.ticker) <> UPPER(m.ticker))
             GROUP BY m.trade_id, m.ticker, p.ticker
             ORDER BY n DESC""")
        if mismatched:
            print(f"\n  [3] trade_id DOES NOT RESOLVE TO A MATCHING POSITION - "
                  f"{sum(r['n'] for r in mismatched)} row(s)")
            print(f"        {'trade_id':<10} {'mae row':<8} {'position':<10} rows")
            for r in mismatched[:20]:
                print(f"        {str(r['trade_id']):<10} {str(r['mae_ticker']):<8} "
                      f"{str(r['position_ticker'] or 'MISSING'):<10} {r['n']:>4}")
            if len(mismatched) > 20:
                print(f"        ... and {len(mismatched) - 20} more")
            print("      These are the dangerous ones. §51's get_pattern_excursions()")
            print("      refuses them at query time, but migrations/010's unique index")
            print("      will REFUSE TO APPLY while duplicates remain - which is the")
            print("      intended gate. Purge before migrating.")

        # 4. Duplicate trade_id - what actually blocks migrations/010.
        dupes = _rows(db, "SELECT trade_id, COUNT(*) n FROM mae_mfe_data "
                          "WHERE trade_id IS NOT NULL GROUP BY trade_id "
                          "HAVING COUNT(*) > 1 ORDER BY n DESC")
        if dupes:
            print(f"\n  [4] DUPLICATE trade_id - {len(dupes)} id(s), "
                  f"{sum(r['n'] for r in dupes)} row(s)")
            for r in dupes[:10]:
                print(f"        trade_id={str(r['trade_id']):<10} {r['n']} rows")
            print("      migrations/010_mae_mfe_integrity.sql will fail until these are")
            print("      gone. Do not drop the constraint - finish the purge.")

        if not (by_ticker or zeroed or mismatched or dupes):
            print("\n  Clean: no fixture tickers, no all-zero excursions, no unresolved")
            print("  or duplicated trade_ids. migrations/010 will apply.")
    except Exception as e:
        print(f"  could not read mae_mfe_data: {e}")

    section("ALERTS created in the window (account_sync writes sync_missing_*)")
    try:
        alerts = _rows(db, "SELECT alert_type, message, triggered_at FROM monitoring_alerts "
                           "WHERE triggered_at >= ? ORDER BY triggered_at", (cutoff,))
        print(f"  {len(alerts)} alert(s)")
        for a in alerts[:15]:
            print(f"    {str(a['triggered_at'])[:19]}  {a['alert_type']}")
        if len(alerts) > 15:
            print(f"    ... and {len(alerts) - 15} more")
    except Exception as e:
        print(f"  could not read alerts: {e}")

    section("ROTATION LOG in the window")
    try:
        rot = _rows(db, "SELECT * FROM rotation_log WHERE executed_at >= ? "
                        "ORDER BY executed_at", (cutoff,))
        print(f"  {len(rot)} rotation(s) - the tests log 'LIVE'/'PAPER' rows with "
              f"candidate 'X'/'NEW'")
        for r in rot:
            print(f"    {str(r['executed_at'])[:19]}  book={r['book']:<5} "
                  f"{r['candidate_ticker']} -> victim {r['victim_ticker']}")
    except Exception as e:
        print(f"  could not read rotation_log: {e}")

    section("WHAT SURVIVED: pattern_database is the learning record")
    try:
        total = _scalar(db, "SELECT COUNT(*) FROM pattern_database")
        closed = _scalar(db, "SELECT COUNT(*) FROM pattern_database WHERE is_closed = 1")
        recent = _scalar(db, "SELECT COUNT(*) FROM pattern_database WHERE recorded_at >= ?",
                         (cutoff,))
        print(f"  {total} patterns, {closed} closed, {recent} recorded in the test window")
        print("  reset_paper_account() does NOT touch this table, so your closed-outcome")
        print("  history is intact apart from any rows the tests added (BMY, FIX).")
    except Exception as e:
        print(f"  could not read pattern_database: {e}")

    section("RECOVERY SOURCE: pre-Postgres SQLite snapshot")
    snap = Path(args.sqlite)
    if not snap.exists():
        print(f"  {snap} not found")
    else:
        import sqlite3
        print(f"  {snap} ({snap.stat().st_size / 1e6:.1f} MB, "
              f"mtime {datetime.fromtimestamp(snap.stat().st_mtime):%Y-%m-%d %H:%M})")
        try:
            con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            for t, tscol in (("paper_trades", "created_at"), ("positions", "entry_time"),
                              ("pattern_database", "recorded_at")):
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    lo = con.execute(f"SELECT MIN({tscol}) FROM {t}").fetchone()[0]
                    hi = con.execute(f"SELECT MAX({tscol}) FROM {t}").fetchone()[0]
                    print(f"    {t:<18} {n:>6} rows   {str(lo)[:19]} -> {str(hi)[:19]}")
                except Exception as e:
                    print(f"    {t:<18} unreadable: {e}")
            con.close()
            print("\n  This covers up to the 2026-07-21 migration. The 21-24 July gap is")
            print("  only in output/logs/scheduler.log - grep for '[PAPER] BOUGHT' and")
            print("  '[PAPER] SOLD' to reconstruct it if the ledger matters.")
        except Exception as e:
            print(f"  could not open snapshot: {e}")

    print("\n" + "=" * 72)
    print("Nothing above was modified. Decide the repair before running anything else.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
