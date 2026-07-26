"""Market-hours-aware scheduler loop - fully synchronous. Every MCP call inside
engine/market_context.py and engine/ticker_analyzer.py already bridges into
asyncio internally (see mcp/base.py's run_async), so this file never touches
asyncio directly. Zero Claude calls, zero API keys - see README.md."""
import logging
import os
import sys
import threading
import time
from datetime import datetime

import pytz
import yaml

from engine.backtest_loop import maybe_run_weekly as maybe_run_weekly_backtest
from engine.learning_loop import maybe_run as maybe_run_learning_loop
from engine.learning_loop import maybe_run_threshold_regret
from engine.market_context import MarketContext, evaluate_market_gate
# §47.3: the cycle body no longer calls notify_buy_signal/notify_urgent_exit.
# It writes to the ui_events outbox via _notify_via_outbox() and lets
# scripts/tp_agent.py do the OS-specific part - which is what allows the
# engine to run in a container that cannot reach a notification centre. The
# fallback path imports engine.notifications.notify lazily, inside that
# helper, so there is nothing to import at module scope any more.
from engine.packet_builder import build_trade_prompt, build_ticker_packet
from engine.pattern_features import build_pattern_features
from engine.position_management import run_loop_b
from engine import paper_trader
from engine import live_trader
from engine.regime_engine import calculate as calc_regime, current_state as get_regime
from engine.ticker_analyzer import TickerAnalyzer
from rules.common import exit_kind_for_loop_b_label   # §D
from engine.ticker_data_adapter import ticker_to_dict, market_to_dict
from learning.pattern_database import PatternDatabase
from rules import swing_buy_rules
from rules.hard_vetoes import check as check_vetoes
from rules.market_filters import evaluate as evaluate_market_score
from rules.risk_rules import RiskEngine
from rules.sell_rules import SellRulesEngine
from storage.database import Database
from storage.log_setup import setup_logging

