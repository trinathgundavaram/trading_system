#!/usr/bin/env python3
"""The ONLY OS-specific process in the system. §47.4 (Phase 3).

WHAT IT IS FOR. The engine - scoring, indicators, scheduler, database,
backtest - runs in a pinned container and produces byte-identical numbers on
macOS, Linux and Windows. That is the property that matters for a system whose
entire output is a score compared against a threshold. But a container cannot
reach a notification centre, cannot hold a power assertion, cannot read the
macOS Keychain and cannot start itself at login. Those four things are what
this file does, natively, on the host.

Roughly 150 lines of OS-specific surface, against ~30,000 lines that no longer
care what operating system they are on. Porting the platform to another OS
means porting THIS FILE - and §43 has already written the alternative
implementations of every branch in it.

IT CONTAINS NO TRADING LOGIC AND MAKES NO DECISIONS. If it dies, the engine
keeps running, keeps trading, and you simply stop getting popups. That is the
correct failure mode and it is worth preserving: never put a decision here.

HOW IT LEARNS ABOUT EVENTS. storage/database.py:log_ui_event() already wrote
to a cross-process outbox with two consumers (scheduler writes, server polls).
This is a third consumer. §47.3 added a transactional pg_notify alongside the
insert, so this listens rather than polls - which removes a one-second latency
floor on the kill-switch alert.

    python3 scripts/tp_agent.py                 # run the agent
    python3 scripts/tp_agent.py --write-env     # materialise .env.runtime, exit
    python3 scripts/tp_agent.py --once          # drain pending events and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import select
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from storage import secrets                                          # noqa: E402  §44
from storage.platform_support import IS_LINUX, IS_MAC, IS_WINDOWS    # noqa: E402  §43

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s tp-agent %(message)s")
log = logging.getLogger("tp-agent")

CHANNEL = "tp_events"

# Which RAW event types deserve an interrupt, and how loud. Everything else in
# the outbox is UI state and stays in the UI - the point of this table is that
# the agent does not become a popup firehose the day someone adds an event.
#
# Deliberately excluded: 'buy_signal' and 'urgent_exit'. Both call sites in
# scheduler.py were migrated in §47.3 and now emit an explicit 'notify' event
# ALONGSIDE the raw one - so listing them here too would fire two popups for
# one event. Anything still in this table is an event type whose call site has
# not been migrated, which is exactly what the table is for.
SEVERITY = {
    "kill_switch_auto": "critical",
    "live_sell": "critical",
    "live_buy": "critical",
    "unmanaged_sell_blocked": "critical",
}


def _esc(s: str) -> str:
    """AppleScript string literals: backslashes first, then quotes."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


# ── 1. Native notifications ─────────────────────────────────────────────────
def notify(title: str, body: str, severity: str = "info") -> None:
    """Best-effort, on whichever OS this is. Never raises: a failed popup must
    not take down the process that also holds the wakelock."""
    try:
        if IS_MAC:
            sound = "Sosumi" if severity == "critical" else "default"
            subprocess.run(["osascript", "-e",
                            f'display notification "{_esc(body)}" '
                            f'with title "{_esc(title)}" sound name "{sound}"'],
                           timeout=5, check=False, capture_output=True)
        elif IS_LINUX:
            urgency = "critical" if severity == "critical" else "normal"
            subprocess.run(["notify-send", "-u", urgency, title, body],
                           timeout=5, check=False)
        elif IS_WINDOWS:
            from win10toast import ToastNotifier

            ToastNotifier().show_toast(title, body, threaded=True)
    except Exception as e:
        log.warning(f"notify failed: {e}")


