"""Hard, OS-level wall-clock ceiling for a scan cycle (2026-07-22, Trinath:
"any process or cycle, both scheduled and manual, has to be completed within
15 min... any hang or hold up has to be auto killed... cancel run should be
able to do all this as well").

BACKGROUND - the 2026-07-22 incident this fixes: scheduler.py's cron never
fired a single scheduled cycle all day (watchdog logged "Scheduled cycle
appears MISSED... cron scheduler may be stuck/dead" continuously from 08:30
onward). Root cause: run_cycle() used to run the entire cycle body
(_run_cycle_impl) INLINE in the same process/thread APScheduler's executor
was using. trading.max_cycle_duration_minutes already existed as a budget,
but it's COOPERATIVE - it only stops NEW ticker analysis from starting once
spent; an already-in-flight MCP call that's truly wedged keeps its thread
forever (production logs show mcp_clients/base.py's own internal asyncio
timeout failing to actually cancel such a call: "run_async: hard 40s ceiling
hit - an inner MCP timeout was defeated (task wouldn't cancel cleanly)").
Python has no safe way to force-kill a thread, so that thread - and the
APScheduler job instance it belonged to - never completed. With
max_instances=1, every subsequent cron tick for the same job id was silently
coalesced away because APScheduler still considered the previous instance
"running", forever.

THE FIX: run the actual cycle body in a CHILD PROCESS, in its own process
group (start_new_session=True) so every descendant it spawns (uvx yfmcp,
npx stock-scanner, the maverick HTTP client, etc.) dies with it. The parent
just waits up to a hard timeout; if the child hasn't finished by then,
SIGTERM the whole group, give it a few seconds, then SIGKILL. Either way
run_supervised() ALWAYS returns within timeout_seconds + a small grace
window - no exceptions. Because scheduler.py's run_cycle() (the function
APScheduler's cron job actually calls) now delegates here instead of running
inline, that function is now ALSO guaranteed to return within the same
bound, which is what keeps the cron scheduler itself from ever being wedged
again: the executor slot frees up on time, so the next scheduled tick fires
as designed instead of being coalesced away.

Also used by server.py's manual /api/cycle/run_now (identical protection,
since it too just calls scheduler.py's run_cycle()) and by
/api/cycle/cancel, which now hard-kills the SAME child pid directly via
kill_current_cycle() below instead of only setting a cooperative flag."""
import logging
import os
import signal
import subprocess
import sys
import time

from storage.database import Database

logger = logging.getLogger(__name__)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULER_PATH = os.path.join(ROOT_DIR, "scheduler.py")

TERM_GRACE_SECONDS = 5  # SIGTERM, then this long to exit cleanly, before SIGKILL
REAP_GRACE_SECONDS = 8  # how long to wait for the OS to actually reap after SIGKILL