setup_logging("scheduler")
logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# NYSE holidays - VERIFY/UPDATE EACH YEAR. Good Friday has no fixed date and
# must be recomputed from Easter each year.
NYSE_HOLIDAYS_2026 = [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# §38.2: runtime data is no longer derived from this module's location, so a
# per-version worktree can point at its own data directory via TP_OUTPUT_DIR.
# Unset, these resolve to <repo>/output exactly as before.
from storage.paths import output_dir, pending_dir

OUTPUT_DIR = str(output_dir())
PENDING_DIR = str(pending_dir())

db = Database()
analyzer = TickerAnalyzer()
sell_engine = SellRulesEngine()
pattern_db = PatternDatabase(db)
cycle_count = 0


def load_config() -> dict:
    """Reads config.yaml fresh every call - this is what makes it hot-reloadable
    without restarting the process."""
    config_path = os.path.join(BASE_DIR, "config.yaml")
    with open(config_path) as f:
        return yaml.safe_load(f)


def _notify_via_outbox(cfg: dict, *, severity: str, title: str, body: str,
                       threshold_pct: float | None = None) -> None:
    """Emit a user-facing notification through the ui_events outbox (§47.3),
    and - only when no host agent is running - deliver it directly as well.

    THE SPLIT THIS IMPLEMENTS. The engine's job is to say what happened and
    how urgent it is. Deciding how a human finds out is the host agent's job,
    because it is the only OS-specific part of the system and the only process
    with a route to a notification centre. Under §47 the engine runs in a
    container, where osascript and notify-send simply do not exist.

    THE FALLBACK IS NOT OPTIONAL. Someone running natively today, with no
    agent installed, must not silently stop getting notifications the moment
    this ships - that would be a regression dressed as an architecture. So if
    TP_HOST_AGENT is not set, this also calls engine/notifications.py directly.
    The agent sets TP_HOST_AGENT=1 in the container environment it writes, so
    exactly one of the two paths delivers and nobody gets two popups.
    """
    # The high_conviction_buy_pct gate is checked FIRST, before the outbox
    # write. It used to live inside notify_buy_signal(); moving the call site
    # here moved the gate too, and putting it after the write would mean every
    # buy signal pops once a host agent is attached - the gate would silently
    # apply only to the fallback path, which is the opposite of the intent.
    if threshold_pct is not None:
        threshold = (cfg.get("notifications", {}) or {}).get("high_conviction_buy_pct", 80)
        if threshold_pct < threshold:
            return

    payload = {"severity": severity, "title": title, "body": body}
    try:
        db.log_ui_event("notify", payload)
    except Exception as e:
        logger.warning(f"could not write notify event to the outbox: {e}")

    if os.getenv("TP_HOST_AGENT", "").strip() in ("1", "true", "yes"):
        return

    try:
        from engine.notifications import notify

        notify(cfg, title=title, message=body, subtitle="Trading Platform",
               severity=severity)
    except Exception as e:
        logger.warning(f"direct notification failed: {e}")


_CLOCK_SKEW_CACHE = {"at": 0.0, "value": None}
_CLOCK_SKEW_TTL_SECONDS = 300


def _clock_skew_seconds(timeout: float = 3.0, _force: bool = False) -> float | None:
    """How far the local clock is from the real world, in seconds. None if
    unknown (no network) - which is NOT treated as a failure.

    §42.4 (Phase 3). Under the containerised architecture this stops being a
    nicety. Docker Desktop on macOS and Windows runs containers inside a VM
    whose clock can lag the host badly after the laptop sleeps, and this
    process decides whether the market is open and when a stop was hit. A
    container that wakes up believing it is 14:05 when it is really 16:40 will
    happily place orders into a closed market and mis-time every stop - and
    that is very close in shape to the 22 July incident, so the failure would
    be hunted in the wrong place.

    Compares the local clock against an HTTP Date header rather than running
    NTP: no client needed in the image, no privileged port, no daemon, and one
    cheap request. Cached for five minutes because this runs at the top of
    every cycle and the answer does not change quickly.

    A None result (no network, blocked egress, DNS down) means "cannot tell",
    and the caller proceeds. Refusing to trade because the health *check*
    failed would convert a network blip into a trading outage, which is a
    worse trade than the risk it guards against - and the market-data calls
    downstream will fail loudly on their own if the network is genuinely gone.
    """
    import email.utils
    import urllib.request
    from datetime import timezone

    now_mono = time.time()
    if not _force and now_mono - _CLOCK_SKEW_CACHE["at"] < _CLOCK_SKEW_TTL_SECONDS:
        return _CLOCK_SKEW_CACHE["value"]

    value = None
    for url in ("https://www.google.com", "https://www.cloudflare.com"):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                served = email.utils.parsedate_to_datetime(r.headers["Date"])
            value = abs((datetime.now(timezone.utc) - served).total_seconds())
            break
        except Exception:
            continue

    _CLOCK_SKEW_CACHE.update({"at": now_mono, "value": value})
    if value is None:
        logger.debug("clock skew unknown - no reference host reachable")
    return value


def is_market_open(cfg: dict) -> bool:
    """Despite the name, this is the SCAN window, not strictly regular
    trading hours: market_open_buffer_minutes/market_close_buffer_minutes
    now EXTEND the window outward (premarket before the 9:30 ET open,
    postmarket after the 16:00 ET close) rather than narrowing it - Trinath
    wants premarket and postmarket tickers analyzed too, not just RTH. With
    the default 30/30, the window is 9:00-16:30 ET (8:00-15:30 CST - CST/CDT
    stays exactly 1hr behind ET/EDT year-round, so no separate CT math is
    needed here). Real order placement never happens from Python regardless
    (see README) and Robinhood wouldn't accept a plain equity order pre/post
    market anyway, so widening this window only affects ANALYSIS - quotes
    fetched pre/post market are legitimately pre/post-market prices, not a
    bug (same honesty note run_cycle()'s force=True docstring already made
    for the outside-hours override case, now also true for the normal
    scheduled pre/post-market window)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in NYSE_HOLIDAYS_2026:
        return False

    from datetime import timedelta
    open_buf = cfg["trading"]["market_open_buffer_minutes"]
    close_buf = cfg["trading"]["market_close_buffer_minutes"]
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0) - timedelta(minutes=open_buf)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0) + timedelta(minutes=close_buf)
    return market_open <= now <= market_close


def run_cycle(force: bool = False):
    """Entry point BOTH scheduler.py's own cron-triggered runs (this process)
    and server.py's manual /api/cycle/run_now (a SEPARATE process) call -
    this is the one choke point that makes any fix here apply to every
    trigger path at once (see server.py's _manual_cycle_lock docstring for
    why an in-memory flag alone can't cover the cross-process case).

    2026-07-22 (Trinath: the cron scheduler silently stopped firing for 5+
    hours straight - see the 2026-07-22 incident notes in README.md):
    delegates to engine/cycle_supervisor.run_supervised(), which now runs
    the actual cycle body (_run_cycle_impl) in a SEPARATE, killable CHILD
    PROCESS instead of inline in this one. This fixes two things at once:

    1. A genuinely wedged MCP call (confirmed possible in production -
       mcp_clients/base.py's run_async docstring documents its own internal
       timeout failing to actually cancel one: "task wouldn't cancel
       cleanly") can no longer occupy this process/thread forever - Python
       has no safe way to force-kill a thread, but the whole child process
       (and every subprocess IT spawned) gets SIGKILLed at a hard ceiling
       either way.
    2. Because run_cycle() - the exact function APScheduler's cron job
       calls - is now guaranteed to return within trading.hard_kill_minutes
       (default 15) no matter what happens inside the cycle, the
       BlockingScheduler's max_instances=1 job slot can never be wedged past
       that ceiling. That's what was actually broken on 2026-07-22: with the
       old inline design, one truly-stuck cycle meant APScheduler considered
       the job "still running" forever, silently coalescing away every
       subsequent 5-minute tick with nothing in the log to explain why.

    db.clear_stale_cycle() / set_cycle_running() / set_cycle_finished() now
    all live inside run_supervised() itself (see that module) - kept out of
    this thin wrapper so there's exactly one place that owns the
    running/finished bookkeeping around the child process's lifetime."""
    from engine.cycle_supervisor import run_supervised
    cfg = load_config()
    timeout_minutes = float(cfg.get("trading", {}).get("hard_kill_minutes", 15))
    run_supervised(force=force, timeout_seconds=max(60.0, timeout_minutes * 60))


# ── Screener throttle + background refresh (2026-07-16, cycle-overrun fix) ──
# Evidence: with HYBRID's 5-min cadence, run_screener() was re-running full
# discovery + quality gate + discovery-scoring EVERY cycle (~60-90s of a
# 130-400s cycle in production logs) even though the candidate set barely
# changes in 5 minutes - the sources themselves cache 2-4h. The candidate
# LIST is now cached for screener.refresh_minutes (default 15); when it goes
# stale, a refresh runs in a BACKGROUND daemon thread while the current cycle
# proceeds on the previous list, so screener cost is amortized to at most
# once per refresh window and NEVER blocks a scan cycle after the first.
# First cycle after process start has no list yet and runs it inline (there's
# nothing to trade against otherwise). Thread-safety: same per-call-connection
# Database + locked TTLCache guarantees the learning-loop background thread
# already relies on.
_screener_cache = {"tickers": [], "refreshed_at": 0.0, "has_run": False}
_screener_lock = threading.Lock()
_screener_refreshing = threading.Event()


def _run_screener_now(cfg: dict, trading_mode: str, regime) -> list:
    from engine.screener import run_screener
    screener_result = run_screener(cfg, mode=trading_mode, regime=regime)
    tickers = [c.ticker for c in screener_result.candidates
               if c.source != "manual_watchlist"]
    logger.info(f"Screener: {len(tickers)} extra candidates "
                f"[sources: {', '.join(screener_result.sources_used)}]")
    with _screener_lock:
        _screener_cache.update(tickers=tickers, refreshed_at=time.time(), has_run=True)
    return tickers


def _get_screener_tickers(cfg: dict, trading_mode: str, regime) -> list:
    """Cached screener candidates; kicks a background refresh when stale.
    refresh_minutes defaults to 15 - i.e. SWING mode behaves exactly as
    before (one screener run per 15-min cycle), while DAY/HYBRID's 5-min
    cycles reuse the list twice between refreshes instead of paying the
    full screener cost every cycle."""
    refresh_minutes = float(cfg.get("screener", {}).get("refresh_minutes", 15))
    with _screener_lock:
        age = time.time() - _screener_cache["refreshed_at"]
        fresh = _screener_cache["has_run"] and age < refresh_minutes * 60
        tickers = list(_screener_cache["tickers"])
        has_run = _screener_cache["has_run"]

    if fresh:
        return tickers

    if not has_run:
        # Cold start - nothing cached to scan against; run inline once.
        db.set_cycle_stage("screener")
        return _run_screener_now(cfg, trading_mode, regime)

    # Stale - refresh in the background (skip if one is already in flight so
    # slow refreshes can't pile up), and scan on the previous list this cycle.
    if not _screener_refreshing.is_set():
        _screener_refreshing.set()

        def _refresh():
            try:
                _run_screener_now(cfg, trading_mode, regime)
            except Exception as e:
                logger.error(f"Background screener refresh failed: {e}", exc_info=True)
            finally:
                _screener_refreshing.clear()

        threading.Thread(target=_refresh, daemon=True, name="screener-refresh-bg").start()
        logger.info(f"Screener list is {age / 60:.1f}min old - background refresh started, "
                    f"scanning on previous {len(tickers)} candidates this cycle")
    return tickers


def _run_cycle_impl(force: bool = False):
    """force=True skips the is_market_open() gate - used by server.py's
    /api/cycle/run_now for an on-demand run outside the normal Mon-Fri
    9:00-16:30 ET (8:00-15:30 CST) scan schedule (e.g. testing, or catching
    up when it's a weekend/holiday and you don't want to wait). That
    schedule already covers premarket (9:00-9:30 ET) and postmarket
    (16:00-16:30 ET) via config.yaml's market_open_buffer_minutes/
    market_close_buffer_minutes - see is_market_open()'s docstring. Kill
    switch and risk limits are NOT bypassed by force - those stay
    safety-critical regardless of how the cycle was triggered. Quotes
    fetched outside even that widened window (nights/weekends) will be
    stale/after-hours prices, not live - that's inherent to the market being
    closed, not a bug in this override."""
    global cycle_count
    cycle_count += 1
    start = time.time()
    triggered_by = "manual" if force else "scheduler"
    logger.info(f"Cycle #{cycle_count} started ({triggered_by})")

    cfg = load_config()

    # §42.4 (Phase 3). Before ANY market-hours reasoning, check the clock.
    # This gate is deliberately above is_market_open(): every decision below
    # it - whether the market is open, whether a stop was hit and when, how
    # long a position has been held - is a function of the local clock, and a
    # wrong clock corrupts all of them silently rather than loudly.
    skew = _clock_skew_seconds()
    max_skew = float(cfg.get("trading", {}).get("max_clock_skew_seconds", 120))
    if skew is not None and skew > max_skew:
        msg = (f"CLOCK SKEW {skew:.0f}s (limit {max_skew:.0f}s) - refusing to trade. "
               f"Market-hours and stop timing cannot be trusted. Restart the "
               f"container/VM, or check NTP on this host.")
        logger.error(msg)
        try:
            db.log("CRITICAL", f"cycle aborted: clock skew {skew:.0f}s")
        except Exception:
            pass
        db.log_cycle(cycle_count, 0, blocked=True, reason=f"clock_skew_{skew:.0f}s",
                     triggered_by=triggered_by)
        return

    if not force and not is_market_open(cfg):
        logger.info("Market closed - skipping")
        return
    if force and not is_market_open(cfg):
        logger.warning("Manual cycle run - market is CLOSED, quotes will be stale/after-hours")

    if cfg["risk"]["kill_switch_triggered"]:
        logger.warning("KILL SWITCH ACTIVE - halted")
        return

    # §10 (Phase 2): book-aware, and the block is now RECORDED.
    #
    # RiskEngine(db, cfg) defaults to the book this mode actually trades (§7),
    # but state it explicitly here - which book a cycle was budgeted against is
    # exactly the sort of thing that should not be inferred from a default two
    # modules away.
    #
    # This is a cheap early exit, NOT the real gate. It runs once, before the
    # ticker loop, so a cycle that starts at 9 trades and finds 15 qualifying
    # candidates would still place all 15. The binding check is per-trade,
    # inside paper_trader.execute_buy() - see §10.
    watch_mode = paper_trader.is_watch_mode(cfg)
    risk_check = RiskEngine(db, cfg, simulated=watch_mode).check()
    if not risk_check["can_trade"]:
        logger.warning(f"Risk limit: {risk_check['reason']}")
        # Every other blocking path in this function logs the block; this one
        # returned silently, so a cycle halted by a risk limit vanished from
        # the cycles table and the Journal tab entirely. "Why did nothing
        # happen for three hours?" had no recorded answer.
        db.log_cycle(cycle_count, 0, blocked=True, reason=risk_check["reason"],
                     triggered_by=triggered_by)
        return

    # Layer 1: Market context via MCPs
    logger.info("Fetching market context...")
    db.set_cycle_stage("market_context")
    mkt = MarketContext().fetch()
    can_trade, reason = evaluate_market_gate(mkt, cfg)
    if not can_trade:
        logger.warning(f"Market gate BLOCKED: {reason}")
        db.log_cycle(cycle_count, 0, blocked=True, reason=reason, triggered_by=triggered_by)
        return

    logger.info(f"Market gate OPEN - F&G={mkt.fear_greed_score}, VIX={mkt.vix_level:.1f}")

    regime, market_dict = _calc_regime_and_market_dict(mkt, cfg)

    # --- Phase 1 market score gate (additional to evaluate_market_gate above -
    # that one is the coarse kill-switch/F&G/VIX/blackout gate; this one is the
    # 0-100 scored gate + crisis/breadth hard veto from rules/market_filters.py) ---
    mkt_gate = evaluate_market_score(market_dict, cfg)
    if not mkt_gate.can_trade:
        logger.warning(f"Market score gate BLOCKED: {mkt_gate.reason}")
        db.log_cycle(cycle_count, 0, blocked=True, reason=mkt_gate.reason, triggered_by=triggered_by)
        return
    logger.info(f"Market score gate OPEN - {mkt_gate.reason}")

    trading_mode = cfg["trading"].get("mode", "SWING").lower()

    # ── Paper trading account seeding (engine/paper_trader.py) ── seed the
    # purse + clone the real book on the very first cycle ever run; no-op
    # afterwards (idempotent - see ensure_seeded's docstring). Runs every
    # cycle regardless of watch/execute mode (2026-07-24, Trinath: the paper
    # book must exist and keep being tracked no matter which mode real
    # trading is in) so a deploy that starts life in EXECUTE mode still gets
    # a paper purse to compare against. Actual simulated buys/sells happen
    # inside _evaluate_ticker() (Loop A signals - NEW paper buys still only
    # happen in WATCH mode, see `if watch_mode:` there) and after Loop B
    # below (urgent exits, now mode-independent for the paper book too).
    try:
        paper_trader.ensure_seeded(db, cfg)
    except Exception as e:
        logger.error(f"Paper account seeding failed: {e}", exc_info=True)

    # ── Robinhood read-only health probe (2026-07-17, Akhil: "added
    # credentials but nothing happens") ── nothing in the scan cycle ever
    # touched the robinhood MCP (only robinhood_sync.py and the Monitor tab's
    # status endpoint use it), so its Data Sources row stayed NO_DATA_YET
    # forever even though the panel promises "will appear after the next
    # cycle touches it". One portfolio read per cycle keeps that promise:
    # 60s-cached, breaker-guarded, instant no-op without credentials. A
    # failure here is itself the useful output - it lands in the source
    # health table with the real error (e.g. the first-ever login exceeding
    # the 30s MCP timeout - see mcp_clients/robinhood_mcp.py's warm-up note).
    try:
        from mcp_clients.robinhood_mcp import get_client as _rh_client
        _rh = _rh_client()
        if _rh.configured():
            _pf = _rh.get_portfolio()
            if _pf:
                logger.info("robinhood: read-only account link OK")
            else:
                logger.warning("robinhood: credentials set but portfolio read "
                               "returned nothing - see Monitor tab's Data "
                               "Sources row for the recorded error")
    except Exception as e:
        logger.warning(f"robinhood health probe failed (non-fatal): {e}")

    # ── Robinhood account auto-sync (engine/account_sync.py, 2026-07-17) ──
    # When account.auto_sync is ON, pulls the CONFIGURED account's real
    # holdings (account.robinhood_account_number - your Agentic account)
    # into the local real book so Loop B analyzes them every cycle. Import-
    # only, never auto-closes; throttled internally to one sync per 15 min.
    try:
        from engine import account_sync
        account_sync.run(db, cfg)
    except Exception as e:
        logger.warning(f"account sync failed (non-fatal): {e}")

    # ── Auto-discovery screener (engine/screener.py) ── OFF by default (see
    # config.yaml's screener.enabled) - when on, adds candidates to the
    # per-ticker loop below alongside the manual watchlist. See
    # engine/screener.py's module docstring for exactly which sources are
    # backed by verified-real MCP tool calls vs. not-yet-implemented.
    screener_tickers = []
    if cfg.get("screener", {}).get("enabled", False):
        try:
            screener_tickers = _get_screener_tickers(cfg, trading_mode, regime)
        except Exception as e:
            logger.error(f"Screener failed: {e}", exc_info=True)

    # Layer 2-3: Per-ticker analysis - PARALLELIZED (2026-07-14 perf pass).
    # Each _evaluate_ticker() call is I/O-bound (MCP subprocess/network round
    # trips dominate its wall time, not CPU), so running several at once via a
    # bounded ThreadPoolExecutor cuts total cycle time roughly by the worker
    # count instead of scanning tickers one at a time with a fixed 0.3s sleep
    # between each (the old sequential loop below this comment, for a ~20-
    # ticker watchlist+screener mix, is what produced the ~5min cycles
    # Trinath reported - almost entirely time.sleep(0.3)*N plus N sequential
    # MCP round trips rather than actual compute).
    #
    # Thread-safety this relies on: storage/database.py's Database opens a
    # fresh sqlite3 connection per call guarded by its own threading.Lock
    # (safe to call from many threads); engine/cache.py's TTLCache now has
    # its own lock too (see that file's 2026-07-14 note); ticker_data_cache
    # below is written with a distinct key per ticker from each thread, which
    # is safe under the GIL even without an explicit lock (no two threads
    # ever write the SAME key). Bounded by trading.cycle_max_parallel_tickers
    # (default 8) so we don't fire off dozens of concurrent `uvx yfmcp@latest`
    # subprocesses and hammer Yahoo's API / local CPU - see config.yaml.
    packets = []
    ticker_data_cache = {}
    # ── Held-position exit coverage (2026-07-16, critical fix) ── the hard
    # stop-loss / take-profit / trailing-stop checks live in rules/
    # sell_rules.py, which only runs inside _evaluate_ticker() - i.e. only
    # for tickers on THIS cycle's scan list. A paper position whose ticker
    # came from the screener and then stopped being resurfaced was therefore
    # NEVER sell-evaluated: production evidence showed ERAS sitting 5.6%
    # below entry (past the -5% stop) with every cycle logging only Loop B
    # "Position healthy" HOLDs, because the flat stop check literally never
    # ran. Every open simulated position now always joins the scan list, so
    # its exit rules run every cycle for as long as it's held. Loop B
    # already fetched full data for these tickers on cache misses anyway,
    # so this moves that cost into the parallel Loop A pass rather than
    # adding new fetches.
    held_sim_tickers = []
    try:
        # Paper book always joins the scan list (2026-07-24, Trinath: paper
        # positions must keep being sell-rule-evaluated no matter which mode
        # real trading is in) - previously gated behind is_watch_mode(), so a
        # paper position whose ticker fell off the watchlist/screener list
        # would silently stop being scanned (and therefore stop being
        # sell-checked) the moment EXECUTE mode took over - the exact class
        # of gap the 2026-07-16 fix above this comment closed for WATCH mode.
        # get_all_positions is correct HERE, unlike the exit paths (§5): this
        # builds the SCAN list, and a held SYNC ticker must keep being scanned
        # so that swing_buy_rules.already_open() still vetoes buying more of
        # something the account already holds. Nothing in this list can trigger
        # an exit - rules/sell_rules.py refuses SYNC/SEED rows outright.
        held_sim_tickers = [p["ticker"] for p in db.get_all_positions(simulated=True)]
        if live_trader.is_live_mode(cfg):
            # Same coverage guarantee for the REAL book in live mode.
            held_sim_tickers += [p["ticker"] for p in db.get_all_positions(simulated=False)]
    except Exception as e:
        logger.warning(f"Could not add held positions to scan list: {e}")
    watchlist = list(dict.fromkeys(cfg["watchlist"] + held_sim_tickers + screener_tickers))  # dedup, preserve order
    screener_ticker_set = set(screener_tickers)  # so _evaluate_ticker knows which tickers to feed
                                                  # real outcomes back to engine/screener.py's learning loop
    os.makedirs(PENDING_DIR, exist_ok=True)

    max_parallel = max(1, cfg["trading"].get("cycle_max_parallel_tickers", 8))
    from concurrent.futures import ThreadPoolExecutor as _CycleExecutor, as_completed as _as_completed
    ticker_results = {}
    # Progress-bar follow-up (2026-07-14): this is the one stage with a real,
    # easily-counted unit of work (N tickers), so it's the anchor for the
    # UI's progress bar - see storage/database.py's set_cycle_stage()
    # docstring for how the other stages (market_context/screener/
    # finalizing) get an approximate % band instead of a true count.
    db.set_cycle_stage("ticker_analysis", tickers_total=len(watchlist), tickers_done=0)
    db.clear_cycle_cancel()  # a stale cancel from a previous cycle must not kill this one
    # Time budget (2026-07-15): config's trading.max_cycle_duration_minutes
    # existed but nothing enforced it - cycles were observed running 20+
    # minutes. Once the budget is exceeded (or a cancel is requested via
    # POST /api/cycle/cancel), no NEW ticker analysis starts; in-flight ones
    # finish and the cycle proceeds straight to its tail with whatever was
    # completed. Cooperative, not a hard kill - running MCP subprocesses
    # can't be force-terminated safely from here.
    budget_seconds = max(60, float(cfg["trading"].get("max_cycle_duration_minutes", 20)) * 60)
    cycle_t0 = time.time()
    aborted_tickers = 0
    # 2026-07-16 (hang forensics, Akhil's 20-40min stuck cycles): two traps
    # removed from this block. (1) `_as_completed(futures)` with NO timeout -
    # the between-completions budget check below can only run when a future
    # COMPLETES, so if every in-flight worker wedged (see mcp_clients/base.py
    # run_async + maverick.py semaphore notes) the loop blocked forever
    # waiting for a completion that never came. (2) `with _CycleExecutor(...)`
    # - Executor.__exit__ calls shutdown(wait=True), which blocks on the very
    # wedged threads we're trying to walk away from (same trap base.py's
    # run_async docstring documents for its throwaway pool). Now: as_completed
    # gets the cycle budget as its timeout, and shutdown(wait=False) lets the
    # cycle finish degraded while any genuinely-stuck worker is abandoned.
    from concurrent.futures import TimeoutError as _FuturesTimeout
    ex = _CycleExecutor(max_workers=max_parallel)
    try:
        futures = {
            ex.submit(_evaluate_ticker, ticker, mkt, market_dict, regime, cfg, trading_mode,
                      ticker_data_cache=ticker_data_cache, cycle_count=cycle_count,
                      from_screener=ticker in screener_ticker_set): ticker
            for ticker in watchlist
        }
        try:
            for future in _as_completed(futures, timeout=budget_seconds):
                ticker = futures[future]
                try:
                    ticker_results[ticker] = future.result()
                except Exception as e:
                    # _evaluate_ticker already catches its own exceptions and
                    # returns None - this is a last-resort net in case something
                    # escapes that (e.g. a bug in the exception handler itself),
                    # so one ticker blowing up can never sink the whole cycle.
                    logger.error(f"Unhandled error analyzing {ticker}: {e}", exc_info=True)
                    ticker_results[ticker] = None
                try:
                    db.increment_cycle_tickers_done()
                except Exception as e:
                    logger.warning(f"Progress tracking update failed (non-fatal): {e}")

                # Cancel / budget check between completions: cancel every
                # not-yet-started future (running ones are left to finish).
                over_budget = (time.time() - cycle_t0) > budget_seconds
                cancelled = False
                try:
                    cancelled = db.is_cycle_cancel_requested()
                except Exception:
                    pass
                if over_budget or cancelled:
                    for f, t in futures.items():
                        if f.cancel():
                            aborted_tickers += 1
                            ticker_results.setdefault(t, None)
                    if aborted_tickers:
                        reason = "cancel requested via API" if cancelled else \
                            f"time budget ({budget_seconds/60:.0f} min) exceeded"
                        logger.warning(
                            f"Cycle wind-down: {reason} - skipped {aborted_tickers} "
                            f"not-yet-started tickers; completed work is kept.")
                    if cancelled:
                        db.clear_cycle_cancel()
                    break
        except _FuturesTimeout:
            stuck = [t for f, t in futures.items() if not f.done()]
            for f, t in futures.items():
                if f.cancel():
                    aborted_tickers += 1
                ticker_results.setdefault(t, None)
            logger.error(
                f"Cycle ticker-analysis hit the {budget_seconds/60:.0f}min budget with "
                f"{len(stuck)} ticker(s) still not finished ({stuck[:10]}) - likely "
                f"wedged MCP calls. Abandoning them and finishing the cycle degraded; "
                f"completed work is kept.")
    finally:
        # wait=False: never block the cycle on a wedged worker thread.
        ex.shutdown(wait=False)

    # Write packet files in original watchlist order (deterministic output),
    # now that every ticker's result is in hand - this part is cheap local
    # disk I/O, not worth parallelizing on its own.
    for ticker in watchlist:
        packet = ticker_results.get(ticker)
        if packet:
            packets.append(packet)
            timestamp = datetime.now(ET).strftime("%Y%m%d_%H%M")
            # try/except (2026-07-15): a live incident proved one
            # malformed display field (string P/E from Yahoo) could crash
            # build_ticker_packet and kill the ENTIRE cycle mid-write -
            # every remaining ticker's packet lost. A packet is display
            # output; failing to render one must never abort the scan.
            try:
                packet_text = build_ticker_packet(packet)
                with open(os.path.join(PENDING_DIR, f"{timestamp}_{ticker}.md"), "w") as f:
                    f.write(packet_text)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"{ticker}: packet render failed ({e}) - signal was still "
                    f"logged; skipping this ticker's markdown file only")

    db.set_cycle_stage("finalizing")
    _run_cycle_tail(mkt, cfg, trading_mode, regime, packets, ticker_data_cache,
                     triggered_by, start, cycle_count)


def _log_rejected(db, ticker: str, stage: str, reason: str,
                  score: float = None, price: float = None,
                  would_have_size: float = None):
    """Record a declined candidate (§18). Never raises.

    rejected_signals had 0 rows against portfolio_risk_log's 244 evaluations:
    a complete record of every trade taken and none at all of any trade
    declined, which makes false negatives unmeasurable. This costs one insert
    per rejection and produces the counterfactual dataset that
    analytics/missed_opportunity.py and the regret modules were built to
    consume and have never been fed - missed_opportunity_outcomes is empty.

    Wrapped because a bookkeeping failure must never change a trading
    decision. The decision has already been made by the time this is called;
    losing the record of it is bad, and turning it into an exception that
    propagates into the ticker loop would be worse.
    """
    try:
        db.log_rejected_signal(
            ticker=ticker, reject_stage=stage,
            reject_reason=(reason or "")[:500],
            score_at_rejection=score, price_at_rejection=price,
            would_have_size=would_have_size)
    except Exception as e:
        logger.warning(f"{ticker}: could not record the rejection ({stage}): {e}")


def _calc_regime_and_market_dict(mkt, cfg: dict):
    """Regime + breadth (once per market snapshot) - SPY fetched the same way
    as any watchlist ticker so we reuse TickerAnalyzer rather than a separate
    data path. Fetched BEFORE market_to_dict() so the real market_breadth
    proxy (engine/market_breadth.py) can compare SPY's own trend against
    sector breadth for spy_ad_aligned, instead of defaulting that field to
    True. Shared by run_cycle() and evaluate_single_ticker() so a manual
    single-ticker evaluation sees the exact same market conditions a full
    cycle would have computed - one code path, not two that can drift.
    Returns (regime, market_dict) - does NOT run the market-score gate
    (evaluate_market_score); that's a "should the whole scan proceed" control
    decision specific to run_cycle(), not something a single-ticker lookup
    should be blocked by (the per-ticker hard-veto check already independently
    catches market-wide conditions like BREADTH_PANIC/AD_COLLAPSE)."""
    try:
        spy_td = analyzer.analyze("SPY", mkt, cfg=cfg)
        if spy_td.price <= 0:
            raise ValueError("SPY price fetch returned 0")
    except Exception as e:
        logger.error(f"SPY fetch failed ({e}) - regime/breadth fall back to neutral SPY defaults", exc_info=True)
        spy_td = None

    market_dict = market_to_dict(mkt, cfg, spy_td=spy_td)

    try:
        if spy_td is None:
            raise ValueError("no SPY data this cycle")
        regime = calc_regime(
            spy_price=spy_td.price, spy_sma50=spy_td.sma_50, spy_sma200=spy_td.sma_200,
            vix=mkt.vix_level, fg_score=mkt.fear_greed_score, ad_ratio=market_dict["ad_ratio"],
        )
        logger.info(f"Regime: {regime.dominant_regime} (bull={regime.bull_pct:.0f}% "
                    f"bear={regime.bear_pct:.0f}% choppy={regime.choppy_pct:.0f}%, "
                    f"transition={regime.transition_probability:.0f}%)")
    except Exception as e:
        logger.error(f"Regime calc failed ({e}) - falling back to CHOPPY/neutral", exc_info=True)
        regime = calc_regime(spy_price=100, spy_sma50=100, spy_sma200=100, vix=mkt.vix_level,
                              fg_score=mkt.fear_greed_score, ad_ratio=market_dict.get("ad_ratio", 0.5))

    # Persisted (not just kept in this process's regime_engine._current
    # singleton) so server.py - a SEPARATE process - can show the real
    # current regime instead of always seeing None. See storage/database.py's
    # latest_regime schema comment. market_mood (News tab follow-up,
    # 2026-07-14) piggybacks on the same call - `mkt` already has fear/greed,
    # VIX, and macro-blackout data this cycle fetched for free.
    try:
        db.save_latest_regime(vars(regime), market_mood={
            "fear_greed_score": mkt.fear_greed_score,
            "fear_greed_rating": mkt.fear_greed_rating,
            "vix_level": mkt.vix_level,
            "hours_to_next_macro": mkt.hours_to_next_major_macro,
            "blackout_active": mkt.blackout_active,
            "blackout_reason": mkt.blackout_reason,
        })
    except Exception as e:
        logger.error(f"Failed to persist regime snapshot: {e}", exc_info=True)

    return regime, market_dict


def _evaluate_ticker(ticker: str, mkt, market_dict: dict, regime, cfg: dict, trading_mode: str,
                      ticker_data_cache: dict, cycle_count: int, from_screener: bool = False,
                      allow_paper: bool = True):
    """One ticker's worth of the old inline run_cycle() loop body, extracted
    so evaluate_single_ticker() (server.py's /api/ticker/evaluate_now, called
    right when a ticker is added to the watchlist) can reuse the exact same
    logic instead of a second, drift-prone copy. Returns a packet dict (same
    shape build_ticker_packet()/build_trade_prompt() already expect) or None

    from_screener: True when this ticker came from engine/screener.py's
    candidate list rather than the manual watchlist - feeds the real
    scoring outcome back to db.record_screener_outcome() so the screener
    can eventually learn which of its own candidates are actually worth
    resurfacing (see engine/screener.py's 2026-07-14 learning follow-up).
    Always False for evaluate_single_ticker()'s on-demand single-ticker
    calls - those aren't screener-sourced.
    if the ticker couldn't be analyzed (no price data, or an exception)."""
    try:
        logger.info(f"Analyzing {ticker}...")
        # Two-phase scoring (2026-07-15, cycle-runtime fix): screener
        # candidates get a LITE first pass - bars/quote/indicators only, no
        # maverick/finviz/scanner/news calls (those were ~11 MCP calls per
        # candidate and the bulk of 5-minute-plus cycles). The score()
        # engine's bucket-availability renormalization handles the missing
        # EXTERNAL evidence honestly. Only candidates that score within
        # PROMOTE_MARGIN of the buy bar earn the full fetch + rescore below.
        td = analyzer.analyze(ticker, mkt, cfg=cfg, lite=from_screener)

        if td.price <= 0:
            logger.warning(f"{ticker}: no price data, skipping")
            return None

        ticker_data_cache[ticker] = td

        # Opportunistic company-name/sector/beta cache - all come from the
        # same yfinance fetch analyzer.analyze() just did, so this is zero
        # extra MCP calls. Powers hover tooltips in the UI AND is how
        # engine/portfolio_risk.py looks up an open position's sector/beta
        # without re-fetching it (open positions are often off the current
        # watchlist - see that module's docstring).
        db.upsert_ticker_info(ticker, company_name=td.company_name, last_price=td.price,
                               sector=td.sector, beta=td.beta,
                               # §18: industry comes from the same payload, so
                               # this is still zero extra MCP calls. It is what
                               # lets portfolio_risk classify the ~95% of
                               # traded names the hand-maintained theme_map
                               # never covered.
                               industry=getattr(td, "industry", None))

        # News tab (2026-07-14): td.news_headlines/news_sentiment_score were
        # already being fetched and scored every cycle for the
        # SENTIMENT_MACRO bucket's news_multiplier - just never persisted
        # anywhere before. Zero extra MCP calls; this only stores what
        # analyze() already fetched.
        try:
            if td.news_headlines:
                db.record_news_items(ticker, td.company_name, td.news_headlines, td.news_sentiment_score)
        except Exception as e:
            logger.warning(f"{ticker}: failed to persist news headlines: {e}")

        # Data Provenance Circuit Breaker follow-up: track CONSECUTIVE stale-
        # data cycles for this ticker, independent of whether veto #16
        # (STALE_DATA_CIRCUIT_BREAKER) actually fires this cycle - an earlier
        # veto or already-open status can short-circuit check_vetoes() before
        # veto #16 is even evaluated, but the underlying data quality is the
        # same regardless. A one-off stale cycle is normal (network blip); a
        # streak crossing data_quality.consecutive_stale_alert_cycles usually
        # means something structural (wrong symbol, delisted, a data source
        # that doesn't cover this ticker) - alert ONCE per streak (on the
        # cycle it crosses the threshold, not every cycle after) rather than
        # silently re-scanning forever with no visibility. Never auto-removes
        # the ticker - that decision stays with the person watching the UI.
        dq_cfg = cfg.get("data_quality", {})
        stale_this_cycle = list(td.stale_indicators)
        if market_dict.get("breadth_stale"):
            stale_this_cycle = stale_this_cycle + ["BREADTH"]
        is_stale_cycle = len(stale_this_cycle) >= dq_cfg.get("stale_indicator_veto_threshold", 3)
        consecutive_stale = db.record_ticker_data_health(ticker, is_stale_cycle)
        alert_after = dq_cfg.get("consecutive_stale_alert_cycles", 3)
        if is_stale_cycle and consecutive_stale == alert_after:
            msg = (f"{ticker}: {', '.join(stale_this_cycle)} stale/fallback for {consecutive_stale} "
                   f"consecutive cycles - verify the ticker symbol and that yfinance has real data for it.")
            logger.warning(msg)
            db.log_alert(f"stale_data_{ticker}_{datetime.now(ET).strftime('%Y%m%d')}",
                          "DATA_QUALITY", "MEDIUM", msg)
            db.log_ui_event("data_quality_alert", {
                "ticker": ticker, "consecutive_stale_cycles": consecutive_stale,
                "stale_indicators": stale_this_cycle,
            })

        _close_due_patterns(ticker, td.price, cfg)

        # Book selection: WATCH mode drives the loop with the simulated book;
        # EXECUTE + auto_trade (live mode, 2026-07-16) drives it with the
        # REAL book and places actual Robinhood orders. EXECUTE without
        # auto_trade behaves as before (prompt files only, any open row).
        watch_mode = allow_paper and paper_trader.is_watch_mode(cfg)
        live_mode = allow_paper and not watch_mode and live_trader.is_live_mode(cfg)
        if watch_mode:
            position = db.get_open_position(ticker, simulated=True)
        elif live_mode:
            position = db.get_open_position(ticker, simulated=False)
        else:
            position = db.get_open_position(ticker)
        sell_result = sell_engine.evaluate(td, position, mkt, cfg) if position else None

        # Independent PAPER sell-rule check (2026-07-24, Trinath: the paper
        # book must keep being "monitored and closed as needed" no matter
        # which mode real trading is in - only NEW paper buys are suppressed
        # once EXECUTE takes over, see `if watch_mode or live_mode:` a bit
        # below, unchanged). `position`/`sell_result` above already cover the
        # paper book when watch_mode is True, so this only runs for the
        # live/neither-mode cases to avoid double-evaluating the same row.
        paper_position = None
        paper_sell_result = None
        if allow_paper and not watch_mode:
            paper_position = db.get_open_position(ticker, simulated=True)
            if paper_position:
                paper_sell_result = sell_engine.evaluate(td, paper_position, mkt, cfg)

        score_result = None
        position_size = None
        portfolio_risk_result = None
        # ticker_dict is only built in the else-branch below (an already-held
        # ticker is never re-evaluated as an entry). Bound to None here so the
        # build_pattern_features call further down can pass it unconditionally
        # without a NameError on the already-open path - that path cannot
        # reach the call today (already_open() pins should_buy False), and
        # this makes that a property of the code rather than of the reader.
        ticker_dict = None
        if position:
            # Already holding this ticker - not evaluated as a new entry.
            # Exit logic (sell_result above) is what governs open positions.
            buy_result = swing_buy_rules.already_open()
        else:
            ticker_dict = ticker_to_dict(td, mkt, cfg)
            veto = check_vetoes(ticker, ticker_dict, market_dict, cfg, mode=trading_mode)
            if veto.vetoed:
                logger.info(f"{ticker}: VETOED - {veto.reason}")
                # §55 (Phase 2.5): instrument the stale-data circuit breaker.
                #
                # data_quality.stale_indicator_veto_threshold is 3-of-5 and was
                # chosen a priori. Nothing counted how often it fired, on which
                # indicators, or at what time of day, so "is 3 right?" had no
                # evidence behind it and changing it would have been swapping
                # one guess for another.
                #
                # The reason string already names the offending indicators
                # (hard_vetoes builds it from the stale list), and the row
                # carries a UTC timestamp, so one week of these answers both
                # halves of the question: which indicators default, and whether
                # this is overwhelmingly a first-N-minutes-after-the-open
                # effect - which is what engine/rules_catalog.py claims and
                # what would make the right fix a longer
                # trading.market_open_buffer_minutes rather than a looser cap.
                #
                # Only this one veto code is instrumented. The others are
                # decisions about the NAME (spread, volume, earnings); this one
                # is a decision about our own DATA, and it is the only one
                # whose threshold is up for calibration.
                if veto.veto_code == "STALE_DATA_CIRCUIT_BREAKER":
                    _log_rejected(db, ticker, "data_quality", veto.reason,
                                  price=getattr(td, "price", None))
                buy_result = swing_buy_rules.from_veto(veto)
                # RESEARCH-MODE SCORING (2026-07-15g): for EXECUTION-quality
                # vetoes (spread/volume/price/earnings/timing) the underlying
                # DATA is fine - the name just isn't tradeable right now.
                # Score it anyway for the pattern/learning record, clearly
                # marked, with should_buy pinned False. Answers "what would
                # have been a buy if execution were acceptable?" without
                # loosening a single live guardrail. Data-quality vetoes
                # (STALE_DATA/BAD_DATA) are NOT research-scored - scoring
                # fallback data teaches the learner garbage.
                _RESEARCH_SCORABLE = {"SPREAD_WIDE", "LOW_VOLUME", "PRICE_RANGE",
                                       "EARNINGS_RISK", "DEAD_ZONE", "TOO_LATE"}
                if (veto.veto_code in _RESEARCH_SCORABLE
                        and cfg.get("screener", {}).get("research_score_vetoed", True)):
                    try:
                        score_result = swing_buy_rules.score(
                            ticker_dict, market_dict, regime, cfg, mode=trading_mode,
                            db=db, ticker=ticker)
                        score_result.passed = False  # NEVER a live buy
                        score_result.breakdown = (
                            f"RESEARCH ONLY - vetoed ({veto.veto_code}: {veto.reason}). "
                            + score_result.breakdown)
                        score_result.threshold_result["data_coverage"]["research_only_veto"] = veto.veto_code
                        # keep the veto as the visible decision; attach the
                        # research score for the signals log/UI breakdown
                        buy_result.rules_passed = [
                            type(buy_result.rules_failed[0])(name=r) for r in score_result.rules_fired]
                        # 2026-07-22 fix (Trinath: "most tickers not scored,
                        # what degraded it"): from_veto() above hardcodes
                        # score=0/pct_score=0 - that's correct for a veto with
                        # NO research score (data-quality vetoes), but here a
                        # real research score was just computed and this line
                        # was the only thing missing to surface it. Every
                        # research-scorable veto (SPREAD_WIDE/LOW_VOLUME/
                        # PRICE_RANGE/EARNINGS_RISK/DEAD_ZONE/TOO_LATE) was
                        # silently showing "Score: 0.0%" in the packet header
                        # while its OWN bucket breakdown (rendered separately
                        # from score_result.buckets, never touched by this
                        # bug) displayed the real, often meaningfully
                        # nonzero score underneath it - a 0.0%-headline/
                        # nonzero-buckets mismatch that, in production,
                        # affected the large majority of tickers per cycle
                        # (most candidates carry a spread/volume/timing veto
                        # at any given moment) and made every scoring
                        # improvement invisible in the one place it's most
                        # visible. should_buy stays pinned False either way -
                        # only the DISPLAYED score changes, never the trading
                        # decision.
                        buy_result.score = score_result.final_score_pct
                        buy_result.pct_score = score_result.final_score_pct
                        buy_result.top_signals = buy_result.rules_passed[:3]
                        if score_result.final_score_pct >= score_result.threshold:
                            logger.info(
                                f"{ticker}: research score {score_result.final_score_pct:.1f}% "
                                f">= bar {score_result.threshold:.0f}% but UNTRADEABLE "
                                f"({veto.veto_code}) - recorded for learning only")
                    except Exception as e:
                        logger.warning(f"{ticker}: research scoring failed (non-fatal): {e}")
                        score_result = None
            else:
                score_result = swing_buy_rules.score(ticker_dict, market_dict, regime, cfg, mode=trading_mode,
                                                      db=db, ticker=ticker)

                # Phase-2 promotion: a lite-scored screener candidate close
                # enough to the bar gets the full external fetch (maverick/
                # finviz/scanner/news) and a rescore - so cheap evidence
                # filters the field, expensive evidence makes the final call.
                #
                # 2026-07-22 catch-22 fix (Trinath: "not even one ticker
                # selected in 3 days that crossed 45%... zero if profile set
                # to moderate or conservative"): this used to compare against
                # score_result.threshold - the FINAL, already-inflated bar
                # (base + stress/VIX/calendar/breadth, up to +20%, e.g.
                # CONSERVATIVE's base 68% can reach 85%). A lite candidate is
                # structurally incapable of reaching a bar that high - it's
                # missing the whole EXTERNAL bucket's evidence (up to 54 of
                # ~250 raw points, see engine/ticker_data_adapter.py's
                # external_bucket_max_points) and hasn't been given the
                # chance to earn those points yet. Gatekeeping "is this worth
                # a deeper look" with the SAME inflated number used for the
                # final buy/no-buy decision double-counts every stress/
                # calendar/breadth penalty and every profile's risk aversion
                # BEFORE the candidate ever gets its full evidence - which is
                # exactly how CONSERVATIVE/MODERATE ended up promoting (and
                # therefore buying) nothing at all. Promotion should only ask
                # "is this worth the expensive fetch", not "does it already
                # clear today's worst-case bar" - so it's now measured
                # against the profile's own nominal base_threshold (or the
                # final threshold, if that happens to be LOWER - a bull-
                # regime/positive-EV credit should still make promotion
                # easier, never harder). The actual buy decision below is
                # UNCHANGED - it still requires clearing the real, fully-
                # inflated final threshold after the rescore.
                PROMOTE_MARGIN = 15.0
                promote_reference = min(
                    score_result.threshold_result.get("base_threshold", score_result.threshold),
                    score_result.threshold,
                )
                if (from_screener and td.data_quality == "lite"
                        and score_result.final_score_pct >= promote_reference - PROMOTE_MARGIN):
                    logger.info(f"{ticker}: lite score {score_result.final_score_pct:.1f}% within "
                                f"{PROMOTE_MARGIN} of nominal bar {promote_reference:.0f}% "
                                f"(final bar {score_result.threshold:.0f}%) - full rescore")
                    td_full = analyzer.analyze(ticker, mkt, cfg=cfg, lite=False)
                    if td_full.price > 0:
                        td = td_full
                        ticker_data_cache[ticker] = td
                        ticker_dict = ticker_to_dict(td, mkt, cfg)
                        score_result = swing_buy_rules.score(
                            ticker_dict, market_dict, regime, cfg, mode=trading_mode,
                            db=db, ticker=ticker)

                buy_result = swing_buy_rules.from_score_result(score_result)

                if buy_result.should_buy:
                    # Resolved trade_mode (2026-07-22, full DAY/SWING/HYBRID
                    # separation - moved up from just before order execution
                    # below, see the comment that used to live there) -
                    # computed HERE, before sizing, so both the Position
                    # Sizing Engine (day_size_multiplier) and the risk_per_share
                    # seed a few lines down (which feeds BOTH sell_rules.py's
                    # R-multiple take-profit target AND
                    # engine/stop_state_machine.py's fallback risk_per_share)
                    # can be genuinely mode-aware instead of always assuming
                    # swing risk. Always uppercase ("DAY"/"SWING") regardless
                    # of trading_mode's casing, matching
                    # _classify_hybrid_leg's convention and
                    # positions.trade_mode's existing stored values.
                    effective_mode = (_classify_hybrid_leg(td, score_result)
                                      if trading_mode.lower() == "hybrid" else trading_mode.upper())
                    if trading_mode.lower() == "hybrid":
                        logger.info(f"{ticker}: hybrid buy classified as {effective_mode} "
                                    f"(vol_ratio {getattr(td, 'volume_ratio', 0):.2f}, "
                                    f"chg {getattr(td, 'change_pct', 0):+.2f}%)")
                    # Portfolio Risk Manager (engine/portfolio_risk.py) runs
                    # BEFORE sizing so its size_multiplier can fold into the
                    # Position Sizing Engine's final suggested $ - "not every
                    # qualifying trade deserves the same capital, and some
                    # shouldn't get MORE capital added to an already-crowded
                    # sector/theme/correlation cluster." Never itself blocks
                    # the signal (advisory only, same posture as everything
                    # else in this codebase - see README).
                    try:
                        from engine.portfolio_risk import PortfolioRiskEngine
                        atr_pct = (ticker_dict.get("atr", 0) / td.price * 100) if td.price else 0.0
                        planned_amount = cfg["trading"]["trade_size_usd"]
                        portfolio_risk_result = PortfolioRiskEngine(db).evaluate(
                            ticker, td.sector, td.beta, planned_amount, atr_pct, cfg,
                            candidate_industry=getattr(td, "industry", None),
                        )
                    except Exception as e:
                        logger.error(f"{ticker}: portfolio risk check failed: {e}", exc_info=True)

                    try:
                        from engine.position_sizing import calculate as calc_position_size
                        position_size = calc_position_size(
                            buy_result, score_result, ticker_dict, regime, cfg,
                            portfolio_risk_result=portfolio_risk_result,
                            mode=effective_mode,  # 2026-07-22: applies day_size_multiplier for DAY legs
                        )
                    except Exception as e:
                        logger.error(f"{ticker}: position sizing failed: {e}", exc_info=True)

        new_pattern_id = None
        if buy_result.should_buy and not _has_open_pattern(ticker):
            # ticker_dict/market_dict (2026-07-26, documentation audit): both
            # were already built for this ticker this cycle, and passing them
            # is what makes adx/cmf/sector RS/squeeze/unusual-options/opex
            # reach the pattern database as REAL values rather than the
            # constants this call used to record. Zero extra fetches - see
            # engine/pattern_features.py's module docstring for what the
            # constants were doing to similarity search.
            features = build_pattern_features(ticker, td, mkt, buy_result, cfg,
                                               regime=regime, score_result=score_result,
                                               ticker_dict=ticker_dict,
                                               market_dict=market_dict)
            # 2026-07-22 (EV mode-keying fix): used to hardcode "SWING" here
            # regardless of trading_mode/effective_mode, so every DAY or
            # HYBRID-configured account's patterns were ALL stored under
            # "SWING" while rules/swing_buy_rules.py's EV lookup queried
            # "DAY" (or, before that file's own fix, "HYBRID") - a permanent
            # mismatch that meant DAY/HYBRID EV lookups never found a single
            # match, ever. `effective_mode` is already resolved above (right
            # after `if buy_result.should_buy:`) to "DAY" or "SWING" - a
            # HYBRID leg is classified per-setup by _classify_hybrid_leg, a
            # plain SWING/DAY config just uppercases trading_mode - so this
            # now records the pattern under the SAME mode key the EV lookup
            # will later query it under.
            # cfg is passed so the row is stamped with the build and the
            # config fingerprint that produced it (§17, Phase 1) - without it
            # a future contamination event is unfilterable, which is exactly
            # how the 23 pre-2026-07-20 patterns became a problem.
            new_pattern_id = pattern_db.record_entry(ticker, effective_mode, features, cfg=cfg)

        if sell_result and sell_result.should_sell:
            logger.info(f"{ticker}: SELL SIGNAL - {sell_result.reason}")
            if watch_mode and position and position.get("simulated"):
                # Mimic acting on the sell signal at the current price - the
                # linked pattern closes with this rule-driven outcome, which
                # is what the learning loop trains on.
                try:
                    paper_trader.execute_sell(db, ticker, td.price,
                                               reason=f"sell_rules:{sell_result.reason}",
                                               pattern_db=pattern_db, cfg=cfg,
                                               # §D: the sell rules decided this
                                               # at the trigger; do not make
                                               # close_pattern re-derive it from
                                               # the sentence, which cannot.
                                               exit_kind=sell_result.exit_kind or None)
                except Exception as e:
                    logger.error(f"{ticker}: paper sell failed: {e}", exc_info=True)
            elif live_mode and position and not position.get("simulated"):
                # LIVE: real Robinhood market sell (engine/live_trader.py).
                try:
                    live_trader.execute_sell_live(db, cfg, ticker,
                                                   reason=f"sell_rules:{sell_result.reason}",
                                                   pattern_db=pattern_db,
                                                   exit_kind=sell_result.exit_kind or None)
                except Exception as e:
                    logger.error(f"{ticker}: live sell failed: {e}", exc_info=True)
        elif buy_result.should_buy:
            logger.info(f"{ticker}: BUY CANDIDATE - score {buy_result.pct_score:.0f}%")
            db.log_ui_event("buy_signal", {
                "ticker": ticker, "pct_score": buy_result.pct_score,
                "regime": regime.dominant_regime, "cycle": cycle_count,
            })
            # §47.3 (Phase 3). The engine states WHAT happened and how urgent
            # it is. It does not know or care how the human finds out - that
            # is scripts/tp_agent.py's job, and it is the only part of the
            # system that differs by operating system. This is what lets the
            # engine run in a container that has no route to a notification
            # centre while the popups keep working.
            _notify_via_outbox(
                cfg,
                severity="info",
                title=f"BUY {ticker} {buy_result.pct_score:.0f}%",
                body=f"regime {regime.dominant_regime}",
                threshold_pct=buy_result.pct_score,
            )
            if watch_mode or live_mode:
                # Take the buy at the signal price, sized by the Position
                # Sizing Engine - simulated fill in WATCH mode, a REAL
                # Robinhood order in EXECUTE + auto_trade (live_trader.py,
                # every safety gate documented there). HYBRID resolves to
                # DAY or SWING per setup (see _classify_hybrid_leg) so every
                # trade carries a real category instead of an ambiguous tag.
                try:
                    # effective_mode already computed above (right after
                    # `if buy_result.should_buy:`) so sizing could use it too
                    # - reused here, not recomputed.
                    pid = new_pattern_id or _latest_open_pattern_id(ticker)
                    # Rotation inputs (engine/rotation.py): the candidate's
                    # final score (rotation bar), this cycle's known prices
                    # (a paper rotation-sell needs the victim's fill price),
                    # and pattern_db (the victim's linked pattern closes with
                    # the real rotation outcome, same as any rule-driven sell).
                    _buy_score = getattr(score_result, "final_score_pct", None)
                    _cycle_prices = {t: d.price for t, d in ticker_data_cache.items()
                                     if getattr(d, "price", 0) > 0}
                    # 2026-07-20 (entry-context audit, Trinath's ask): the
                    # score/regime/ATR context computed for THIS signal used
                    # to be dropped on the way into positions - db.open_position()
                    # never took them, so entry_signal_score/entry_regime/
                    # setup_type/risk_per_share sat NULL for every paper/live
                    # position ever opened. That silently defeated
                    # stop_state_machine.py's tiered ATR multiplier (always
                    # fell back to the 75-score "standard" branch),
                    # sell_rules.py's dynamic R-multiple take-profit (always
                    # fell back to the flat 10% target), and
                    # rules/exit_scorer.py's regime-deterioration rule (never
                    # fires with entry_regime=None). confirm_fill.py already
                    # does this seeding for the manual live-confirm path
                    # (see its `db.update_position_by_ticker(...)` call) -
                    # this mirrors that same shape for the automated
                    # paper/live buy path, computed once here since every
                    # piece (score_result, regime, ticker_dict's ATR, the
                    # pattern features just built above) is already in scope.
                    # Price-dependent fields (current_stop_price/high_watermark)
                    # are deliberately left for execute_buy/execute_buy_live to
                    # fill in themselves, since live's fill price can differ
                    # from td.price (the signal price) and the seed should
                    # reflect what was actually paid.
                    _entry_atr = float(ticker_dict.get("atr") or 0)
                    # DAY-mode risk_per_share seed (2026-07-22, full DAY/
                    # SWING/HYBRID separation): uses stop_loss_day_pct
                    # instead of stop_loss_swing_pct when this leg resolved
                    # to DAY (effective_mode, computed above) - this single
                    # value cascades into BOTH sell_rules.py's R-multiple
                    # take-profit target (entry + risk_per_share * r_multiple)
                    # AND engine/stop_state_machine.py's fallback risk_per_share
                    # (position.get("risk_per_share") or atr*1.5), so a DAY
                    # leg gets a tighter take-profit target and a tighter
                    # stop from this one seed, with no changes needed to
                    # sell_rules.py itself.
                    _risk_profile_cfg = cfg.get("risk", {}).get(cfg.get("risk_level", "MODERATE"), {})
                    _max_risk_pct = (
                        _risk_profile_cfg.get("stop_loss_day_pct", _risk_profile_cfg.get("stop_loss_swing_pct", 5.0) / 2)
                        if effective_mode == "DAY"
                        else _risk_profile_cfg.get("stop_loss_swing_pct", 5.0)
                    ) / 100.0
                    _risk_per_share = min(max(1.2 * _entry_atr, td.price * 0.015),
                                           td.price * _max_risk_pct) if td.price else 0.0
                    # `features` is only bound above when this ticker didn't
                    # already have an open pattern (_has_open_pattern gate) -
                    # locals().get(...) avoids a NameError on the branch where
                    # it wasn't built this cycle.
                    _features = locals().get("features")
                    _entry_seed = {
                        "entry_signal_score": _buy_score,
                        "entry_regime": getattr(regime, "dominant_regime", None),
                        "setup_type": (_features.get("setup_type") if _features else None) or "unknown",
                        "risk_per_share": _risk_per_share,
                        # §53 (Phase 2.5): the SAME quantity the portfolio-risk
                        # candidate check uses one screen up
                        # (atr_pct = atr / price * 100), persisted so that the
                        # count of already-open high-volatility positions is
                        # measured in ATR units too. Before this, that count
                        # substituted stop distance and read systematically
                        # low - see engine/portfolio_risk._position_atr_pct.
                        "entry_atr_pct": (_entry_atr / td.price * 100) if td.price else None,
                    }
                    # ── §18 (Phase 2): portfolio risk BINDS ────────────────
                    #
                    # This result was computed above and, until now, only fed
                    # the sizing multiplier. A limit that is measured and then
                    # ignored is documentation. `allowed` is False only when
                    # portfolio_risk.hard_block_on_severe_breach is on AND a
                    # cap is breached severely (1.5x by default), so the
                    # ordinary case is still a size reduction rather than a
                    # refusal.
                    #
                    # Placed after sizing so the log records the size this
                    # trade WOULD have taken - that figure is the whole value
                    # of the counterfactual, and computing it costs nothing
                    # since sizing has already run.
                    if portfolio_risk_result is None and not watch_mode:
                        # P1-07 (audit finding, external review 2026-07-26):
                        # the try/except around PortfolioRiskEngine.evaluate()
                        # above logs and swallows any exception, leaving
                        # portfolio_risk_result=None - which this block used
                        # to treat as "no opinion, proceed" (the `is not None`
                        # guard skipped the whole check). For a REAL order
                        # that is fail-OPEN: a risk-engine crash silently
                        # removed the only thing standing between a signal
                        # and an unbounded-concentration live trade. Live
                        # mode now fails CLOSED - an unevaluable portfolio
                        # risk check blocks the buy exactly like a measured
                        # severe breach would. Paper/watch mode is left
                        # advisory (unchanged) since nothing real is at stake
                        # and blocking it would also break paper trading
                        # whenever the risk engine has a transient bug.
                        _would_have = getattr(position_size, "suggested_dollar_amount", None)
                        logger.error(f"{ticker}: BUY blocked - portfolio risk could not be "
                                     f"evaluated (see error above) and live mode fails closed")
                        _log_rejected(db, ticker, "portfolio_risk_unavailable",
                                      "portfolio risk engine raised an exception; "
                                      "live mode fails closed rather than proceeding unchecked",
                                      _buy_score, td.price, _would_have)
                        db.log_ui_event("buy_blocked", {
                            "ticker": ticker, "stage": "portfolio_risk_unavailable",
                            "reason": "portfolio risk engine unavailable; live fails closed",
                            "would_have_size": _would_have,
                        })
                        continue_buy = False
                    elif portfolio_risk_result is not None and not portfolio_risk_result.allowed:
                        _would_have = getattr(position_size, "suggested_dollar_amount", None)
                        logger.info(f"{ticker}: BUY blocked by portfolio risk - "
                                    f"{portfolio_risk_result.reason}")
                        _log_rejected(db, ticker, "portfolio_risk",
                                      portfolio_risk_result.reason,
                                      _buy_score, td.price, _would_have)
                        db.log_ui_event("buy_blocked", {
                            "ticker": ticker, "stage": "portfolio_risk",
                            "reason": portfolio_risk_result.reason,
                            "would_have_size": _would_have,
                        })
                        continue_buy = False
                    else:
                        continue_buy = True

                    if not continue_buy:
                        pass
                    elif watch_mode:
                        paper_trader.execute_buy(
                            db, cfg, ticker, td.price, position_size=position_size,
                            pattern_id=pid, trade_mode=effective_mode,
                            buy_score=_buy_score, prices=_cycle_prices,
                            pattern_db=pattern_db, entry_seed=_entry_seed)
                    else:
                        live_trader.execute_buy_live(
                            db, cfg, ticker, td.price, position_size=position_size,
                            pattern_id=pid, trade_mode=effective_mode,
                            buy_score=_buy_score, pattern_db=pattern_db,
                            entry_seed=_entry_seed)
                except Exception as e:
                    logger.error(f"{ticker}: {'paper' if watch_mode else 'live'} buy failed: {e}",
                                 exc_info=True)
        else:
            logger.info(f"{ticker}: HOLD - score {buy_result.pct_score:.0f}%")
            # §18: record the counterfactual. portfolio_risk_log had 244 rows
            # of evaluations and rejected_signals had 0, so there was a record
            # of every trade taken and none of any trade declined - which
            # makes false negatives unmeasurable. One insert per rejection
            # feeds the missed_opportunity and regret modules, which were
            # built to consume exactly this and have never been given
            # anything (missed_opportunity_outcomes is empty).
            #
            # `stage` distinguishes the reasons: a name vetoed for a wide
            # spread and a name that simply scored 58% are different
            # questions, and pooling them would make the dataset unusable for
            # either.
            _stage = "veto" if getattr(buy_result, "veto_code", None) else "threshold"
            _log_rejected(db, ticker, _stage,
                          getattr(buy_result, "reason", None) or "below threshold",
                          buy_result.pct_score, td.price, None)

        # Dispatch the independent paper sell check computed above - only
        # populated when watch_mode is False (see its computation above), so
        # this never double-closes a position the watch_mode branch already
        # handled via `sell_result`/`position`.
        if paper_sell_result and paper_sell_result.should_sell:
            logger.info(f"{ticker}: PAPER SELL SIGNAL (mode-independent) - {paper_sell_result.reason}")
            try:
                paper_trader.execute_sell(db, ticker, td.price,
                                           reason=f"sell_rules:{paper_sell_result.reason}",
                                           pattern_db=pattern_db, cfg=cfg,
                                           exit_kind=paper_sell_result.exit_kind or None)
            except Exception as e:
                logger.error(f"{ticker}: paper sell (mode-independent check) failed: {e}", exc_info=True)

        # §16: these two lines are the clearest illustration of the cross-book
        # bug. Same ticker, two different rows, two different highs - and
        # until the book scope was added, each of these statements wrote to
        # BOTH rows, so the second call silently overwrote the first with the
        # other book's number. trail_high feeds the trailing stop, so the real
        # holding's stop was tracking the paper position's price history.
        if paper_position and td.price > paper_position.get("trail_high", paper_position["entry_price"]):
            db.update_trail_high(ticker, td.price, simulated=True)

        if position and td.price > position.get("trail_high", position["entry_price"]):
            db.update_trail_high(ticker, td.price, simulated=False)

        # Full decision-context snapshot, persisted alongside the signal so
        # analytics/decision_replay.py can reconstruct "why did the system
        # buy/hold/sell TICKER on this date" from stored data alone, without
        # re-deriving anything live. score_result is None for vetoed/
        # already-open tickers - threshold/ev/execution-quality were never
        # computed for those, so those fields correctly stay None too.
        db.log_signal(
            ticker, td, buy_result, sell_result, score_result=score_result,
            threshold_result=getattr(score_result, "threshold_result", None),
            ev_result=getattr(score_result, "ev_result", None),
            execution_quality=getattr(score_result, "execution_quality", None),
            position_size=position_size,
            portfolio_risk=portfolio_risk_result,
            regime=regime,
            asset_class=getattr(score_result, "asset_class", None),
            probabilistic_decision=getattr(score_result, "probabilistic_decision", None),
            # 2026-07-22 (EV mode-keying follow-up): effective_mode is only
            # ever set above when buy_result.should_buy was True this cycle
            # (locals().get avoids a NameError on the HOLD/veto/already-open
            # branches where it was never computed) - falls back to the raw
            # configured trading_mode so every row still carries SOME mode
            # label, just not a post-classification one for non-BUY rows.
            trade_mode=(locals().get("effective_mode") or trading_mode.upper()),
        )

        # Screener learning follow-up: feed the REAL scoring outcome back to
        # engine/screener.py's candidate-quality tracking - only for tickers
        # that came from the screener, not the manual watchlist (a manually
        # curated ticker's outcome isn't a signal about screener quality).
        # buy_pct only recorded when scoring actually ran (score_result is
        # not None) - a vetoed candidate's pct_score is always 0, which
        # would incorrectly drag down the average otherwise.
        if from_screener:
            try:
                # Outage hygiene (2026-07-15c, external review): a score
                # produced under bucket-availability redistribution (EXTERNAL
                # sources down) is not comparable to a fully-observed score -
                # counting it into a ticker's qualify-rate statistics would
                # let a data outage brand good candidates "low quality".
                # Skip the stats write for those cycles entirely.
                _outage = bool(
                    score_result is not None
                    and (score_result.threshold_result or {}).get("data_coverage", {}).get("unavailable_buckets")
                )
                if _outage:
                    logger.info(f"{ticker}: outage-adjusted score - excluded from screener learning stats")
                else:
                    db.record_screener_outcome(
                        ticker, trading_mode, qualified=bool(buy_result.should_buy),
                        stale_data_blocked=is_stale_cycle,
                        buy_pct=(buy_result.pct_score if score_result is not None else None),
                    )
            except Exception as e:
                logger.error(f"{ticker}: screener outcome tracking failed: {e}", exc_info=True)

        return {
            "ticker": ticker,
            "td": td,
            "buy_result": buy_result,
            "sell_result": sell_result,
            "position": position,
            "market_context": mkt,
            "position_size": position_size,
            "portfolio_risk": portfolio_risk_result,
            "score_result": score_result,
        }

    except Exception as e:
        logger.error(f"Error analyzing {ticker}: {e}", exc_info=True)
        return None


def _run_cycle_tail(mkt, cfg: dict, trading_mode: str, regime, packets: list,
                     ticker_data_cache: dict, triggered_by: str, start: float, cycle_count: int):
    """Loop B (position management) + learning-loop automation + prompt
    writing + cycle logging - everything run_cycle() does after its per-ticker
    loop finishes, extracted so it stays in one place. NOT run by
    evaluate_single_ticker() - a single ad-hoc ticker lookup shouldn't
    re-manage every open position or trigger the learning loop, it's purely
    informational."""
    # Near-miss telemetry (2026-07-15g): answers "zero buys because the tape
    # is bad, or because the bar is miscalibrated?" - a cycle with several
    # names within 5 pts of the bar is a calibration/coverage question; a
    # cycle where nothing gets close is just a bad tape. Logged every cycle
    # so the distinction is visible over time without any DB spelunking.
    try:
        _near, _buys, _scored = 0, 0, 0
        for p in packets:
            sr = p.get("score_result")
            if sr is None:
                continue
            _scored += 1
            if sr.passed:
                _buys += 1
            elif sr.final_score_pct >= sr.threshold - 5.0:
                _near += 1
        logger.info(f"Cycle signal quality: {_buys} BUY, {_near} near-miss "
                    f"(within 5 pts of bar), {_scored} scored total"
                    + (" - tape offered nothing close this cycle" if (_buys + _near) == 0 and _scored > 0 else ""))
    except Exception:
        pass

    # ══ LOOP B: POSITION MANAGEMENT ══ - runs for every open position
    # (confirm_fill.py-managed, not the watchlist scan above), reusing
    # ticker_data_cache so watchlist tickers that are also open positions
    # aren't fetched twice.
    try:
        position_actions = run_loop_b(ticker_data_cache, mkt, cfg, regime=regime, analyzer=analyzer)
    except Exception as e:
        logger.error(f"Loop B (position management) failed: {e}", exc_info=True)
        position_actions = []

    watch_mode_cycle = paper_trader.is_watch_mode(cfg)
    live_mode_cycle = not watch_mode_cycle and live_trader.is_live_mode(cfg)
    for a in position_actions:
        pa = a["priority_action"]
        logger.info(f"{a['ticker']}: [Loop B] {pa['label']} (priority {pa['priority']}) - {pa['reason']}")
        if pa.get("urgent"):
            db.log_ui_event("urgent_exit", {
                "ticker": a["ticker"], "label": pa["label"], "reason": pa["reason"],
                "priority": pa["priority"], "cycle": cycle_count,
            })
            # §47.3, as above: state the event, let the host agent deliver it.
            _notify_via_outbox(cfg, severity="critical",
                               title=f"URGENT EXIT: {a['ticker']}",
                               body=pa["reason"])
            # Urgent Loop B exits are acted on immediately: paper sell for
            # SIMULATED positions - ALWAYS, regardless of watch/live mode
            # (2026-07-24, Trinath: paper positions must keep being
            # "monitored and closed as needed" no matter which mode real
            # trading is in - this used to require watch_mode_cycle, which
            # left paper/SEED positions un-auto-closed - advisory alert
            # only - the moment EXECUTE+auto_trade armed) - and a REAL
            # Robinhood sell for real positions only in EXECUTE + auto_trade.
            # Otherwise (real position, not armed) advisory only.
            if (a.get("position") or {}).get("simulated"):
                price = getattr(a.get("ticker_data"), "price", None)
                try:
                    paper_trader.execute_sell(db, a["ticker"], price,
                                               reason=f"loop_b_urgent:{pa['label']}",
                                               pattern_db=pattern_db, cfg=cfg,
                                               # §D: only the EOD flatten is a
                                               # distinct kind; the rest are the
                                               # Exit Score acting = rule_exit.
                                               exit_kind=exit_kind_for_loop_b_label(pa["label"]))
                except Exception as e:
                    logger.error(f"{a['ticker']}: paper urgent exit failed: {e}", exc_info=True)
            elif live_mode_cycle and not (a.get("position") or {}).get("simulated"):
                try:
                    live_trader.execute_sell_live(db, cfg, a["ticker"],
                                                   reason=f"loop_b_urgent:{pa['label']}",
                                                   pattern_db=pattern_db,
                                                   exit_kind=exit_kind_for_loop_b_label(pa["label"]))
                except Exception as e:
                    logger.error(f"{a['ticker']}: live urgent exit failed: {e}", exc_info=True)

    # ══ Learning loop automation ══ - cheap no-op most cycles (just checks the
    # trigger condition against learning_runs); only does real work every
    # learning.walk_forward_trigger_trades closed patterns or
    # learning.walk_forward_trigger_days days - walk-forward attribution
    # across every rule plus a champion/challenger re-evaluation, which is
    # NOT cheap. 2026-07-14: Trinath asked for this to run in the background
    # instead of adding to the reported cycle time - previously this ran
    # inline, so on whichever cycle happened to cross the trigger threshold,
    # the prompt file (and therefore "Cycle #N done in Xs") would wait on the
    # full learning run to finish first. `storage/database.py`'s Database
    # already opens a fresh sqlite3 connection per call under its own lock
    # (verified safe for concurrent use earlier this session), so it's safe
    # to hand this to a daemon thread and let run_cycle() finish/write the
    # prompt without waiting on it. A daemon thread's uncaught exception
    # doesn't propagate anywhere on its own, hence the explicit try/except
    # + logging inside the wrapper - silently losing a learning-loop failure
    # would be worse than the inline version this replaces.
    def _run_learning_loop_background():
        try:
            # 2026-07-22 (EV mode-keying fix): pattern_database rows are only
            # ever recorded under "DAY" or "SWING" (never "HYBRID" - see
            # record_entry's call above and _classify_hybrid_leg) - passing
            # mode="HYBRID" straight through for a HYBRID-configured account
            # used to query a pattern-DB bucket that's never written, so the
            # walk-forward/champion-challenger learning loop silently never
            # triggered for any HYBRID account. Run it once per real bucket a
            # HYBRID config actually populates; plain SWING/DAY configs still
            # run their single real bucket exactly as before.
            _learning_modes = ("DAY", "SWING") if trading_mode.lower() == "hybrid" else (trading_mode.upper(),)
            for _lm in _learning_modes:
                maybe_run_learning_loop(db, cfg, mode=_lm)
        except Exception as e:
            logger.error(f"Learning loop automation failed (background): {e}", exc_info=True)

        # 2026-07-23 (OXY dynamic-threshold review): weekly, time-triggered,
        # mode-agnostic (HOLD signals aren't split by mode the way
        # pattern_database rows are) - same "cheap no-op most cycles" shape,
        # so it rides along in this same background thread rather than
        # spawning a second one.
        try:
            maybe_run_threshold_regret(db, cfg)
        except Exception as e:
            logger.error(f"Threshold-regret automation failed (background): {e}", exc_info=True)

    threading.Thread(
        target=_run_learning_loop_background, daemon=True, name="learning-loop-bg"
    ).start()

    # ══ Weekly historical-replay automation (2026-07-23) ══ - same
    # cheap-no-op-most-cycles / background-thread pattern as the learning
    # loop above (engine/backtest_loop.py's maybe_run_weekly() only does real
    # work every config.yaml backtest.weekly_trigger_days days; every other
    # cycle this is a single fast DB read). Runs in its own daemon thread so
    # a multi-minute historical replay never adds to this cycle's reported
    # duration, and a failure here is caught/logged rather than taking down
    # run_cycle() - same rationale as _run_learning_loop_background above.
    def _run_backtest_loop_background():
        try:
            maybe_run_weekly_backtest(db, cfg)
        except Exception as e:
            logger.error(f"Weekly backtest automation failed (background): {e}", exc_info=True)

    threading.Thread(
        target=_run_backtest_loop_background, daemon=True, name="backtest-loop-bg"
    ).start()

    if packets or position_actions:
        prompt = build_trade_prompt(packets, cfg, position_actions=position_actions)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, "trade_prompt.md"), "w") as f:
            f.write(prompt)
        with open(os.path.join(OUTPUT_DIR, "analysis_packet.md"), "w") as f:
            f.write("\n\n---\n\n".join(build_ticker_packet(p) for p in packets))
        logger.info("Prompt ready -> output/trade_prompt.md")
        logger.info(
            f"  {len(packets)} tickers | "
            f"{sum(1 for p in packets if p['buy_result'].should_buy)} buy candidates | "
            f"{sum(1 for p in packets if p.get('sell_result') and p['sell_result'].should_sell)} sell signals | "
            f"{len(position_actions)} managed positions"
        )
        _prune_pending_prompts()

    try:
        db.prune_old_news()
    except Exception as e:
        logger.warning(f"News pruning failed: {e}")

    # ── Paper equity-curve point (2026-07-16, Portfolio tab) ── one snapshot
    # per cycle, regardless of watch/execute mode (2026-07-24, Trinath: the
    # paper equity curve must keep recording no matter which mode real
    # trading is in - previously gated to WATCH-only, so the curve went flat
    # the moment EXECUTE+auto_trade armed even though the paper book was
    # still open and being priced) so the UI can plot portfolio value over
    # time without gaps. Prices come from this cycle's already-fetched
    # ticker data plus the ticker_info cache for held names that weren't on
    # this cycle's scan list - zero extra network calls. snap.get("seeded")
    # below already no-ops safely on a not-yet-seeded account.
    try:
        prices = {t: td.price for t, td in ticker_data_cache.items()
                  if getattr(td, "price", 0) and td.price > 0}
        held = [p["ticker"] for p in db.get_all_positions(simulated=True)]
        missing = [t for t in held if t not in prices]
        if missing:
            # 2026-07-16 fix ('portfolio value never moves'): a held
            # ticker that drops off the scan list was falling back to a
            # ticker_info last_price frozen at buy time - so market_value
            # stayed pinned at cost and the equity curve flatlined. Fetch
            # a LIVE quote for each missing holding (<= max_positions of
            # them, provider REST, seconds) and write it back so the
            # cache heals too; last_price stays as the final fallback.
            from mcp_clients.market_data import router as md_router
            for t in missing:
                try:
                    q = md_router.get_quote(t)
                    if q and q[0].get("price"):
                        prices[t] = q[0]["price"]
                        db.upsert_ticker_info(t, last_price=q[0]["price"])
                except Exception:
                    pass
            still_missing = [t for t in missing if t not in prices]
            if still_missing:
                info = db.get_ticker_info_bulk(still_missing)
                for t, i in info.items():
                    lp = (i or {}).get("last_price")
                    if lp:
                        prices[t] = lp
        snap = paper_trader.snapshot(db, prices=prices)
        if snap.get("seeded"):
            db.record_paper_equity(snap)
    except Exception as e:
        logger.warning(f"Paper equity snapshot failed: {e}")

    duration = time.time() - start
    db.log_cycle(cycle_count, len(packets), duration=duration, triggered_by=triggered_by)
    logger.info(f"Cycle #{cycle_count} done in {duration:.0f}s")

    db.log_ui_event("cycle_complete", {
        "cycle": cycle_count,
        "n_tickers": len(packets),
        "n_buy": sum(1 for p in packets if p["buy_result"].should_buy),
        "n_sell": sum(1 for p in packets if p.get("sell_result") and p["sell_result"].should_sell),
        "n_managed_positions": len(position_actions),
        "regime": regime.dominant_regime,
        "duration_sec": round(duration, 1),
        "triggered_by": triggered_by,
    })

    # §9 (Phase 2): belt and braces. Both sell paths already check the breaker
    # after every close, so this should never be the one that trips it - but
    # "should never" is why the original had zero call sites. A close that
    # bypassed those paths, or a loss booked by confirm_fill.py between cycles,
    # gets caught here at the cost of one query per cycle.
    try:
        from rules.risk_rules import trip_kill_switch_if_needed
        if trip_kill_switch_if_needed(db, cfg):
            logger.critical("Kill switch tripped at end of cycle - "
                            "a close outside the normal sell paths breached the limit")
    except Exception as e:
        logger.error(f"End-of-cycle kill-switch check failed: {e}", exc_info=True)