# ── 2. Keep the machine awake during market hours ───────────────────────────
class Wakelock:
    """A sleeping laptop is a stopped scheduler on every OS.

    Held ONLY while the market is open, deliberately: an unconditional
    wakelock means the machine never sleeps at all, which is a battery and
    thermal cost for no benefit overnight. This is the caffeinate that
    service.sh used to wrap around the scheduler - moved out here because the
    scheduler now runs in a container, where a power assertion has no
    meaning."""

    def __init__(self):
        self.proc = None

    def acquire(self) -> None:
        if self.proc:
            return
        try:
            if IS_WINDOWS:
                import ctypes

                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED: keep the system awake,
                # let the display sleep.
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                self.proc = "win"
            elif IS_MAC:
                self.proc = subprocess.Popen(["caffeinate", "-i"])
            elif IS_LINUX:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle", "--why=trading platform",
                     "sleep", "infinity"])
            if self.proc:
                log.info("wakelock acquired")
        except Exception as e:
            log.warning(f"could not acquire wakelock: {e}")

    def release(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc == "win":
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            else:
                self.proc.terminate()
            log.info("wakelock released")
        except Exception as e:
            log.warning(f"could not release wakelock: {e}")
        finally:
            self.proc = None


def market_open_now() -> bool:
    """Coarse: weekday, 09:00-16:30 New York, matching the scheduler's scan
    window including its pre/post-market buffers. Holidays are not consulted -
    holding a wakelock on Thanksgiving is a trivial cost, and duplicating the
    holiday calendar here would be a second source of truth about market
    hours, which is a much worse problem than a wasted power assertion."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (9, 0) and (now.hour, now.minute) <= (16, 30)


# ── 3. Secrets: host keyring -> container environment ───────────────────────
def write_runtime_env(path: str = ".env.runtime") -> Path:
    """Docker cannot read the macOS Keychain, the Windows Credential Locker or
    a D-Bus Secret Service. Materialise the resolved secrets to a 0600 file
    immediately before `compose up`, and remove it once the containers have
    started - the values live on in the container environment, not on disk.

    TP_HOST_AGENT=1 goes in too: it tells the containerised scheduler that
    this agent exists and it should NOT try to deliver notifications itself
    (see scheduler._notify_via_outbox). Exactly one of the two paths delivers,
    so nobody gets two popups."""
    p = secrets.export_env(REPO / path)
    with open(p, "a") as f:
        f.write("TP_HOST_AGENT=1\n")
    return p


# ── 4. The event loop ───────────────────────────────────────────────────────
def _pg_args() -> dict:
    return {
        "dbname": os.getenv("POSTGRES_DB", "trading_platform"),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "user": secrets.get("POSTGRES_USER", required=False) or os.getenv("USER", ""),
        "password": secrets.get("POSTGRES_PASSWORD", required=False) or "",
    }


def handle(ev: dict, lock: Wakelock) -> None:
    """One event. 'notify' is the explicit, engine-authored form (§47.3); the
    SEVERITY table catches the raw event types that predate it so an alert is
    not lost just because a call site was not migrated."""
    t = ev.get("type")
    p = ev.get("payload", {}) or {}
    if t == "notify":
        notify(p.get("title", "Trading Platform"), p.get("body", ""),
               p.get("severity", "info"))
    elif t in SEVERITY:
        notify(t.replace("_", " ").title(),
               ", ".join(f"{k}={v}" for k, v in p.items()),
               SEVERITY[t])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-env", action="store_true",
                    help="materialise .env.runtime for docker compose, then exit")
    ap.add_argument("--once", action="store_true",
                    help="drain pending notifications and exit (for testing)")
    args = ap.parse_args(argv)

    if args.write_env:
        p = write_runtime_env()
        print(f"wrote {p}")
        return 0

    import psycopg2
    import psycopg2.extensions

    conn = psycopg2.connect(**_pg_args())
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute(f"LISTEN {CHANNEL};")
    lock = Wakelock()
    log.info(f"listening on {CHANNEL}")

    try:
        while True:
            # The 5s wait doubles as the market-hours tick, so one loop covers
            # both jobs without a second thread and without a timer.
            #
            # select() on Windows accepts SOCKETS ONLY, not an arbitrary
            # object with a fileno() - so the elegant version of this loop
            # does not run on the one platform this file exists to support.
            # A plain sleep plus poll() costs at most 5s of latency on a
            # notification and works everywhere.
            if IS_WINDOWS:
                time.sleep(5)
            elif select.select([conn], [], [], 5) == ([], [], []):
                pass
            conn.poll()
            while conn.notifies:
                raw = conn.notifies.pop(0).payload
                try:
                    handle(json.loads(raw), lock)
                except Exception as e:
                    log.warning(f"bad event payload ({e}): {raw[:200]}")
            if market_open_now():
                lock.acquire()
            else:
                lock.release()
            if args.once:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    # A crash here must never be silent: this process is what turns an alert
    # into something a human sees, so its own death has to be visible.
    try:
        sys.exit(main())
    except Exception:
        log.exception("tp-agent died")
        time.sleep(2)  # let the supervisor's throttle interval do its job
        sys.exit(1)