def _kill_process_group(pid: int, *, reason: str) -> None:
    """Best-effort SIGTERM -> grace -> SIGKILL of an entire process group.
    Safe to call on a pid that's already dead or was never a group leader
    (ProcessLookupError swallowed either way) - the 15-min auto-kill and a
    user-triggered /api/cycle/cancel can legitimately race to kill the same
    pid, and that must never raise."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    logger.warning(f"cycle_supervisor: sent SIGTERM to cycle process group {pgid} (pid {pid}) - {reason}")
    deadline = time.time() + TERM_GRACE_SECONDS
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)  # signal 0 = alive-check only, raises once the group is gone
        except ProcessLookupError:
            return
        time.sleep(0.3)
    try:
        os.killpg(pgid, signal.SIGKILL)
        logger.warning(f"cycle_supervisor: process group {pgid} (pid {pid}) still alive after "
                        f"{TERM_GRACE_SECONDS}s grace - sent SIGKILL")
    except ProcessLookupError:
        pass


def kill_current_cycle(reason: str = "user_cancel") -> bool:
    """Immediate hard kill for server.py's /api/cycle/cancel (2026-07-22:
    Trinath wants Cancel to do everything the 15-min auto-kill does, not the
    old cooperative "let in-flight tickers finish" behavior). Reads the
    running cycle's child pid straight out of cycle_status - a cross-process
    handle, so this works identically whether the cycle was started by
    scheduler.py's cron or server.py's manual run_now. Returns True if a live
    cycle was found and killed, False if nothing was running."""
    db = Database()
    status = db.get_cycle_status()
    pid = status.get("pid")
    if not status.get("is_running") or not pid:
        return False
    pid = int(pid)
    # Record the kill BEFORE actually signaling anything (2026-07-22, found
    # by test_cycle_supervisor.py: writing this AFTER _kill_process_group()
    # raced against the owning run_supervised() call's own finally block -
    # if the child happened to die and that thread's set_cycle_finished()
    # ran first, mark_cycle_killed()'s pid-guard would then see pid already
    # cleared and silently no-op, so the cycle looked like a normal finish
    # instead of a user cancel). set_cycle_finished() deliberately never
    # touches kill_reason, so recording it now - before the actual kill
    # signal even goes out - makes the outcome deterministic regardless of
    # which thread's cleanup wins that race, and gives the UI/DB an
    # immediate "not running" the instant Cancel is clicked rather than
    # waiting on the supervisor thread's own wait() to notice.
    db.mark_cycle_killed(expected_pid=pid, reason=reason)
    _kill_process_group(pid, reason=reason)
    return True


def run_supervised(force: bool, timeout_seconds: float) -> None:
    """Spawns `python3 scheduler.py --run-once [--force]` as the cycle's own
    child process, records its pid in cycle_status, and blocks until the
    child exits or timeout_seconds elapses - whichever comes first. This
    call IS the long-running part (same as the old inline run_cycle() was);
    callers already run it off whatever thread shouldn't be blocked (an
    APScheduler executor thread, or server.py's BackgroundTasks thread), so
    blocking here is expected and fine.

    On timeout: hard-kills the child's entire process group (every MCP
    subprocess it spawned included) and marks the cycle finished with
    kill_reason='timeout_15min' so the UI/logs show what actually happened
    instead of it looking like a normal, silent finish. Either way, this
    function is guaranteed to return within roughly
    timeout_seconds + TERM_GRACE_SECONDS + REAP_GRACE_SECONDS."""
    db = Database()
    triggered_by = "manual" if force else "scheduler"

    if db.clear_stale_cycle(max_age_minutes=max(1, timeout_seconds / 60)):
        logger.warning("cycle_supervisor: cleared a stale cycle_status row from a previous "
                        "process lifetime before starting this cycle")

    db.set_cycle_running(triggered_by)

    cmd = [sys.executable, SCHEDULER_PATH, "--run-once"]
    if force:
        cmd.append("--force")

    proc = None
    try:
        # start_new_session=True makes this child (and every grandchild MCP
        # subprocess it spawns - uvx, npx, etc.) its own process group, which
        # is what lets os.killpg() below take the WHOLE tree down atomically
        # instead of just the immediate child.
        proc = subprocess.Popen(cmd, cwd=ROOT_DIR, start_new_session=True)
        db.set_cycle_pid(proc.pid)
        logger.info(f"cycle_supervisor: spawned cycle child pid={proc.pid} "
                    f"(triggered_by={triggered_by}, hard ceiling {timeout_seconds / 60:.1f} min)")
        proc.wait(timeout=timeout_seconds)
        if proc.returncode:
            logger.warning(f"cycle_supervisor: cycle child pid={proc.pid} exited with non-zero "
                            f"code {proc.returncode} (ran to completion within the time limit "
                            f"regardless - see output/logs/scheduler.log for the actual error)")
    except subprocess.TimeoutExpired:
        logger.error(f"cycle_supervisor: cycle child pid={proc.pid} exceeded the "
                     f"{timeout_seconds / 60:.1f} min hard limit - force-killing the whole cycle "
                     f"(process group, every MCP subprocess it spawned included) and clearing "
                     f"state so the next cycle isn't blocked.")
        _kill_process_group(proc.pid, reason=f"exceeded {timeout_seconds / 60:.1f} min hard limit")
        try:
            proc.wait(timeout=REAP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.error(f"cycle_supervisor: pid={proc.pid} did not reap even after SIGKILL "
                         f"(possible zombie) - resetting cycle state anyway.")
        db.mark_cycle_killed(expected_pid=proc.pid, reason="timeout_15min")
        return
    except Exception as e:
        logger.error(f"cycle_supervisor: failed to run cycle child: {e}", exc_info=True)
    finally:
        # Whatever happened above, this cycle is over from the supervisor's
        # point of view - always leave is_running=0 so it can never get
        # stuck true even if something above raised unexpectedly. Idempotent
        # with the timeout path's mark_cycle_killed() above (doesn't touch
        # kill_reason, so a real kill reason is never erased by this).
        db.set_cycle_finished()