def evaluate_single_ticker(ticker: str, cfg: dict = None) -> dict | None:
    """On-demand single-ticker evaluation, independent of the watchlist loop -
    used by server.py's /api/ticker/evaluate_now so a freshly-added ticker
    shows up under the Signals tab immediately instead of waiting for the
    next full scheduled cycle (which could be up to scan_interval_minutes
    away, or until Monday if the market's closed). Runs regardless of market
    hours/kill-switch/risk limits - this only ever WRITES a signals-table row
    for visibility, it never opens a position or places any order, so the
    guards that exist to stop new BUY exposure don't apply to a read-only
    lookup. Returns a small summary dict for the UI toast, or None if the
    ticker couldn't be analyzed (e.g. no price data - already-invalid tickers
    should have been caught by /api/ticker/validate before this is ever
    called, but this degrades gracefully either way)."""
    cfg = cfg or load_config()
    logger.info(f"Manual evaluation requested: {ticker}")

    mkt = MarketContext().fetch()
    regime, market_dict = _calc_regime_and_market_dict(mkt, cfg)
    trading_mode = cfg["trading"].get("mode", "SWING").lower()

    # allow_paper=False: this endpoint's contract is "read-only lookup, never
    # opens a position" (see docstring) - that now covers PAPER positions too.
    # Paper trades only happen from scheduled/manual full cycles.
    packet = _evaluate_ticker(ticker, mkt, market_dict, regime, cfg, trading_mode,
                               ticker_data_cache={}, cycle_count=cycle_count,
                               allow_paper=False)
    if not packet:
        return None

    buy_result = packet["buy_result"]
    logger.info(f"{ticker}: manual evaluation complete - score {buy_result.pct_score:.0f}%")
    return {
        "ticker": ticker,
        "should_buy": buy_result.should_buy,
        "pct_score": buy_result.pct_score,
        "regime": regime.dominant_regime,
        "rules_failed": [{"name": r.name, "detail": r.detail} for r in buy_result.rules_failed[:1]],
    }


