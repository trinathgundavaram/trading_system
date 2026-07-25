#!/usr/bin/env python3
"""Cross-table integrity check (§15, Phase 2). READ-ONLY.

Exits NON-ZERO on any discrepancy, so it can gate a deploy or page you rather
than writing a report nobody reads. That is the whole design intent: the
production-write incident of 2026-07-25 sat undetected for four days, and the
purse-versus-ledger check below would have flagged it within one.

    python3 scripts/reconcile.py
    python3 scripts/reconcile.py --quiet     # only failures
    python3 scripts/reconcile.py --json      # machine-readable, for CI

Every check is a question with a right answer, phrased so that ROWS RETURNED
MEANS SOMETHING IS WRONG. A check that returns rows in the healthy case is a
check nobody will keep running.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# (name, why it matters, SQL). Rows returned == a finding.
CHECKS = [
    ("paper sells with no matching closed position",
     "A ledger line with no position behind it means the purse moved for a "
     "trade the position table has no record of.",
     """SELECT pt.id, pt.ticker FROM paper_trades pt
         WHERE pt.side = 'sell' AND NOT EXISTS (
           SELECT 1 FROM positions p
            WHERE p.ticker = pt.ticker AND p.status = 'closed'
              AND COALESCE(p.simulated, 0) = 1)"""),

    ("mae_mfe outcome disagrees with paper_trades by >0.1pp",
     "The ADPT case: the same trade recorded as -1.88%% in one table and "
     "-3.20%% in the other. Quarantined rows are excluded - they are already "
     "known-bad and would drown out new disagreements.",
     """SELECT m.ticker, m.outcome_pct, pt.pnl_pct
          FROM mae_mfe_data m
          JOIN paper_trades pt ON pt.ticker = m.ticker AND pt.side = 'sell'
         WHERE COALESCE(m.data_quality, 'ok') = 'ok'
           AND ABS(COALESCE(m.outcome_pct, 0) - COALESCE(pt.pnl_pct, 0)) > 0.1"""),

    ("mae_mfe hold time disagrees with the position by >0.1h",
     "Same finding, the other column. close_position() is now the single "
     "definition of hold time; a disagreement means something recomputed it.",
     """SELECT m.ticker, m.hold_hours
          FROM mae_mfe_data m
         WHERE COALESCE(m.data_quality, 'ok') = 'ok'
           AND m.hold_hours IS NOT NULL
           AND EXISTS (
             SELECT 1 FROM pattern_database pd
              WHERE pd.ticker = m.ticker AND pd.is_closed = 1
                AND COALESCE(pd.data_quality, 'ok') = 'ok'
                AND pd.hold_hours IS NOT NULL
                AND ABS(pd.hold_hours - m.hold_hours) > 0.1)"""),

    ("open positions with no stop",
     "An open position without a stop is an unbounded loss. This is the "
     "check that should have existed before 18 of 29 trades closed on stops.",
     """SELECT ticker, COALESCE(simulated, 0) AS book FROM positions
         WHERE status = 'open'
           AND COALESCE(trade_mode, '') NOT IN ('SYNC', 'SEED')
           AND (current_stop_price IS NULL OR current_stop_price <= 0)"""),

    ("stop at or above entry",
     "A stop that is not below the entry is not a stop - it either fires "
     "immediately or was written by something that did not know the price. "
     "SMFL had a stop exactly equal to its entry (2026-07-24 audit).",
     """SELECT ticker, entry_price, current_stop_price FROM positions
         WHERE status = 'open'
           AND COALESCE(trade_mode, '') NOT IN ('SYNC', 'SEED')
           AND current_stop_price IS NOT NULL
           AND current_stop_price >= entry_price"""),

    ("duplicate open positions in one book",
     "The §14 invariant. If this returns rows, the unique index is not on "
     "this database - check the CRITICAL log line at startup.",
     """SELECT ticker, COALESCE(simulated, 0) AS book, COUNT(*) AS n
          FROM positions WHERE status = 'open'
         GROUP BY 1, 2 HAVING COUNT(*) > 1"""),

    ("the §14 unique index is missing",
     "Without it the duplicate check above is a snapshot, not a guarantee - "
     "it says there are no duplicates right now, not that there cannot be.",
     """SELECT 'uq_open_position_per_ticker_book' AS missing_index
         WHERE NOT EXISTS (
           SELECT 1 FROM pg_indexes
            WHERE indexname = 'uq_open_position_per_ticker_book')"""),

    ("paper cash disagrees with the trade ledger",
     "THE check. Reconciling the purse against its own ledger catches a "
     "whole class of silent accounting drift, and it is the one that would "
     "have flagged the 2026-07-25 production-write incident within a day.",
     """SELECT a.cash, a.starting_cash - (
              SELECT COALESCE(SUM(CASE WHEN side = 'buy' THEN dollar_amount
                                       ELSE -dollar_amount END), 0)
                FROM paper_trades) AS expected
          FROM paper_account a
         WHERE ABS(a.cash - (a.starting_cash - (
              SELECT COALESCE(SUM(CASE WHEN side = 'buy' THEN dollar_amount
                                       ELSE -dollar_amount END), 0)
                FROM paper_trades))) > 0.01"""),

    ("closed positions still linked to an open pattern",
     "The learning loop reads closed patterns. A position that closed while "
     "its pattern stayed open is a trade whose outcome will never be learned "
     "from - it is missing evidence rather than wrong evidence, which is why "
     "nothing else notices it.",
     """SELECT p.ticker, p.pattern_id FROM positions p
          JOIN pattern_database pd ON pd.id = p.pattern_id
         WHERE p.status = 'closed' AND COALESCE(pd.is_closed, 0) = 0"""),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="only report findings")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--limit", type=int, default=10,
                    help="rows to show per finding (default 10)")
    args = ap.parse_args()

    from storage.database import Database
    db = Database()

    findings, results = 0, []
    for name, why, sql in CHECKS:
        try:
            with db._conn() as conn:
                rows = conn.execute(sql).fetchall()
        except Exception as e:
            # A check that cannot RUN is a finding. Reporting it as a pass
            # would make a broken query indistinguishable from a clean book,
            # which is the failure mode this whole script exists to avoid.
            results.append({"check": name, "status": "ERROR", "detail": str(e)[:300]})
            findings += 1
            if not args.json:
                print(f"[ERROR] {name}\n        {str(e)[:300]}")
            continue

        rows = [list(r) for r in rows]
        ok = not rows
        results.append({"check": name, "status": "PASS" if ok else "FAIL",
                        "n": len(rows), "rows": rows[:args.limit]})
        if ok:
            if not args.quiet and not args.json:
                print(f"[PASS ] {name}")
            continue

        findings += 1
        if not args.json:
            print(f"[FAIL ] {name}  ({len(rows)} row(s))")
            print(f"         {why}")
            for r in rows[:args.limit]:
                print(f"         {r}")
            if len(rows) > args.limit:
                print(f"         ... and {len(rows) - args.limit} more")

    if args.json:
        print(json.dumps({"findings": findings, "checks": results}, indent=2, default=str))
    elif findings:
        print(f"\n{findings} check(s) failed. Exiting 1.")
        print("Do NOT resolve these by writing corrective rows at invented "
              "prices - a fabricated fix propagates into paper_trades and from "
              "there into the learning tables, which is the problem §15 is "
              "cleaning up in the first place.")
    else:
        print(f"\nAll {len(CHECKS)} checks passed.")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
