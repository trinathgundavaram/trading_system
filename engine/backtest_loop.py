"""Automates engine/backtest_engine.py's Stage 1 historical replay on a
weekly cadence, mirroring engine/learning_loop.py's exact pattern (this
codebase's existing convention for "run an expensive analysis periodically
without anyone having to remember to do it by hand").

Call maybe_run_weekly() once per scheduler.py cycle - like learning_loop's
maybe_run(), it's a cheap no-op almost every call (just checks how long it's
been since the last backtest_runs row) and only does real work when
config.yaml's backtest.weekly_trigger_days have elapsed since the last
attempt. scheduler.py is expected to wrap this call in its own background
thread (same "learning-loop-bg" pattern already used for
engine/learning_loop.py) so a multi-minute replay never adds to the reported
live-cycle time.

Results land in the SAME place a manual `python run_backtest.py` run or the
Learning tab's "Run Backtest Now" button would put them - engine/
backtest_engine.py's run_and_persist() is the one shared code path all three
callers use, so there's no separate "scheduled backtest" implementation to
drift from the other two.
"""
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = [
    "VRT", "ORCL", "MU", "FIX", "ASTS", "NFLX",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
]

# engine/backtest_loop.py -> repo root is one level up
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("backtest", {}) or {}


def resolve_backtest_tickers(cfg: dict, db=None) -> list:
    """Single shared ticker-list resolution, called by all three backtest
    callers (server.py's manual-run endpoint, maybe_run_weekly below,
    run_backtest.py's CLI) so "auto" mode behaves identically everywhere -
    same "one code path" convention run_and_persist() already established
    for the run+persist logic itself.

    config.yaml's backtest.ticker_source controls this:
      "static" (default) - uses backtest.tickers verbatim, exactly the
        pre-2026-07-24 behavior.
      "screener_discovered" - ignores backtest.tickers and instead pulls up
        to backtest.max_auto_tickers (default 60) names from the LIVE
        screener_candidates table (storage/database.py's
        get_most_discovered_tickers()), ordered by how often the live
        discovery sources found them worth surfacing - NEVER by how well
        they scored. A backtest universe hand-picked as "tickers that
        already cleared 50%+" would trivially clear 50%+ again over that
        SAME history - that's look-ahead/selection bias dressed up as a
        result, not evidence the strategy has real edge (2026-07-24 review:
        "will that help or create more noise" - it would mostly create
        noise that LOOKS like signal). Ordering by discovery frequency
        instead avoids that: it reflects what the live system finds worth
        re-scanning, which is knowable without knowing the outcome.
      Falls back to backtest.tickers/DEFAULT_TICKERS if db is None or the
      query returns nothing (e.g. a fresh install with no screener history
      yet) - "auto" mode never leaves a backtest with zero tickers.
    """
    bcfg = _cfg(cfg)
    static_tickers = bcfg.get("tickers") or DEFAULT_TICKERS
    if bcfg.get("ticker_source", "static") != "screener_discovered" or db is None:
        return static_tickers
    try:
        max_auto = int(bcfg.get("max_auto_tickers", 60))
        discovered = db.get_most_discovered_tickers(mode="swing", limit=max_auto)
    except Exception as e:
        logger.warning(f"screener_discovered ticker resolution failed, falling back to the static list: {e}")
        discovered = []
    return discovered or static_tickers


def spawn_backtest_subprocess(tickers: list, start: str, end: str, warmup_days: int,
                               max_hold_days: int, triggered_by: str) -> subprocess.Popen:
    """Launches run_backtest.py as its OWN OS process rather than calling
    engine.backtest_engine.run_and_persist() in-process.

    Why a subprocess and not a thread (this used to be a plain in-process
    call, both here and in server.py's manual-run endpoint): the replay
    recomputes indicators over a growing window on every simulated day for
    every ticker - real CPU work, easily minutes for a dozen+ tickers over a
    year. Running that on a background thread inside server.py's own process
    doesn't block the asyncio event loop directly, but it DOES hold the GIL
    for long stretches, and every other concurrent request (Signals tab,
    etc.) needs that same GIL to do its own work - hence "a few other tabs
    take very long to load" while a backtest is running. Running scheduler.py's
    weekly auto-trigger in-process has the same effect on its own live
    trading cycle. A subprocess has its own interpreter and its own GIL, so
    a multi-minute replay no longer competes with anything else in the
    caller's process. It's also why engine/backtest_engine.py's
    _patch_database()/_patch_market_breadth() monkeypatches (which reassign
    storage.database.Database / engine.market_breadth for the whole process
    they run in) are now fully contained to the child process instead of
    theoretically racing anything concurrent in the parent.

    Returns immediately (does not wait for the child to finish) - the
    backtest_runs DB row (written by the child near-immediately after
    startup) is the cross-process source of truth for "is one running right
    now", not this Popen handle.
    """
    args = [
        sys.executable, "run_backtest.py",
        "--tickers", *tickers,
        "--start", start, "--end", end,
        "--warmup-days", str(warmup_days),
        "--max-hold-days", str(max_hold_days),
        "--triggered-by", triggered_by,
    ]
    log_dir = _REPO_ROOT / "output" / "backtest_results" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{triggered_by}.log"
    logger.info(f"Spawning backtest subprocess ({triggered_by}): {' '.join(args)} (log: {log_path})")
    with open(log_path, "w") as logf:
        return subprocess.Popen(args, cwd=str(_REPO_ROOT), stdout=logf, stderr=subprocess.STDOUT)