def _has_open_pattern(ticker: str) -> bool:
    """True if a pattern_database entry is already open for this ticker - avoids
    recording a fresh entry every cycle while a BUY signal keeps firing on the
    same setup (would otherwise create dozens of near-duplicate rows per day).
    2026-07-22 (EV mode-keying fix): no longer filters mode="SWING" - patterns
    are now genuinely recorded under "DAY" or "SWING" (see record_entry call
    above), so a mode-filtered query here would miss a ticker's open DAY
    pattern and let a duplicate row get recorded for it every cycle."""
    open_patterns = db.get_patterns(ticker=ticker, closed_only=False)
    return any(not p["is_closed"] for p in open_patterns)


def _classify_hybrid_leg(td, score_result) -> str:
    """HYBRID mode buy categorization (2026-07-16, Akhil's ask: 'hybrid
    doesn't give much clarity'). A HYBRID buy is tagged DAY when it looks
    like an intraday setup judged by the day-trade standard, else SWING:

      DAY  = clears the stricter day-trade bar (threshold + 3.0, mirroring
             rules/dynamic_thresholds.py's mode_adj for DAY - a same-day
             round trip pays the spread twice so it must be visibly better)
             AND shows intraday momentum: volume running >=1.5x average or
             a >=2% move on the day.
      SWING = everything else - a trend/multi-day setup scanned at hybrid's
             faster cadence.

    Transparent and logged per trade; stored in positions.trade_mode /
    paper_trades.trade_mode so day and swing legs can be dissected
    separately in the Portfolio tab."""
    try:
        clears_day_bar = (score_result is not None
                          and score_result.final_score_pct >= score_result.threshold + 3.0)
        vol_ratio = getattr(td, "volume_ratio", 0) or 0
        chg = abs(getattr(td, "change_pct", 0) or 0)
        return "DAY" if (clears_day_bar and (vol_ratio >= 1.5 or chg >= 2.0)) else "SWING"
    except Exception:
        return "SWING"


