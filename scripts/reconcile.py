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
import datetime as _dt
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

    ("stop at or above entry BEFORE it ever advanced",
     "A stop set at or above entry at ENTRY TIME is not a stop - it fires "
     "immediately, or it was written by something that did not know the "
     "price. SMFL had a stop exactly equal to its entry (2026-07-24 audit).\n"
     "         Restricted to stops still in INITIAL_RISK. Once the stop "
     "machine advances to BREAKEVEN / PROFIT_PROTECT / TREND_FOLLOWING, a "
     "stop ABOVE entry is the goal, not a defect - it means the position can "
     "no longer lose money. The first version of this check omitted that "
     "condition and flagged healthy profit-protected positions, which is how "
     "a reconciliation script teaches people to ignore it.",
     """SELECT ticker, entry_price, current_stop_price,
               COALESCE(stop_state, '(never set)') AS stop_state
          FROM positions
         WHERE status = 'open'
           AND COALESCE(trade_mode, '') NOT IN ('SYNC', 'SEED')
           AND current_stop_price IS NOT NULL
           AND current_stop_price >= entry_price
           AND COALESCE(UPPER(stop_state), 'INITIAL_RISK') = 'INITIAL_RISK'"""),

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


BASELINE = REPO / "docs" / "reconcile_baseline.json"


def _key(row) -> str:
    """A stable identity for one finding row, for baseline comparison."""
    return "|".join("" if v is None else str(v) for v in row)


def _load_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    try:
        return json.loads(BASELINE.read_text()).get("findings", {})
    except Exception as e:
        print(f"WARNING: could not read {BASELINE.name} ({e}). Treating every "
              f"finding as new, which is the safe direction.")
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="only report findings")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--limit", type=int, default=10,
                    help="rows to show per finding (default 10)")
    ap.add_argument("--accept-baseline", metavar="REASON",
                    help="record today's findings as KNOWN, with REASON. "
                         "Future runs then fail only on findings that are NEW. "
                         "Writes docs/reconcile_baseline.json - commit it, so "
                         "the list is reviewable and its growth shows in a diff.")
    args = ap.parse_args()

    from storage.database import Database
    db = Database()

    baseline = {} if args.accept_baseline else _load_baseline()
    new_findings, results, to_baseline = 0, [], {}
    known_total, resolved = 0, []

    for name, why, sql in CHECKS:
        try:
            with db._conn() as conn:
                rows = conn.execute(sql).fetchall()
        except Exception as e:
            # A check that cannot RUN is a finding. Reporting it as a pass
            # would make a broken query indistinguishable from a clean book,
            # which is the failure mode this whole script exists to avoid.
            results.append({"check": name, "status": "ERROR", "detail": str(e)[:300]})
            new_findings += 1
            if not args.json:
                print(f"[ERROR] {name}\n        {str(e)[:300]}")
            continue

        rows = [list(r) for r in rows]
        keys = [_key(r) for r in rows]
        to_baseline[name] = keys

        accepted = set(baseline.get(name, []))
        # A baseline entry that no longer appears is GOOD NEWS, and worth
        # saying so - otherwise the baseline silently accumulates rows that
        # were fixed years ago and stops describing anything.
        gone = accepted - set(keys)
        if gone:
            resolved.append((name, len(gone)))

        new_rows = [r for r, k in zip(rows, keys) if k not in accepted]
        known_n = len(rows) - len(new_rows)
        known_total += known_n

        status = "PASS" if not rows else ("KNOWN" if not new_rows else "FAIL")
        results.append({"check": name, "status": status, "n": len(rows),
                        "new": len(new_rows), "known": known_n,
                        "rows": new_rows[:args.limit] or rows[:args.limit]})

        if not rows:
            if not args.quiet and not args.json:
                print(f"[PASS ] {name}")
            continue

        if not new_rows:
            if not args.json:
                print(f"[KNOWN] {name}  ({known_n} row(s), all accepted)")
            continue

        new_findings += 1
        if not args.json:
            print(f"[FAIL ] {name}  ({len(new_rows)} NEW"
                  + (f", {known_n} known" if known_n else "") + ")")
            print(f"         {why}")
            for r in new_rows[:args.limit]:
                print(f"         {r}")
            if len(new_rows) > args.limit:
                print(f"         ... and {len(new_rows) - args.limit} more")

    if args.accept_baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({
            "accepted_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "reason": args.accept_baseline,
            "findings": {k: v for k, v in to_baseline.items() if v},
        }, indent=2))
        n = sum(len(v) for v in to_baseline.values())
        print(f"\nAccepted {n} finding(s) into {BASELINE.relative_to(REPO)}.")
        print(f"Reason recorded: {args.accept_baseline}")
        print("\nCOMMIT THAT FILE. It is a list of things known to be wrong "
              "and deliberately tolerated, which is exactly the kind of list "
              "that should be reviewable and whose growth should show up in a "
              "diff. Accepting a finding is not fixing it.")
        return 0

    if args.json:
        print(json.dumps({"new": new_findings, "known": known_total,
                          "checks": results}, indent=2, default=str))
        return 1 if new_findings else 0

    for name, n in resolved:
        print(f"\n[RESOLVED] {n} baselined row(s) no longer appear in: {name}")
        print("           Re-run with --accept-baseline to prune them.")

    if new_findings:
        print(f"\n{new_findings} check(s) have NEW findings. Exiting 1.")
        print("Do NOT resolve these by writing corrective rows at invented "
              "prices - a fabricated fix propagates into paper_trades and from "
              "there into the learning tables, which is the problem §15 is "
              "cleaning up in the first place.")
        if known_total:
            print(f"\n({known_total} further row(s) are already accepted in "
                  f"{BASELINE.name} and are not counted above.)")
    elif known_total:
        print(f"\nNo new findings. {known_total} row(s) remain accepted in "
              f"{BASELINE.name} - known damage, not drift.")
    else:
        print(f"\nAll {len(CHECKS)} checks passed.")

    return 1 if new_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