def kill_running_backtest(reason: str = "user_abort") -> bool:
    """HARD kill switch for server.py's POST /api/backtest/abort (2026-07-27,
    Trinath: a manual backtest had been running for a long time and there was
    no way to stop it short of finding the PID by hand in Terminal - the
    scheduler's own runaway cycles already get this via
    engine/cycle_supervisor.py's /api/cycle/cancel, the backtest subprocess
    never did).

    Reads the running backtest_runs row's pid - recorded by
    Database.log_backtest_run_start() via os.getpid(), so this works no
    matter which of the three paths (weekly auto-trigger, the Learning tab's
    "Run backtest now" button, or a bare `python run_backtest.py` from the
    CLI) started it - and kills that whole process group via
    cycle_supervisor's portable kill primitive (same SIGTERM -> grace ->
    SIGKILL, POSIX process-group / Windows psutil-tree behavior already
    proven out for the scan-cycle kill switch, not a second implementation of
    the same thing). Marks the run 'failed' afterward so the Learning tab
    shows why it stopped and the next run isn't blocked by a stale 'running'
    row. Returns True if it cleared a run (killed a live one, OR found the
    'running' row already had no process behind it - reap_stale_backtest_run()
    handles that second case so clicking Abort on an already-dead run still
    reports success instead of a confusing "nothing to abort"), False if
    nothing was running at all."""
    from storage.database import Database
    db = Database()
    if db.reap_stale_backtest_run():
        return True  # row was already stale (no live process) - reaped, done
    run = db.get_running_backtest_run()
    pid = run.get("pid") if run else None
    if not run or not pid:
        return False
    from engine.cycle_supervisor import _kill_process_tree
    _kill_process_tree(int(pid), reason=reason)
    db.log_backtest_run_failed(run["id"], error=f"Aborted by user ({reason})")
    return True


def maybe_run_weekly(db, cfg: dict) -> dict | None:
    """Returns {"spawned": True, "pid": ...} if a run was kicked off this
    call, else None (trigger condition not met, or backtest.enabled is
    False). The run itself happens in a detached subprocess (see
    spawn_backtest_subprocess's docstring) - this function no longer waits
    for it to finish, so it can't return the run's summary the way it used
    to. Check backtest_runs (get_last_backtest_run/get_recent_backtest_runs)
    for results once status flips from 'running' to 'completed'."""
    bcfg = _cfg(cfg)
    if not bcfg.get("enabled", True):
        return None

    trigger_days = bcfg.get("weekly_trigger_days", 7)
    last_run = db.get_last_backtest_run()

    if last_run is not None and last_run.get("status") == "running":
        # A previous run is still in flight (or got stuck) - never start a
        # second one concurrently. It'll be picked up again next cycle once
        # that row moves to completed/failed.
        return None

    reason = _check_trigger(last_run, trigger_days)
    if reason is None:
        return None

    logger.info(f"Weekly backtest trigger: {reason}")

    tickers = resolve_backtest_tickers(cfg, db)
    months = bcfg.get("months", 12)
    warmup_days = bcfg.get("warmup_days", 260)
    max_hold_days = bcfg.get("max_hold_days", 20)
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=int(months * 30.44))).isoformat()

    proc = spawn_backtest_subprocess(
        tickers, start, end, warmup_days, max_hold_days, triggered_by="weekly_auto",
    )
    return {"spawned": True, "pid": proc.pid}


def _check_trigger(last_run, trigger_days: int) -> str | None:
    if last_run is None:
        return "first backtest run ever - no prior backtest_runs row"

    ref_ts = last_run.get("completed_at") or last_run.get("started_at")
    try:
        last_at = datetime.fromisoformat(ref_ts)
        days_since = (datetime.utcnow() - last_at).total_seconds() / 86400
    except (ValueError, TypeError):
        # Can't parse the timestamp - force a run rather than getting
        # permanently stuck on a malformed row.
        return "couldn't parse last run's timestamp - forcing a run"

    if days_since >= trigger_days:
        return f"{days_since:.1f} days since last run >= {trigger_days} threshold"
    return None