def _latest_open_pattern_id(ticker: str):
    """The open pattern this paper buy corresponds to when record_entry()
    didn't run this cycle (signal kept firing on an already-recorded setup) -
    same most-recent-unclosed heuristic confirm_fill.py uses for real fills.
    2026-07-22 (EV mode-keying fix): no longer filters mode="SWING" - see
    _has_open_pattern's comment for why."""
    open_patterns = [p for p in db.get_patterns(ticker=ticker, closed_only=False)
                     if not p["is_closed"]]
    if not open_patterns:
        return None
    open_patterns.sort(key=lambda p: p["recorded_at"], reverse=True)
    return open_patterns[0]["id"]


def _close_due_patterns(ticker: str, current_price: float, cfg: dict):
    """Auto-closes any open pattern_database entry for this ticker once
    `pattern_hold_days` has elapsed, using the current price already fetched
    this cycle. This is a SIMULATED/time-based outcome, not a real fill -
    Robinhood stays Claude-Desktop-only, so this code never sees whether you
    actually took the trade. It's still useful signal: it answers "how did
    this setup perform" for every BUY candidate the rules engine produced,
    which is exactly what the EV engine / walk-forward / Bayesian updater need
    to learn from. See README.md for the real-trade vs. simulated-trade
    distinction."""
    hold_days = cfg["learning"].get("pattern_hold_days", 5)
    # 2026-07-22 (EV mode-keying fix): no longer filters mode="SWING" - see
    # _has_open_pattern's comment above for why a mode filter here would
    # silently strand DAY-mode patterns open forever.
    open_patterns = db.get_patterns(ticker=ticker, closed_only=False)
    # timezone-aware UTC, then dropped to naive for comparison with the
    # naive-UTC ISO strings already stored in the DB (utcnow() is
    # deprecated on Python 3.12+ and was warning on every cycle).
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc).replace(tzinfo=None)
    for p in open_patterns:
        if p["is_closed"]:
            continue
        # WATCH-mode override: if a paper position is holding this pattern,
        # its close comes from the RULE-DRIVEN paper exit (sell rules /
        # urgent Loop B), not this flat time-based fallback - that's the
        # realistic outcome the learning loop should train on.
        try:
            if db.get_open_position_by_pattern(p["id"], simulated=True):
                continue
        except Exception:
            pass
        try:
            recorded = datetime.fromisoformat(p["recorded_at"])
        except (ValueError, TypeError):
            continue
        age_days = (now - recorded).total_seconds() / 86400
        if age_days < hold_days:
            continue
        entry_price = p["features"].get("_entry_price")
        if not entry_price:
            continue
        outcome_pct = (current_price - entry_price) / entry_price * 100
        # §50: exit_kind passed explicitly rather than left to classify_exit's
        # derivation. This close is not a market event at all - the horizon
        # simply expired - so the caller is the only place that knows, and
        # saying so here keeps the classification independent of the literal
        # spelling of the reason string.
        pattern_db.close_trade(p["id"], outcome_pct, age_days * 24,
                                exit_reason="time_based_close",
                                exit_kind="time_stop")
        logger.info(f"{ticker}: pattern #{p['id']} auto-closed after {age_days:.1f}d, outcome {outcome_pct:+.2f}%")


