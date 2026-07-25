#!/usr/bin/env python3
"""Backfill daily_stats drawdown from the existing paper equity curve (§11).

The curve already holds real history. Backfilling makes the metric immediately
comparable against the past instead of starting blank - which matters because
the whole point of a drawdown cap is to be set from what the account actually
did, not from a round number someone liked.

Idempotent: every day is recomputed from the curve, so this can be re-run after
any correction to the equity series. The intraday figure is written with
GREATEST, so a re-run can raise a recorded high-water mark but never erase one.

    python3 scripts/backfill_drawdown.py            # write, then report
    python3 scripts/backfill_drawdown.py --dry-run  # report only

The report is the reason to run this interactively rather than as a one-shot
SQL file: it prints the observed distribution of intraday drawdown, which is
the input to choosing risk.max_intraday_drawdown_pct. A cap set above every
value the account has ever produced is documentation; one set below the median
halts the session most days. Neither is knowable without looking.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report without writing daily_stats")
    args = ap.parse_args()

    from storage.database import Database
    db = Database()

    rows = db.get_paper_equity_history(limit=100000)
    if not rows:
        print("paper_equity_history is empty - nothing to backfill.")
        print("This is not an error: the curve is written one point per WATCH "
              "cycle, so it fills in as the scheduler runs.")
        return 0

    # Group by LOCAL day, matching update_drawdown()'s window exactly.
    offset = datetime.utcnow() - datetime.now()
    by_day: dict[str, list[float]] = {}
    skipped = 0
    for r in rows:
        tv = r.get("total_value")
        if tv is None:
            skipped += 1
            continue
        try:
            day = (datetime.fromisoformat(r["timestamp"]) - offset).date().isoformat()
        except (TypeError, ValueError):
            skipped += 1
            continue
        by_day.setdefault(day, []).append(float(tv))

    print(f"{len(rows)} equity points across {len(by_day)} local days"
          + (f" ({skipped} unusable, skipped)" if skipped else ""))

    written = 0
    if not args.dry_run:
        written = db.backfill_drawdown()
        print(f"wrote {written} day(s) to daily_stats\n")
    else:
        print("dry run - nothing written\n")

    # ── The distribution, which is the actual output worth reading ──────────
    print(f"{'day':<12} {'points':>7} {'intraday_dd%':>13} {'close':>12}")
    print("-" * 48)
    singles, observed = 0, []
    for day in sorted(by_day):
        eq = by_day[day]
        if len(eq) < 2:
            # One point is a level, not a curve. Counted, not charged as 0% -
            # "no drawdown" is a claim, and a single sample cannot make it.
            singles += 1
            continue
        peak, dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak * 100)
        observed.append(dd)
        print(f"{day:<12} {len(eq):>7} {dd:>13.3f} {eq[-1]:>12.2f}")

    if singles:
        print(f"\n{singles} day(s) had a single equity point and were skipped "
              f"- a level, not a curve.")

    if observed:
        s = sorted(observed)
        worst = s[-1]
        median = s[len(s) // 2]
        print(f"\nintraday drawdown: median {median:.2f}%  worst {worst:.2f}%  "
              f"over {len(s)} day(s)")
        print("\nSetting risk.max_intraday_drawdown_pct:")
        print(f"  - below {median:.2f}% halts the session on a typical day.")
        print(f"  - above {worst:.2f}% has never bound and would not have "
              f"halted anything yet.")
        if len(s) < 5:
            print(f"  - {len(s)} day(s) is not a distribution. Treat the "
                  f"configured 3.0% as provisional and revisit it once the "
                  f"curve has a few weeks in it.")
    else:
        print("\nNo day had more than one equity point, so no drawdown could "
              "be computed. The configured cap is unvalidated - it is a guess "
              "until this says otherwise.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
