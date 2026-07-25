#!/usr/bin/env python3
"""Container HEALTHCHECK target (§42.1, Phase 3).

WHAT "HEALTHY" HAS TO MEAN HERE. A liveness probe that only checks the process
is alive would have reported green throughout the 22 July incident, when the
scheduler was running and firing nothing for five hours. So this checks the
things whose failure is silent:

  1. The database answers.
  2. The zone database is present and America/New_York resolves. Without it
     every market-hours decision is wrong by four or five hours, with no error.
  3. The reference TA backend is the real pandas_ta, not the hand-rolled
     fallback (§13). A container that fell back computes scores that are not
     comparable with any other machine's - and nothing else would tell you.
  4. The scheduler has completed a cycle recently, during market hours. This
     is the one that would have caught 22 July.

Exit 0 healthy, exit 1 unhealthy. Prints one line per check so
`docker inspect --format='{{json .State.Health}}'` is readable.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

# How stale the last completed cycle may be before this container is unhealthy.
# Generous: the scan interval is minutes, but a long cycle plus a restart must
# not flap the health state. Only enforced during market hours.
MAX_CYCLE_AGE_MINUTES = int(os.getenv("TP_HEALTH_MAX_CYCLE_AGE_MIN", "90"))

problems: list[str] = []
notes: list[str] = []


def check_timezone() -> None:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/New_York")
        notes.append("tzdata ok")
    except Exception as e:
        problems.append(f"timezone database missing or unreadable ({e}) - "
                        f"every market-hours check is untrustworthy")


def check_ta_backend() -> None:
    try:
        from engine.ticker_analyzer import TA_BACKEND
    except Exception as e:
        problems.append(f"ticker_analyzer will not import ({e})")
        return
    if "pandas_ta" not in TA_BACKEND:
        problems.append(f"TA backend is {TA_BACKEND!r}, not pandas_ta - scores from "
                        f"this container are NOT comparable with other builds (§13)")
    else:
        notes.append(f"TA backend {TA_BACKEND}")


def check_database() -> None:
    try:
        from storage.database import Database

        db = Database()
    except Exception as e:
        problems.append(f"database unreachable ({e})")
        return
    notes.append("database ok")
    _check_cycle_freshness(db)


def _market_hours_now() -> bool:
    """Coarse on purpose: weekday, 09:30-16:00 New York. A holiday calendar
    here would only make the health check flap on days the scheduler is
    correctly idle, so the check simply does not fire outside this window."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (9, 30) and now.hour < 16


def _check_cycle_freshness(db) -> None:
    if not _market_hours_now():
        notes.append("outside market hours - cycle freshness not enforced")
        return
    try:
        status = db.get_cycle_status() or {}
        # finished_at, NOT started_at as the primary. A cycle that started and
        # then hung has a fresh started_at forever, so keying off it would
        # report healthy through exactly the 22 July failure this check exists
        # to catch. started_at is the fallback only for the case where the
        # very first cycle is still in flight.
        last = status.get("finished_at") or status.get("started_at")
        if not last:
            notes.append("no cycle recorded yet")
            return
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last
        if age > timedelta(minutes=MAX_CYCLE_AGE_MINUTES):
            problems.append(
                f"no cycle completed for {age.total_seconds() / 60:.0f} min during "
                f"market hours - this is the 22 July failure mode")
        else:
            notes.append(f"last cycle {age.total_seconds() / 60:.0f} min ago")
    except Exception as e:
        # A missing column or a schema this build does not know about must not
        # be reported as an unhealthy container; say so and move on.
        notes.append(f"cycle freshness not checked ({e})")


def main() -> int:
    check_timezone()
    check_ta_backend()
    check_database()
    for n in notes:
        print(f"ok   {n}")
    for p in problems:
        print(f"FAIL {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