def _prune_pending_prompts(max_age_hours: float = 48):
    """Keeps output/pending_prompts/ from growing forever."""
    now = time.time()
    if not os.path.isdir(PENDING_DIR):
        return
    for name in os.listdir(PENDING_DIR):
        fp = os.path.join(PENDING_DIR, name)
        try:
            if now - os.path.getmtime(fp) > max_age_hours * 3600:
                os.remove(fp)
        except OSError:
            continue


def _effective_scan_interval(cfg: dict) -> int:
    """DAY and HYBRID (day-trade legs mixed with swing) both need much
    tighter scan cadence than pure SWING - a day-trade setup can come and go
    within a single 15-min swing-scan interval. Falls back to
    trading.scan_interval_minutes for SWING (or anything else unrecognized).
    NOTE: this is read once at scheduler start, same limitation
    scan_interval_minutes already had - config.yaml is hot-reloadable for
    values read INSIDE run_cycle() every call, but the APScheduler cron
    trigger itself is fixed for the life of the process (see start()) and
    needs a restart to pick up a mode/interval change."""
    mode = cfg["trading"].get("mode", "SWING").upper()
    if mode in ("DAY", "HYBRID"):
        return cfg["trading"].get("day_trade_scan_interval_minutes", 5)
    return cfg["trading"]["scan_interval_minutes"]


