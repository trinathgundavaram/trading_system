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
        ("rotation_log", "executed_at"),
        ("monitoring_alerts", "created_at"),
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

    section("ALERTS created in the window (account_sync writes sync_missing_*)")
    try:
        alerts = _rows(db, "SELECT alert_type, message, created_at FROM monitoring_alerts "
                           "WHERE created_at >= ? ORDER BY created_at", (cutoff,))
        print(f"  {len(alerts)} alert(s)")
        for a in alerts[:15]:
            print(f"    {str(a['created_at'])[:19]}  {a['alert_type']}")
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
