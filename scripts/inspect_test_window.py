#!/usr/bin/env python3
"""READ-ONLY companion to scripts/repair_test_damage.py.

That script's default --window-start (2026-07-25T00:00:00) has no upper
bound, and a dry run against this database on 2026-07-28 returned row
counts several times larger than the script's own docstring describes for
the original incident (e.g. 28 paper_trades vs. the documented 10,
41 pattern_database rows vs. the documented 2) - strong evidence the open
window is now also catching real activity from the days since 2026-07-25,
not just that incident's test residue.

This prints, per table, a per-day row count (so the actual incident spike
and any gap before real activity resumes are visible at a glance) and a
timestamp-ordered sample of individual rows straddling 2026-07-25 in both
directions, with enough columns (ticker, price/amount, mode, reason) to
eyeball which look like test fixtures (AAA/BBB/CCC/NEW, suspiciously round
numbers, a burst of rows one second apart) versus real trading activity.
Changes nothing.

    python3 scripts/inspect_test_window.py
    python3 scripts/inspect_test_window.py --sample 40   # more rows per table
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from storage.database import Database  # noqa: E402

WINDOW_START = "2026-07-25T00:00:00"

# (table, timestamp column, extra columns to show, per-row label columns)
TABLES = [
    ("positions", "entry_time",
     "ticker, entry_price, shares, dollar_amount, simulated, trade_mode, status"),
    ("paper_trades", "created_at",
     "ticker, side, price, shares, dollar_amount, reason, trade_mode"),
    ("trades", "timestamp", "ticker, side, status"),
    ("pattern_database", "recorded_at", "ticker, mode, is_closed"),
    ("paper_equity_history", "timestamp", "total_value, cash, n_open"),
    ("rotation_log", "executed_at", "*"),
    ("mae_mfe_data", "recorded_at", "ticker, trade_id, mae_pct, mfe_pct"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=25,
                    help="rows to print per table on each side of the boundary (default 25)")
    ap.add_argument("--window-start", default=WINDOW_START)
    args = ap.parse_args()

    db = Database()
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row

        for table, tscol, cols in TABLES:
            print(f"\n{'=' * 78}\n{table}  (timestamp column: {tscol})\n{'=' * 78}")
            try:
                by_day = conn.execute(
                    f"SELECT SUBSTRING({tscol}, 1, 10) AS day, COUNT(*) AS n "
                    f"FROM {table} WHERE {tscol} >= ? GROUP BY 1 ORDER BY 1",
                    (args.window_start,),
                ).fetchall()
            except Exception as e:
                print(f"  SKIPPED ({e})")
                continue
            if not by_day:
                print("  (no rows in window)")
                continue
            print("  per-day count in window:")
            for r in by_day:
                print(f"    {r['day']}   {r['n']:>4} rows")

            select_cols = "*" if cols == "*" else f"{tscol}, {cols}"
            rows = conn.execute(
                f"SELECT {select_cols} FROM {table} WHERE {tscol} >= ? "
                f"ORDER BY {tscol} LIMIT ?",
                (args.window_start, args.sample),
            ).fetchall()
            print(f"\n  first {len(rows)} row(s) in window, oldest first:")
            for r in rows:
                print(f"    {dict(r)}")

    print(f"\n{'=' * 78}\nFor reference - repair_test_damage.py's documented original incident "
          f"scope (positions=35, paper_trades=10, pattern_database=2, "
          f"paper_equity_history=1, trades=4). Anything well beyond that, "
          f"especially with day-stamps after 2026-07-25, is very likely real "
          f"activity the unbounded default window would also delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