_last_heartbeat_warn_at = 0.0  # module-level, rate-limits the missed-cycle warning below


def _check_cycle_heartbeat(interval_minutes: float):
    """2026-07-21 (prod-readiness pass): the cron scheduler can silently
    stop firing (Mac idle-sleep, a wedged BlockingScheduler thread, etc)
    with nothing in the log to flag it - see the 2026-07-21 09:00 ET
    incident where 8+ scheduled cycles were skipped in a row with zero
    indication until a human happened to check. Runs from the price-watch
    loop deliberately - a separate lightweight thread from the cron job
    itself - so this keeps checking even if the cron scheduler is the thing
    that's actually stuck. Rate-limited to one warning per 5 minutes so a
    real outage doesn't spam the log on every ~30s tick."""
    global _last_heartbeat_warn_at
    try:
        cs = db.get_cycle_status()
        next_run_at = cs.get("next_run_at")
        if not next_run_at or cs.get("is_running"):
            return
        next_dt = datetime.fromisoformat(next_run_at)
        now_dt = datetime.now(next_dt.tzinfo) if next_dt.tzinfo else datetime.utcnow()
        overdue_minutes = (now_dt - next_dt).total_seconds() / 60
        # A full extra interval (min 6 min) of grace before calling it
        # "missed" - max_instances=1/coalesce can legitimately push one
        # cycle a little late without anything being wrong.
        grace_minutes = max(interval_minutes * 2, 6)
        if overdue_minutes > grace_minutes:
            now_wall = time.time()
            if now_wall - _last_heartbeat_warn_at > 300:
                _last_heartbeat_warn_at = now_wall
                logger.warning(
                    f"Scheduled cycle appears MISSED - expected at {next_run_at}, "
                    f"now {overdue_minutes:.0f} min overdue with the market open. "
                    f"The cron scheduler may be stuck/dead (check for recent "
                    f"'apscheduler.executors.default: Running job' lines above) - "
                    f"a manual cycle can be triggered from the UI in the meantime."
                )
    except Exception as e:
        logger.debug(f"Cycle heartbeat check failed (non-fatal): {e}")


def _price_watch_loop():
    """Intra-cycle exit watch (2026-07-16, Akhil's ask: 'if there is a rapid
    drop in 5 mins that would impact a lot'). Every
    paper_trading.price_watch.interval_seconds (default 30), fetch a live
    quote for each open position and sell the moment it crosses its stop,
    target, or trailing stop - the same triggers the cycle's sell rules
    would fire, just checked every ~30s instead of every 5 minutes.

    2026-07-24 (Trinath: paper positions must keep being "monitored and
    closed as needed" no matter which mode real trading is in): the PAPER
    book is watched every iteration unconditionally now - previously gated
    to `watch or live`, so a paper/SEED position stopped getting this
    between-cycles check entirely the moment EXECUTE+auto_trade armed (it
    still got the once-per-scan-cycle sell-rule check via
    held_sim_tickers/_evaluate_ticker, just not this faster intra-cycle one).
    The REAL book is still watched, with REAL market sells placed on a
    cross, ONLY when EXECUTE+auto_trade is actually armed (`live` below) -
    that part is unchanged.

    Safety properties: market-hours gated so stale after-hours quotes can't
    fire bogus exits; config hot-reloaded every iteration
    (paper_trading.price_watch.enabled turns it off live); quote volume is
    tiny (<= total open positions per tick, well inside Alpaca's 200/min
    alongside the scans); and a race with the scan cycle is harmless -
    execute_sell/execute_sell_live close by ticker within their own book, so
    the second closer finds nothing and no-ops. The linked pattern still
    closes with the real outcome, so the learning loop sees these fast exits
    too."""
    logger.info("Price watch: intra-cycle stop/target monitor started")
    while True:
        interval = 30.0
        try:
            cfg = load_config()
            if is_market_open(cfg):
                _check_cycle_heartbeat(_effective_scan_interval(cfg))
            pw = (cfg.get("paper_trading", {}) or {}).get("price_watch", {}) or {}
            interval = max(10.0, float(pw.get("interval_seconds", 30)))
            live = live_trader.is_live_mode(cfg)
            if not pw.get("enabled", True) or not is_market_open(cfg):
                time.sleep(max(interval, 30))
                continue
            # Paper book always watched; real book joins only when
            # EXECUTE+auto_trade is armed.
            # get_MANAGED_positions, not get_all_positions (§5, 2026-07-24):
            # this loop calls check_exit_triggers() and then sells on a hit,
            # so a SYNC row reaching it would mean an ATR stop computed for a
            # $100 engine entry liquidating a real multi-thousand-dollar
            # holding between cycles.
            positions = db.get_managed_positions(simulated=True)
            if live:
                positions = positions + db.get_managed_positions(simulated=False)
            if not positions:
                time.sleep(interval)
                continue
            from mcp_clients.market_data import router as md_router
            for p in positions:
                ticker = p["ticker"]
                is_real = not p.get("simulated")
                try:
                    q = md_router.get_quote(ticker)
                    price = (q[0].get("price") if q else None)
                    if not price:
                        continue
                    db.upsert_ticker_info(ticker, last_price=price)
                    if price > (p.get("trail_high") or p.get("entry_price") or 0):
                        # §16: scoped to the book THIS position is in. The
                        # loop above mixes both books (paper positions plus
                        # live ones when `live`), so an unscoped ratchet here
                        # crossed them on every price tick.
                        db.update_trail_high(ticker, price, simulated=not is_real)
                    reason = paper_trader.check_exit_triggers(p, price, cfg)
                    if reason:
                        logger.info(f"{ticker}: [PRICE WATCH] exit trigger between cycles - {reason}")
                        if is_real and live:
                            # §D: check_exit_triggers' first token IS the kind
                            # ("stop_loss" / "take_profit" / "trailing_stop"),
                            # which is why classify_exit could already read
                            # price_watch: reasons back. Passing it explicitly
                            # means a future edit to that vocabulary breaks
                            # close_pattern's EXIT_KINDS validation loudly
                            # rather than quietly reverting these rows to NULL.
                            live_trader.execute_sell_live(
                                db, cfg, ticker,
                                reason=f"price_watch:{reason.split(' ')[0]}",
                                pattern_db=pattern_db,
                                exit_kind=reason.split(" ")[0])
                        elif not is_real:
                            paper_trader.execute_sell(
                                db, ticker, price,
                                reason=f"price_watch:{reason.split(' ')[0]}",
                                pattern_db=pattern_db, cfg=cfg,
                                exit_kind=reason.split(" ")[0])
                        # (real position, live not armed) - advisory only via
                        # the log line above; no order placed, matching the
                        # scan-cycle sell-rule path's behavior for the same case.
                except Exception as e:
                    logger.warning(f"{ticker}: price watch check failed: {e}")
        except Exception as e:
            logger.error(f"Price watch loop error: {e}", exc_info=True)
            time.sleep(60)
        time.sleep(interval)


def _log_startup_health_check():
    """One-time diagnostic at scheduler startup (2026-07-21, prod-readiness
    pass, after two real incidents that were each invisible until someone
    went looking): a too-low open-file limit (documented fd-exhaustion
    incident in service.sh, 2026-07-17) and a missing caffeinate wrapper
    (the Mac idle-sleeping through a scheduled cron window, 2026-07-21) both
    silently degrade this process with nothing in the log to point at why.
    Both are only fixable by re-running ./service.sh install if the live
    launchd plist predates the fix - this makes that checkable from
    scheduler.log directly instead of requiring `ps`/`launchctl` on the Mac
    itself. Best-effort only: any failure here is logged and swallowed, it
    must never block startup."""
    try:
        import resource  # POSIX only; Windows has no per-process fd limit
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 2048:
            logger.warning(
                f"Startup check: open-file limit is low (soft={soft}, hard={hard}) - "
                f"see service.sh's 2026-07-17 fd-exhaustion fix. If this is launchd-managed, "
                f"run ./service.sh install to pick up the raised 4096/8192 limits.")
        else:
            logger.info(f"Startup check: open-file limit soft={soft} hard={hard} (OK)")
    except ImportError:
        logger.info("Startup check: no per-process open-file limit on this platform")
    except Exception as e:
        logger.warning(f"Startup check: could not read open-file limit: {e}")

    _log_sleep_prevention_check()


def _log_sleep_prevention_check():
    """Is anything stopping this machine idle-sleeping through a scan window?

    A sleeping machine is a stopped scheduler on every OS - that is what
    happened on the morning of 2026-07-21 - and the failure is silent, so it
    has to be checked rather than assumed.

    PHASE 3 CHANGED WHO HOLDS THE LOCK. This used to walk the process ancestry
    looking for `caffeinate`, which answered the question only on macOS and
    only when the scheduler was a direct child of it. Under §47 the scheduler
    runs in a container, where a power assertion is meaningless, and
    scripts/tp_agent.py holds the wakelock on the host instead. So: inside a
    container, say who owns it and stop; outside, check the mechanism this
    platform actually uses.

    The OS-specific part is storage/platform_support.py's business, not this
    file's - the engine must stay free of host binaries so it can run in a
    container (§47.1). Best-effort: a diagnostic must never block startup."""
    try:
        from storage.platform_support import sleep_prevention_status

        held, explanation = sleep_prevention_status()
        if held is True:
            logger.info(f"Startup check: {explanation} (OK)")
        elif held is False:
            logger.warning(f"Startup check: {explanation}")
        else:
            logger.info(f"Startup check: {explanation}")
    except Exception as e:
        logger.warning(f"Startup check: could not determine sleep-prevention status: {e}")


def start():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_SCHEDULER_STARTED

    _log_startup_health_check()
    cfg = load_config()

    # §6 (Phase 1, 2026-07-24): the resolved execution posture goes into the
    # log at startup. Under launchd there is no console, and scheduler.log is
    # the only place anyone can answer "was this process able to place real
    # orders?" after the fact.
    from storage import banner
    banner.log_banner(cfg, logger)

    interval = _effective_scan_interval(cfg)
    scheduler = BlockingScheduler(timezone=ET)
    scheduler.add_job(
        run_cycle, "cron", day_of_week="mon-fri", hour="9-16", minute=f"*/{interval}",
        id="trading_cycle", max_instances=1, coalesce=True,
    )

    def _record_next_run(event=None):
        job = scheduler.get_job("trading_cycle")
        if job and job.next_run_time:
            try:
                db.set_next_cycle_time(job.next_run_time.isoformat())
            except Exception as e:
                logger.warning(f"Failed to record next cycle time: {e}")

    scheduler.add_listener(_record_next_run, EVENT_SCHEDULER_STARTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    logger.info(f"Scheduler started - every {interval} min Mon-Fri 9:00-16:30 ET "
                f"(8:00-15:30 CST - includes premarket/postmarket per current config buffers)")

    # Intra-cycle stop/target watch (see _price_watch_loop) - daemon so it
    # never blocks shutdown.
    threading.Thread(target=_price_watch_loop, daemon=True, name="price-watch").start()

    run_cycle()  # run immediately on start
    scheduler.start()


def _run_once_cli():
    """Child-process entry point for engine/cycle_supervisor.py's
    run_supervised() (2026-07-22 hard-kill fix). Runs exactly ONE cycle body
    and exits - no BlockingScheduler, no cron, no price-watch thread. Being
    its own OS process (the parent Popen()s it with start_new_session=True)
    is what lets a hard wall-clock ceiling actually terminate a wedged MCP
    call: SIGKILL-ing this whole process group takes down every uvx/npx
    subprocess this cycle spawned too, which asyncio-level cancellation
    inside mcp_clients/base.py has already been proven unable to guarantee
    (see that module's run_async() docstring: "task wouldn't cancel
    cleanly"). Deliberately does NOT go through run_cycle()'s
    set_cycle_running/finished bookkeeping - the parent process
    (run_supervised) already owns that around this child's whole lifetime,
    including the case where this process gets killed mid-cycle."""
    force = "--force" in sys.argv
    _run_cycle_impl(force=force)


if __name__ == "__main__":
    if "--run-once" in sys.argv:
        _run_once_cli()
    else:
        start()
