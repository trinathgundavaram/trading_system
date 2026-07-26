"""PostgreSQL persistence layer for the active (free, MCP-SDK-driven) pipeline.
Tables: signals, trades, positions, cycles, daily_stats.

2026-07-21 (SQLite -> Postgres migration): this file used to be a SQLite
single-file store, and the hours-long "OS-level file-open stall" hangs that
caused (see prod_readiness_plan.md - a live py-spy dump once caught every
worker thread simultaneously stuck inside a bare sqlite3.connect() with zero
exceptions raised, for 14+ minutes) is exactly why this moved to a real
client-server database with a proper connection pool. _get_pool() below
hands out an already-open TCP connection in microseconds instead of doing a
filesystem open() syscall on every single call - the entire class of "the OS
silently stalls a file open" failure this file used to carry ~150 lines of
hard-won workaround code for (_open_with_timeout, the old _conn()'s retry
ladder) simply cannot happen against a connection pool, so none of that is
needed anymore.

Everything BELOW the connection layer (all ~120 public methods) is
UNCHANGED from the SQLite version on purpose: _PGConnWrapper/_PGCursorWrapper
below mimic sqlite3.Connection's .execute()/.executemany()/.executescript()/
.row_factory interface closely enough that the business-logic methods making
up the bulk of this file never needed to be touched line-by-line - only the
connection plumbing, the schema DDL (AUTOINCREMENT -> SERIAL, a couple of
idempotent-migration simplifications Postgres makes unnecessary), and the 3
call sites that read cursor.lastrowid (Postgres has no such attribute - those
3 INSERTs now use RETURNING id instead, handled transparently by the wrapper)."""
import logging
import os
import sqlite3   # kept ONLY for the sqlite3.Row sentinel object used as a
                  # marker below (conn.row_factory = sqlite3.Row, ~47 call
                  # sites elsewhere in this file) - there is no
                  # sqlite3.connect() anywhere in this file anymore, zero
                  # functional dependency on the sqlite3 module beyond that
                  # one marker value, which costs nothing (stdlib, no I/O).
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# Postgres connection settings - override via .env (see .env.template).
# Defaults match a fresh local `brew install postgresql` with default
# trust-auth (no password) and a `trading_platform` database created once
# via migrate_to_postgres.py (or `createdb trading_platform`).
PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
PG_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
PG_DB = os.getenv("POSTGRES_DB", "trading_platform")
PG_USER = os.getenv("POSTGRES_USER") or os.getenv("USER") or "postgres"
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

# Legacy SQLite path - no longer opened by anything in this file. Kept as a
# constant only because migrate_to_postgres.py uses it as the default
# migration SOURCE, and nothing else in the app referenced db.path directly
# (verified via repo-wide grep before this migration).
DB_PATH = Path(__file__).resolve().parent.parent / "output" / "trading.db"

# ── Unmanaged position modes (§5, Phase 1, 2026-07-24 audit) ────────────────
# SYNC: engine/account_sync.py's import of REAL Robinhood holdings.
# SEED: robinhood_sync.py's clone of the real book into the paper book.
# Neither is a position this engine decided to enter, so neither is one this
# engine may decide to exit. This tuple is the single source of truth; every
# other layer that repeats the check (rules/sell_rules.py,
# engine/live_trader.py, engine/rotation.py) mirrors it deliberately rather
# than importing it, so that the purest, dependency-free modules stay
# importable without the Postgres driver. tests/test_sync_quarantine.py
# asserts the mirrors have not drifted.
MANAGED_EXCLUDED_MODES = ("SYNC", "SEED")


def is_unmanaged_mode(trade_mode) -> bool:
    """True when a position's trade_mode marks it as NOT this engine's to
    close. Case-insensitive and None-safe: a NULL trade_mode is a legacy
    engine row, which IS managed."""
    return str(trade_mode or "").upper() in MANAGED_EXCLUDED_MODES


# §14: the advisory-lock key that serialises position-opening. Any constant
# works as long as nothing else in this database picks the same one; a literal
# is used rather than hashtext('...') so the value is greppable and stable
# across Postgres versions. The second key slot carries the book (0 = live,
# 1 = paper), so the two books never wait on each other.
_OPEN_POSITION_LOCK_KEY = 0x7B0DEB17


class _PositionRaceLost(Exception):
    """Internal. Raised inside try_open_position()'s transaction so that a
    lost race rolls back any cash already debited in the same transaction,
    and caught immediately by its caller. Never escapes this module - losing
    a race is a normal outcome that the public method reports by returning
    None, not an error condition."""


def _local_day_window_utc(day: date = None) -> tuple:
    """[start, end) in naive-UTC isoformat for one LOCAL calendar day.

    Every timestamp column in this schema is written as naive
    `datetime.utcnow().isoformat()`, but a trading day is a LOCAL day: an
    evening close at 00:30 UTC belongs to the session that just ended, not to
    tomorrow. So the window has to be converted before comparing.

    Computed in Python rather than in SQL. SQLite's `date(col,'localtime')`
    read the OS timezone directly and Postgres has no equivalent; leaning on
    the Postgres SERVER's configured timezone would silently break this
    whenever that is set to anything other than the machine's local zone.
    This is a plain string-range comparison against an already-UTC column, so
    the database's timezone configuration cannot affect the answer.

    Extracted 2026-07-25 (§11) from paper_realized_pnl_today(), which is now
    one of its two callers. Its own docstring already made the argument: two
    implementations of the same window is how you get two different answers
    from the same data - and a drawdown day that disagreed with a realised-P&L
    day would be exactly that failure.
    """
    local_ref = datetime.combine(day, datetime.min.time()) if day else datetime.now()
    local_midnight = local_ref.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_offset = datetime.utcnow() - datetime.now()   # naive local->UTC on this machine
    return ((local_midnight + utc_offset).isoformat(),
            (local_midnight + timedelta(days=1) + utc_offset).isoformat())


_POOL_LOCK = threading.Lock()
_POOL = None  # process-wide pool, shared by every Database() instance in
              # this process (scheduler.py, server.py, main.py, confirm_fill.py
              # etc. each construct their own Database(), same as before -
              # this pool lives at module scope so they still share one small
              # set of real connections instead of each opening their own).


# Pool bounds. 2-20 remains the default and is what the release machine runs;
# the environment override exists because 2 is not always available (2026-07-25).
# A server with a small max_connections, a pgbouncer in front, or a single-client
# Postgres like PGlite - which scripts/rehearse_cutover.py uses precisely so the
# cutover can be rehearsed without provisioning a second server - all reject the
# second connection, and the failure surfaces as "server closed the connection
# unexpectedly" from inside Database.__init__, which reads like the server died
# rather than like a pool that asked for more than it could have.
PG_POOL_MIN = max(1, int(os.getenv("TP_PG_POOL_MIN", "2")))
PG_POOL_MAX = max(PG_POOL_MIN, int(os.getenv("TP_PG_POOL_MAX", "20")))


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = psycopg2.pool.ThreadedConnectionPool(
                minconn=PG_POOL_MIN, maxconn=PG_POOL_MAX,
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASSWORD,
                connect_timeout=10,
            )
            logger.info(f"storage.database: Postgres pool ready "
                        f"(host={PG_HOST}:{PG_PORT} db={PG_DB} user={PG_USER}, "
                        f"pool size {PG_POOL_MIN}-{PG_POOL_MAX})")
    return _POOL


class _PGCursorWrapper:
    """Thin wrapper so code written against sqlite3's cursor interface
    (cur.fetchone()/.fetchall()/.lastrowid) doesn't need to change."""
    __slots__ = ("_cur", "_returned_id", "_fetched_return")

    def __init__(self, cur):
        self._cur = cur
        self._returned_id = None
        self._fetched_return = False

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        """Postgres has no native lastrowid - the 3 call sites that need one
        now execute INSERT ... RETURNING id (see save_signal/add_pattern/
        log_rejected_signal), so the id is just the returned row's 'id'
        value. Cached so repeated access after the cursor's already been
        consumed doesn't raise. None of those 3 call sites set row_factory
        (so this normally sees a plain tuple), but handled defensively for
        dict-style rows too (RealDictRow) in case that ever changes -
        row[0] would raise KeyError on a dict row, not just return the
        wrong thing, so this isn't just belt-and-suspenders."""
        if not self._fetched_return:
            self._fetched_return = True
            try:
                row = self._cur.fetchone()
                if row is None:
                    self._returned_id = None
                elif isinstance(row, dict):
                    self._returned_id = row.get("id")
                else:
                    self._returned_id = row[0]
            except psycopg2.ProgrammingError:
                self._returned_id = None  # statement had no result set
        return self._returned_id


class _PGConnWrapper:
    """sqlite3.Connection-compatible facade over one pooled psycopg2
    connection (2026-07-21 Postgres migration - see module docstring). Lets
    every business-logic method elsewhere in this file - conn.execute(...),
    conn.row_factory = sqlite3.Row, conn.executemany(...),
    conn.executescript(...) - keep working completely unchanged. Translates
    sqlite's `?` placeholders to psycopg2's `%s` (verified via repo grep:
    this file never uses a literal '?' inside SQL text, only as a
    placeholder, so a blind replace is safe)."""
    __slots__ = ("_pg_conn", "row_factory")

    def __init__(self, pg_conn):
        self._pg_conn = pg_conn
        self.row_factory = None  # sqlite3-compatible default: plain tuples

    def _cursor(self):
        # RealDictCursor (not DictCursor): verified via repo-wide scan that
        # every one of the ~47 `row_factory = sqlite3.Row` call sites in this
        # file immediately converts via dict(row)/row["col"] - never
        # positional row[0] - so RealDictCursor's rows (genuine dict
        # subclass, unlike DictCursor's list-like DictRow) are the exact
        # match, not just a close-enough one.
        cursor_factory = psycopg2.extras.RealDictCursor if self.row_factory else None
        return self._pg_conn.cursor(cursor_factory=cursor_factory)

    @staticmethod
    def _xlate(sql: str) -> str:
        return sql.replace("?", "%s")

    @staticmethod
    def _native(v):
        """2026-07-21 (found during a real cutover): numpy scalars
        (np.float64/np.int64/np.bool_, produced throughout the scoring/
        position-management code via pandas-ta) are subclasses of the
        matching Python builtin, so old psycopg2 happily accepted them - but
        as of numpy 2.0, repr(np.float64(x)) changed from just the number to
        "np.float64(x)", and psycopg2's float adapter uses repr() internally
        for round-trip precision. The result: a numpy scalar param gets
        inlined into the SQL text as the literal (invalid) text
        "np.float64(0.85)" instead of the number 0.85, which Postgres then
        parses as a schema-qualified function call and rejects with
        'schema "np" does not exist'. Rather than hunt down and fix every
        numpy-producing call site (score_result, indicators, pandas-ta
        outputs, etc.), coerce defensively here: any object exposing
        numpy's scalar .item() method (and isn't a str/bytes, which also
        happen to define other dunder methods but never .item()) is
        converted to its native Python equivalent before reaching
        psycopg2."""
        if hasattr(v, "item") and not isinstance(v, (str, bytes, bytearray)):
            try:
                return v.item()
            except (ValueError, AttributeError):
                return v
        return v

    def _native_params(self, params):
        if isinstance(params, dict):
            return {k: self._native(v) for k, v in params.items()}
        return tuple(self._native(v) for v in params)

    def execute(self, sql, params=()):
        cur = self._cursor()
        cur.execute(self._xlate(sql), self._native_params(params))
        return _PGCursorWrapper(cur)

    def executemany(self, sql, seq_of_params):
        cur = self._cursor()
        cur.executemany(self._xlate(sql), [self._native_params(p) for p in seq_of_params])
        return _PGCursorWrapper(cur)

    def executescript(self, sql):
        # psycopg2 runs multi-statement ;-separated SQL in one execute()
        # call natively - no separate "executescript" concept needed.
        cur = self._cursor()
        cur.execute(sql)
        return _PGCursorWrapper(cur)

    def commit(self):
        self._pg_conn.commit()

    def rollback(self):
        self._pg_conn.rollback()


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    signal TEXT,
    confidence REAL,
    price REAL,
    buy_score REAL,
    buy_pct REAL,
    sell_triggered_rule TEXT,
    sell_reason TEXT,
    data_quality TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    amount REAL,
    shares REAL,
    fill_price REAL,
    order_id TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    entry_price REAL,
    entry_time TEXT,
    shares REAL,
    dollar_amount REAL,
    trail_high REAL,
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS cycles (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    cycle_num INTEGER,
    ticker_count INTEGER,
    blocked INTEGER DEFAULT 0,
    reason TEXT,
    duration REAL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    cycles_run INTEGER DEFAULT 0,
    signals_generated INTEGER DEFAULT 0,
    trades_placed INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    kill_switch_triggered INTEGER DEFAULT 0,
    -- §7/§8 (Phase 2). The paper book keeps its OWN counters rather than
    -- sharing the live ones. Two reasons: a paper session and a live session
    -- must not consume each other's daily budget, and `realized_pnl` is
    -- audited as real-money-only - a guarantee worth preserving rather than
    -- quietly widening. db.trades_placed_today()/realized_pnl_today() resolve
    -- the right column, so no caller needs to know which one it landed in.
    paper_trades_placed INTEGER DEFAULT 0,
    paper_winning_trades INTEGER DEFAULT 0,
    paper_realized_pnl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    level TEXT,
    message TEXT
);

-- ============ Learning / Analytics backend (v8.3) ============

-- Immutable indicator/rule snapshot captured at the moment of a confirmed real
-- fill (confirm_fill.py's cmd_buy/cmd_sell call save_trade_snapshot()) -
-- linked from trades.snapshot_id (see _migrate_trade_snapshot_column). This
-- table existed since early in the build but nothing called it until
-- confirm_fill.py was wired to use it - see README.
CREATE TABLE IF NOT EXISTS trade_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    signal_id TEXT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL              -- full immutable JSON blob - NEVER UPDATE after INSERT
);

CREATE TABLE IF NOT EXISTS pattern_database (
    id SERIAL PRIMARY KEY,
    trade_id TEXT,
    ticker TEXT NOT NULL,
    mode TEXT NOT NULL,              -- SWING | DAY
    recorded_at TEXT NOT NULL,
    features TEXT NOT NULL,          -- JSON dict of the 25 features (raw, pre-encoding)
    outcome_pct REAL,                -- NULL until the trade closes
    hold_hours REAL,
    exit_reason TEXT,                -- human-readable sentence, shown in the UI
    -- §50 (Phase 2.5, migrations/009). The COUNTABLE companion to exit_reason:
    -- one of rules/common.py's EXIT_KINDS, or NULL for "not determinable".
    -- exit_reason interpolates prices into itself, so every stop-loss exit is
    -- its own distinct string and the column cannot be grouped on. NULL here
    -- means unclassified, not unclosed - filter `exit_kind IS NOT NULL`.
    exit_kind TEXT,
    is_closed INTEGER DEFAULT 0,
    -- Provenance (§17, migrations/001 + 003). Which build and which scoring
    -- configuration produced this row. Two rows with different
    -- config_fingerprints came from different strategies and must not be
    -- pooled, however close their recorded_at values are.
    app_version TEXT,
    engine_version TEXT,
    config_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS bayesian_weekly_tracker (
    week_start TEXT PRIMARY KEY,
    total_weight_change_pct REAL DEFAULT 0,
    pending_changes TEXT,             -- JSON of changes blocked by the weekly cap
    cap_hit_at TEXT
);

CREATE TABLE IF NOT EXISTS bayesian_monthly_tracker (
    month_start TEXT PRIMARY KEY,
    total_weight_change_pct REAL DEFAULT 0,
    cap_hit_at TEXT
);

CREATE TABLE IF NOT EXISTS bayesian_weight_history (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    bucket TEXT,
    old_weight REAL,
    new_weight REAL,
    change_pct REAL,
    occurrences INTEGER,
    win_rate_when_fired REAL,
    overall_win_rate REAL,
    applied INTEGER DEFAULT 1,       -- 0 if blocked by a cap/gate
    block_reason TEXT
);

CREATE TABLE IF NOT EXISTS champion_challenger (
    id TEXT PRIMARY KEY,
    challenger_start TEXT,
    challenger_config TEXT,          -- JSON of challenger rule weights
    champion_trades INTEGER DEFAULT 0,
    challenger_trades INTEGER DEFAULT 0,
    champion_wins INTEGER DEFAULT 0,
    challenger_wins INTEGER DEFAULT 0,
    champion_pnl_pct REAL DEFAULT 0,
    challenger_pnl_pct REAL DEFAULT 0,
    status TEXT DEFAULT 'running',   -- running | promoted | discarded
    statistical_significance REAL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS override_analytics (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    override_type TEXT,              -- approve | deny | size | stop | exit
    system_recommendation TEXT,      -- JSON
    user_action TEXT,                -- JSON
    outcome_pct REAL,
    system_would_have_pct REAL,
    override_improved INTEGER,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_signals (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    reject_stage TEXT,               -- which of the 10 framework steps rejected it
    reject_reason TEXT,
    score_at_rejection REAL,
    price_at_rejection REAL,
    simulated_outcome_pct REAL,      -- filled in later by opportunity_cost.py
    simulated_at TEXT
);

-- ============ Position Management (Phase 3) ============

CREATE TABLE IF NOT EXISTS re_entry_cooldowns (
    ticker TEXT PRIMARY KEY,
    exit_time TEXT,
    cooldown_until TEXT,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS mae_mfe_data (
    -- §C1 (migrations/012). Was `id TEXT PRIMARY KEY` holding uuid4 strings
    -- and `trade_id TEXT` holding a stringified positions.id with no FK - so
    -- nothing stopped trade_id='1' being claimed by five different tickers at
    -- once, which is exactly what the 2026-07-25 snapshot contained.
    --
    -- ON DELETE SET NULL, not CASCADE: reset_paper_account() deletes every
    -- simulated position by design and deliberately does NOT delete excursion
    -- rows. CASCADE would make the reset silently destroy history the reset's
    -- own docstring promises to keep. A trade's maximum adverse excursion
    -- stays true after its position row is gone; what stops being true is
    -- which position it was, and that is what NULL says.
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    trade_id INTEGER REFERENCES positions (id) ON DELETE SET NULL,
    ticker TEXT,
    setup_type TEXT,
    regime TEXT,
    mae_pct REAL,
    mfe_pct REAL,
    outcome_pct REAL,
    hold_hours REAL,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS monitoring_alerts (
    id TEXT PRIMARY KEY,
    alert_type TEXT,
    severity TEXT,                   -- LOW | MEDIUM | HIGH | CRITICAL
    message TEXT,
    triggered_at TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    resolution TEXT
);

-- ============ Ticker info cache (company names, validation) ============
-- Populated two ways: (1) opportunistically every scan cycle - scheduler.py
-- already fetches yfinance info for every watchlist ticker via
-- engine/ticker_analyzer.py, which now also captures company_name, so this
-- costs zero extra MCP calls for tickers already being scanned; (2) a live
-- yfinance_get_ticker_info call when validating a NEW ticker before adding it
-- to the watchlist (server.py's /api/ticker/validate), for a ticker that
-- hasn't been scanned yet. Company names essentially never change, so no TTL
-- eviction - a stale name is not a real-world problem here.
CREATE TABLE IF NOT EXISTS ticker_info_cache (
    ticker TEXT PRIMARY KEY,
    company_name TEXT,
    last_price REAL,
    valid INTEGER DEFAULT 1,
    updated_at TEXT
);

-- ============ Portfolio risk manager support ============
-- Records the dollar amount / sector / theme / beta the Portfolio Risk
-- Manager (engine/portfolio_risk.py) attributed to each open position at
-- evaluation time, purely for the UI/journal to show WHY a size was reduced
-- or a candidate was flagged - the live check itself is always recomputed
-- fresh from ticker_info_cache + the open positions table, this table is
-- history only, never read back into a live decision.
CREATE TABLE IF NOT EXISTS portfolio_risk_log (
    id SERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    sector TEXT,
    themes TEXT,                 -- JSON list
    sector_exposure_pct REAL,
    theme_exposure_pct REAL,
    portfolio_beta REAL,
    max_pairwise_correlation REAL,
    high_vol_position_count INTEGER,
    size_multiplier REAL,
    blocked INTEGER DEFAULT 0,
    reasons TEXT                 -- JSON list
);

-- ============ Weight-change provenance ("git commit for trading logic") ============
-- Every time learning/bayesian_updater.py's apply_bucket_weight_to_config()
-- is called - whether it succeeds, is blocked by the shadow-validation gate,
-- or is force-applied bypassing that gate - this permanently records the
-- FULL context behind the decision, not just old_weight->new_weight
-- (bayesian_weight_history already has that). Six months from now, "why did
-- we change TREND from 21% to 23%?" is answerable from this table alone,
-- without relying on memory or reconstructing state from scattered logs.
CREATE TABLE IF NOT EXISTS weight_change_log (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    bucket TEXT NOT NULL,
    mode TEXT NOT NULL,
    old_weight REAL,
    new_weight REAL,
    strategy_version TEXT,        -- JSON - learning/model_versioning.py snapshot at decision time
    config_hash TEXT,             -- sha256[:16] of config.yaml at decision time
    feature_ranking TEXT,         -- JSON - analytics/feature_importance.py snapshot at decision time
    walk_forward_report TEXT,     -- JSON - latest learning_runs row at decision time
    champion_challenge_id TEXT,   -- FK-ish to champion_challenger.id, NULL if none (e.g. force-applied)
    trade_count INTEGER,          -- closed trades supporting this decision
    decision TEXT,                -- accepted | rejected | forced
    decision_reason TEXT
);

-- ============ Full decision-context snapshots (execution replay) ============
-- Extends the `signals` table (see _migrate_decision_context_columns) with
-- everything computed about a candidate this cycle beyond the bucket
-- breakdown already stored - threshold math, EV lookup, execution quality,
-- suggested position size, portfolio risk, and the regime/asset-class this
-- decision was made under. This is what makes "why did the system buy NVDA
-- on 2026-07-14?" answerable from stored data instead of needing to
-- re-derive it live - see analytics/decision_replay.py.

-- ============ Latest regime snapshot (cross-process) ============
-- engine/regime_engine.py's current_state() is a module-level singleton -
-- it's only ever populated in whichever process calls calculate()
-- (scheduler.py, once per cycle). server.py runs as a SEPARATE process and
-- would see current_state() as permanently None even after scheduler.py has
-- been calculating a real regime for hours, since Python globals aren't
-- shared across processes. This single-row table is how server.py reads the
-- real latest regime instead.
CREATE TABLE IF NOT EXISTS latest_regime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT,
    dominant_regime TEXT,
    bull_pct REAL,
    bear_pct REAL,
    choppy_pct REAL,
    transition_probability REAL,
    crisis_active INTEGER,
    confidence_gap REAL,
    confidence_level TEXT,
    confidence_score REAL,
    regime_version TEXT
);

-- ============ News headlines (per-ticker, real data, persisted) ============
-- 2026-07-14: engine/ticker_analyzer.py has been fetching and sentiment-
-- scoring real yfinance headlines every cycle (feeds the SENTIMENT_MACRO
-- bucket's news_multiplier) since long before this table existed - they
-- were computed and thrown away, never persisted or shown anywhere. This is
-- what the News tab reads from. UNIQUE(ticker, headline) dedupes the same
-- real story reappearing across many consecutive cycles into one row.
CREATE TABLE IF NOT EXISTS news_items (
    id SERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    company_name TEXT,
    headline TEXT NOT NULL,
    sentiment_score REAL,
    sentiment_label TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    UNIQUE(ticker, headline)
);
CREATE INDEX IF NOT EXISTS idx_news_items_last_seen ON news_items(last_seen_at);

-- ============ Cross-process cycle-running status ============
-- scheduler.py (the scan loop, cron-triggered) and server.py (the UI's
-- process, triggering MANUAL runs via /api/cycle/run_now) are SEPARATE
-- processes - server.py's in-memory _manual_cycle_lock only knows about
-- runs IT triggered, not scheduler.py's own scheduled cycles. This
-- single-row table is how the UI finds out ANY cycle - scheduled or
-- manual - is currently in progress, the same cross-process pattern
-- latest_regime/ui_events already use elsewhere in this file.
CREATE TABLE IF NOT EXISTS cycle_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    is_running INTEGER DEFAULT 0,
    started_at TEXT,
    triggered_by TEXT,
    finished_at TEXT,
    next_run_at TEXT,
    stage TEXT,
    tickers_total INTEGER,
    tickers_done INTEGER
);

-- ============ Real-time UI push (Phase: real-time push + notifications) ============
-- scheduler.py and server.py run as SEPARATE processes (see server.py's
-- broadcast() docstring) so scheduler.py can't call server.py's in-memory
-- broadcast() directly. This table is the cross-process outbox: scheduler.py
-- INSERTs an event after anything the UI should know about immediately
-- (cycle complete, high-conviction buy signal, urgent Loop B exit); server.py
-- polls for new rows (see server.py's _event_poll_loop) and pushes them over
-- the already-open /ws connections.
CREATE TABLE IF NOT EXISTS ui_events (
    id SERIAL PRIMARY KEY,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- cycle_complete | buy_signal | urgent_exit
    payload TEXT NOT NULL            -- JSON
);

-- ============ Learning-loop automation (scheduler-triggered) ============
-- Records each time scheduler.py auto-runs learning/walk_forward.py +
-- champion/challenger evaluation, so the UI's Learning tab has something to
-- show without anyone needing to run these in a Python shell manually.
-- Nothing here is ever auto-APPLIED (see learning/walk_forward.py's own
-- docstring) - this table stores proposals for human review, not decisions.
CREATE TABLE IF NOT EXISTS learning_runs (
    id SERIAL PRIMARY KEY,
    run_at TEXT NOT NULL,
    trigger_reason TEXT,
    mode TEXT,
    n_patterns INTEGER,
    proposals TEXT,                  -- JSON: run_walk_forward() output (attribution + stability per rule)
    challenges_evaluated TEXT        -- JSON list: any running champion/challenger evaluate() results
);

-- ============ Historical replay / backtest runs (2026-07-23) ============
-- engine/backtest_engine.py's Stage 1 market-data-only replay (see that
-- module's docstring) - runs rules/hard_vetoes.py + rules/swing_buy_rules.py
-- unmodified against historical bars instead of a separately-maintained
-- "backtest strategy". Triggered weekly by engine/backtest_loop.py (same
-- background-thread pattern as learning_runs above) or on-demand via the
-- Learning tab's "Run Backtest Now" button (server.py's POST
-- /api/backtest/run). status lets the UI show "running" while a multi-ticker/
-- multi-month replay is still in progress, and stops a second run from
-- starting concurrently (get_running_backtest_run()).
CREATE TABLE IF NOT EXISTS backtest_runs (
    id SERIAL PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,             -- running | completed | failed
    triggered_by TEXT,                -- 'weekly_auto' | 'manual'
    tickers TEXT,                     -- JSON list
    start_date TEXT,
    end_date TEXT,
    n_scored INTEGER,
    veto_counts TEXT,                 -- JSON
    summary TEXT,                     -- JSON: engine/backtest_engine.py's summarize() output
    trades TEXT,                      -- JSON: full per-trade list
    config TEXT,                      -- JSON: risk_level/thresholds/mode used for this run
    error TEXT,
    output_dir TEXT                   -- where results.json/summary.md were written
);

-- ============ Screener candidate persistence/aging ============
-- engine/screener.py: tracks a candidate's history ACROSS scan cycles, not
-- just within one. A ticker appearing for the 3rd straight cycle with a
-- rising discovery score is meaningfully different evidence than a one-off
-- appearance (deployment-review finding: "that persistence is valuable").
-- One row per (ticker, mode) - reset only when a ticker stops appearing for
-- longer than engine/screener.py's staleness window (see _load_history()).
CREATE TABLE IF NOT EXISTS screener_candidates (
    ticker TEXT NOT NULL,
    mode TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1,
    last_score REAL,
    best_score REAL,
    PRIMARY KEY (ticker, mode)
);

-- ============ Missed Opportunity Report ============
-- "Sometimes your biggest gains come from studying the trades you didn't
-- take." One row per signals-table HOLD row that was actually SCORED (not
-- hard-vetoed - those have no bucket data), caching the simulated forward
-- price outcome so analytics/missed_opportunity.py doesn't re-hit yfinance
-- every report call. signal_id is the PK (1:1 with a signals row) rather
-- than a separate autoincrement id, since re-evaluating the SAME signal
-- should overwrite, not duplicate.
CREATE TABLE IF NOT EXISTS missed_opportunity_outcomes (
    signal_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    hold_days INTEGER,
    entry_price REAL,
    would_have_returned_pct REAL,   -- return at the hold_days bar (comparable to a real simulated close)
    peak_return_pct REAL,           -- best return reached ANY time in the window - the "you left this on the table" number
    peak_at_days INTEGER,
    trough_return_pct REAL,
    trough_at_days INTEGER,
    still_pending INTEGER DEFAULT 0 -- not enough calendar time has passed yet to fill hold_days of forward bars
);

-- ============ Regret Analysis ============
-- Every closed pattern_database trade -> was the exit too early/too
-- conservative relative to what the ticker did afterward? pattern_id is the
-- PK (1:1 with a pattern_database row).
CREATE TABLE IF NOT EXISTS regret_analysis (
    pattern_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    entry_price REAL,
    exit_price REAL,
    exit_reason TEXT,
    forward_window_days INTEGER,
    highest_afterwards REAL,
    lowest_afterwards REAL,
    regret_pts REAL,                -- max(0, highest_afterwards - exit_price)
    regret_pct REAL,                -- regret_pts / entry_price * 100
    downside_avoided_pts REAL,      -- max(0, exit_price - lowest_afterwards) - a GOOD-exit signal, not regret
    downside_avoided_pct REAL,
    classification TEXT,
    still_maturing INTEGER DEFAULT 0
);

-- ============ Threshold regret analysis (2026-07-23) ============
-- "Collect every signal that looks like this - high-quality stock score but
-- rejected because of dynamic threshold adjustments - and analyze their
-- subsequent returns" (OXY review, Trinath). One row per periodic run of
-- analytics/missed_opportunity.py's evaluate_threshold_regret() (triggered
-- weekly by engine/learning_loop.py's maybe_run_threshold_regret(), same
-- background-thread pattern as learning_runs/backtest_runs above) - stores
-- the full bucketed snapshot (by adjustment-size bucket AND the
-- breadth-double-counting isolation) so the UI can show how the picture
-- develops as more HOLD signals mature, not just the latest read. Nothing
-- here is ever auto-applied - same posture as learning_runs.
CREATE TABLE IF NOT EXISTS threshold_regret_runs (
    id SERIAL PRIMARY KEY,
    run_at TEXT NOT NULL,
    trigger_reason TEXT,
    n_signals INTEGER,
    n_evaluated INTEGER,
    n_still_pending INTEGER,
    report TEXT                      -- JSON: full evaluate_threshold_regret() return dict
);
"""


class Database:
    def __init__(self, path: Path = None):
        # 2026-07-20 (production incident, Trinath caught it from a stray
        # "TEST" trade in the UI): this used to be `path: Path = DB_PATH` -
        # a mutable-default-style gotcha where the default is bound to
        # module-level DB_PATH's value ONCE, at class-definition/import
        # time. Reassigning `database.DB_PATH = <scratch path>` afterward
        # (e.g. an isolated test) does NOT change that already-bound
        # default - Database() with no explicit path still silently opened
        # the REAL output/trading.db. That's exactly what happened running
        # a supposedly-isolated integration test for the entry_signal_score
        # seeding fix: it wrote a real TEST buy+sell into production,
        # including a real (if small) hit to paper_account's realized P&L,
        # which has since been cleaned up. Evaluating module DB_PATH here,
        # inside the function body, re-reads the current value on every
        # call instead of freezing it at import time - `database.DB_PATH =
        # X; Database()` now actually does what it looks like it does.
        path = Path(path) if path is not None else Path(DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        # 2026-07-17 (round 3 of hang forensics): this used to be acquired
        # around EVERY `_conn()` call (`with self._lock, self._conn() as
        # conn:`, ~65 call sites) - which meant one slow/stalled DB open (see
        # _open_with_timeout's docstring - up to ~60s across its 4 retries
        # when the OS-level stall is persistent, which live logs show it can
        # be) blocked EVERY OTHER thread's DB access in the whole process,
        # not just the caller doing the slow open. scheduler.py holds one
        # singleton Database() shared by every ticker worker + the
        # price-watch thread, so this single-process lock is exactly why a
        # py-spy dump caught ALL of them wedged on the same line at once -
        # they were queued behind each other, not independently stuck.
        # Removed from every call site: each _conn() call already opens its
        # OWN fresh connection (nothing here shares one connection across
        # threads), so SQLite's own file-level locking + the existing
        # timeout=30/retry-with-backoff loop already in _conn() is what
        # actually protects concurrent access - this Python lock never
        # protected cross-PROCESS access anyway (server.py makes a fresh
        # Database() per request with its own unshared Lock()), so dropping
        # it here doesn't remove any safety this app could actually rely on,
        # it only removes serialization that was making one slow open freeze
        # unrelated threads. Left defined (unused internally) since
        # robinhood_sync.py still references db._lock directly.
        self._lock = threading.Lock()
        self.init_db()

    def init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate_version_columns(conn)
            self._migrate_position_columns(conn)
            self._migrate_signal_detail_columns(conn)
            self._migrate_trade_snapshot_column(conn)
            self._migrate_cycle_trigger_column(conn)
            self._migrate_exit_engine_columns(conn)
            self._migrate_ticker_info_risk_columns(conn)
            self._migrate_decision_context_columns(conn)
            self._migrate_ticker_health_columns(conn)
            self._migrate_screener_outcome_columns(conn)
            self._add_column_if_missing(conn, "cycle_status", "next_run_at", "TEXT")
            self._migrate_market_mood_columns(conn)
            self._add_column_if_missing(conn, "cycle_status", "stage", "TEXT")
            self._add_column_if_missing(conn, "cycle_status", "tickers_total", "INTEGER")
            self._add_column_if_missing(conn, "cycle_status", "tickers_done", "INTEGER")
            self._add_column_if_missing(conn, "cycle_status", "cancel_requested", "INTEGER DEFAULT 0")
            # 2026-07-22 (hard-kill fix, Trinath: "any hang has to be auto
            # killed... cancel run should be able to do all this as well"):
            # pid is the CHILD PROCESS running the actual cycle body (see
            # engine/cycle_supervisor.py) - a cross-process handle so ANY
            # process (server.py's /api/cycle/cancel included) can signal it
            # directly, same pattern as everything else in this table.
            # kill_reason records WHY a cycle ended abnormally
            # ("timeout_15min" / "user_cancel") so it's visible in the UI/DB
            # instead of looking like a normal finish.
            self._add_column_if_missing(conn, "cycle_status", "pid", "INTEGER")
            self._add_column_if_missing(conn, "cycle_status", "kill_reason", "TEXT")
            self._migrate_paper_trading(conn)
            self._migrate_rotation_log(conn)
            self._migrate_phase1_columns(conn)

        # LAST, and deliberately on its own transaction. This is the only
        # statement in init_db that can legitimately fail on existing DATA
        # rather than on schema, and in Postgres a failed statement aborts the
        # whole transaction it is in - so running it above would mean one
        # pre-existing duplicate position silently rolled back every migration
        # that preceded it. See the method for what it does when it fails.
        self._ensure_open_position_uniqueness()

    def _migrate_phase1_columns(self, conn):
        """Phase 1 (§5, §17). Mirrors migrations/002 and 003 so that a FRESH
        database converges to the same schema as a migrated one.

        The .sql files remain the reviewable record - they carry the backward
        SQL that migrations/README requires, and 002 additionally performs the
        one-time data quarantine, which is a data change and therefore
        deliberately NOT repeated here. This method is schema only, and every
        statement is idempotent."""
        # §17: pattern provenance. 001 added app_version via SQL; add it here
        # too so a fresh database that never ran 001 still has it.
        for col in ("app_version", "engine_version", "config_fingerprint"):
            self._add_column_if_missing(conn, "pattern_database", col, "TEXT")
        # §5: preserved stop machinery for quarantined SYNC/SEED rows.
        self._add_column_if_missing(conn, "positions", "quarantined_stop_price", "REAL")
        self._add_column_if_missing(conn, "positions", "quarantined_stop_state", "TEXT")
        self._add_column_if_missing(conn, "positions", "quarantined_at", "TEXT")
        # §7/§8: per-book daily counters. Mirrors migrations/004.
        self._add_column_if_missing(conn, "daily_stats", "paper_trades_placed",
                                     "INTEGER DEFAULT 0")
        self._add_column_if_missing(conn, "daily_stats", "paper_winning_trades",
                                     "INTEGER DEFAULT 0")
        self._add_column_if_missing(conn, "daily_stats", "paper_realized_pnl",
                                     "REAL DEFAULT 0")
        # §11: drawdown. `max_drawdown` already existed (declared, defaulted to
        # 0, written by nothing since the schema was created). The other three
        # are new. Book-separated for the same reason as the counters above:
        # the only equity curve that exists today is the paper one, and writing
        # it into a column the live ledger reads is the contamination §7 exists
        # to prevent. Mirrors migrations/005.
        self._add_column_if_missing(conn, "daily_stats", "running_drawdown",
                                     "REAL DEFAULT 0")
        self._add_column_if_missing(conn, "daily_stats", "paper_max_drawdown",
                                     "REAL DEFAULT 0")
        self._add_column_if_missing(conn, "daily_stats", "paper_running_drawdown",
                                     "REAL DEFAULT 0")
        # §15: learning-data quarantine. Mirrors migrations/007's SCHEMA only.
        # The sweep that classifies existing rows is a DATA change and stays in
        # the .sql file, deliberately - re-running a data classification on
        # every process start would re-quarantine rows an operator had
        # examined and cleared by hand.
        self._add_column_if_missing(conn, "mae_mfe_data", "data_quality",
                                     "TEXT DEFAULT 'ok'")
        self._add_column_if_missing(conn, "pattern_database", "data_quality",
                                     "TEXT DEFAULT 'ok'")
        # §18: the dollar amount a declined trade would have taken. Without it
        # a rejected_signals row is a note rather than a counterfactual -
        # "what did skipping this cost?" has no denominator. Mirrors
        # migrations/008.
        self._add_column_if_missing(conn, "rejected_signals", "would_have_size", "REAL")

    def _migrate_rotation_log(self, conn):
        """Portfolio Rotation Engine (engine/rotation.py, 2026-07-17): one row
        per executed rotation (weakest holding closed to make room for a
        top-tier new candidate at max_positions). Durable on purpose - the
        weekly rotation budget (rotation.max_rotations_per_week) is counted
        from this table, so a scheduler restart can't reset the budget the
        way an in-memory counter (or the hourly-pruned ui_events table)
        would. book: 'PAPER' | 'LIVE', counted separately."""
        conn.execute("""CREATE TABLE IF NOT EXISTS rotation_log (
            id SERIAL PRIMARY KEY,
            executed_at TEXT NOT NULL,
            book TEXT NOT NULL,
            candidate_ticker TEXT NOT NULL,
            candidate_score REAL,
            victim_ticker TEXT NOT NULL,
            victim_health REAL,
            victim_days_held REAL,
            reason TEXT
        )""")

    def _migrate_paper_trading(self, conn):
        """WATCH-mode paper trading (2026-07-16, Akhil's ask): when
        trading.watch_execute == WATCH the scheduler mimics every BUY signal
        as a real trade - simulated positions live in the same `positions`
        table (simulated=1) so sell_rules/Loop B manage them identically to
        real ones, and a single-row `paper_account` tracks the purse (cash
        out on buy, cash back on sell). `paper_trades` is the immutable
        buy/sell ledger for the UI + P/L review. Real rows (confirm_fill.py)
        have simulated=0/NULL - every book-sensitive query COALESCEs."""
        self._add_column_if_missing(conn, "positions", "simulated", "INTEGER DEFAULT 0")
        # Which trading mode (SWING/DAY/HYBRID) was active when this buy was
        # made (2026-07-16, Akhil's ask) - so every trade can be attributed
        # to the strategy category it was bought under. NULL on rows that
        # predate this column; 'SEED' on positions cloned from the real book.
        self._add_column_if_missing(conn, "positions", "trade_mode", "TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            starting_cash REAL NOT NULL,
            cash REAL NOT NULL,
            realized_pnl REAL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL,
            shares REAL,
            dollar_amount REAL,
            reason TEXT,
            pattern_id INTEGER,
            pnl REAL,
            pnl_pct REAL,
            created_at TEXT
        )""")
        # (after CREATE, so a fresh DB has the table before ALTER runs)
        self._add_column_if_missing(conn, "paper_trades", "trade_mode", "TEXT")
        # One row per WATCH-mode scan cycle - the portfolio equity curve the
        # UI's Portfolio tab plots ("change in portfolio over time").
        conn.execute("""CREATE TABLE IF NOT EXISTS paper_equity_history (
            id SERIAL PRIMARY KEY,
            timestamp TEXT NOT NULL,
            total_value REAL,
            cash REAL,
            invested_cost REAL,
            market_value REAL,
            unrealized_pnl REAL,
            realized_pnl REAL,
            n_open INTEGER
        )""")

    def _ensure_open_position_uniqueness(self):
        """The §14 invariant, as a structural guarantee: at most one OPEN
        position per (ticker, book). Mirrors migrations/006.

        Partial (WHERE status = 'open') so the history of closed positions in
        the same ticker is unaffected - the constraint is about what is held
        now, not about what was ever held.

        Creating it can FAIL, on exactly one condition: the table already
        contains duplicates. That is not a reason to take the process down -
        scheduler.py, server.py and main.py each construct a Database() at
        startup, and refusing to boot would take the UI with it, including the
        page an operator would use to look at the duplicates. So it logs
        CRITICAL with the query that finds them and carries on WITHOUT the
        index, which means the race stays open until someone reconciles the
        book.

        scripts/reconcile.py (§15) checks for the index's absence directly, so
        this cannot degrade quietly into a log line nobody reads.
        """
        try:
            with self._conn() as conn:
                conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS uq_open_position_per_ticker_book
                       ON positions (ticker, (COALESCE(simulated, 0)))
                       WHERE status = 'open'""")
        except Exception as e:
            logger.critical(
                "§14: could NOT create uq_open_position_per_ticker_book - the "
                "duplicate-position race is still open on this database. "
                "Almost certainly duplicates already exist; find them with:\n"
                "  SELECT ticker, COALESCE(simulated,0) AS book, COUNT(*) "
                "FROM positions WHERE status='open' GROUP BY 1,2 HAVING COUNT(*) > 1;\n"
                f"  underlying error: {e}")

    def _migrate_market_mood_columns(self, conn):
        """News tab follow-up (2026-07-14) - latest_regime gains fear/greed +
        VIX + macro-blackout fields so server.py can show current market
        mood without a new table (see save_latest_regime()'s docstring)."""
        new_cols = {
            "fear_greed_score": "INTEGER", "fear_greed_rating": "TEXT",
            "vix_level": "REAL", "hours_to_next_macro": "REAL",
            "blackout_active": "INTEGER", "blackout_reason": "TEXT",
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "latest_regime", col, coltype)

    def _add_column_if_missing(self, conn, table: str, col: str, coltype: str):
        """2026-07-21 (Postgres migration): unlike SQLite, Postgres supports
        ADD COLUMN IF NOT EXISTS natively and handles the same
        multiple-processes-racing-to-migrate scenario the old SQLite version's
        docstring describes (main.py/scheduler.py/server.py each construct
        their own Database() at roughly the same time) atomically at the
        database level - no PRAGMA table_info() probe or duplicate-column
        exception handling needed anymore, the database itself makes this
        both idempotent and race-safe."""
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}")

    def _migrate_version_columns(self, conn):
        """ALTER TABLE additions for the 6 model-version fields + VIX percentiles
        (CREATE TABLE IF NOT EXISTS won't add columns to a signals table created
        by an earlier build)."""
        new_cols = {
            "rule_engine_version": "TEXT", "weight_version": "TEXT", "regime_version": "TEXT",
            "prompt_version": "TEXT", "threshold_version": "TEXT", "pattern_db_version": "TEXT",
            "vix_percentile_1y": "REAL", "vix_percentile_3m": "REAL",
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "signals", col, coltype)

    def _migrate_position_columns(self, conn):
        """Links a position row back to the pattern_database entry that
        produced it, so confirm_fill.py can close the pattern with the real
        outcome instead of the time-based simulated one. Also adds every
        Position Management Engine (Phase 3) field - all nullable, all
        defaulted to 0/False/None until confirm_fill.py or position_management.py
        populates them for a real, currently-open position."""
        new_cols = {
            "pattern_id": "INTEGER",
            "stop_state": "TEXT",
            "current_stop_price": "REAL",
            "current_target_price": "REAL",
            "max_adverse_excursion_pct": "REAL",
            "max_favorable_excursion_pct": "REAL",
            "high_watermark_price": "REAL",
            "exit_stage_reached": "INTEGER",
            "current_profit_r": "REAL",
            "entry_signal_score": "REAL",
            "entry_p_win": "REAL",
            "entry_ev": "REAL",
            "entry_regime": "TEXT",
            "setup_type": "TEXT",
            "entry_rs_percentile": "REAL",
            "entry_ad_ratio": "REAL",
            # §53 (Phase 2.5, migrations/011). ATR as a % of price at entry.
            # engine/portfolio_risk.py counts "how many high-volatility
            # positions are already open" against
            # portfolio_risk.high_vol_atr_pct_threshold, and had nothing to
            # count with: ATR was computed live per cycle and never persisted,
            # so it substituted stop distance as a proxy - a DIFFERENT
            # QUANTITY compared against a threshold expressed in ATR units.
            # See _position_atr_pct() for what that cost.
            "entry_atr_pct": "REAL",
            "risk_per_share": "REAL",
            "position_health_score": "REAL",
            "prev_cycle_pnl_pct": "REAL",
            "days_held": "REAL",
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "positions", col, coltype)

    def _migrate_exit_engine_columns(self, conn):
        """Previous-cycle readings for the unified 6-bucket Exit Score engine
        (rules/exit_scorer.py) - lets MOMENTUM_WEAKNESS/TREND_DETERIORATION
        detect a real trend/momentum ROLLOVER (this cycle's reading vs last
        cycle's) instead of only a static level. All nullable; a position's
        first Loop B cycle after entry has no prior reading yet, so those
        rules simply contribute 0 (neutral) until the second cycle."""
        new_cols = {
            "prev_cycle_adx": "REAL",
            "prev_cycle_macd_hist": "REAL",
            "prev_cycle_stoch_k": "REAL",
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "positions", col, coltype)

    def _migrate_ticker_health_columns(self, conn):
        """Data Provenance Circuit Breaker follow-up: tracks how many
        CONSECUTIVE cycles in a row a ticker's data has tripped (or nearly
        tripped) rules/hard_vetoes.py's veto #16 - see
        db.record_ticker_data_health() and scheduler.py's _evaluate_ticker().
        A single stale cycle is normal (network blip); several in a row
        usually means something structural (wrong symbol, delisted, a data
        source that just doesn't cover this name) worth a human's attention."""
        new_cols = {
            "consecutive_stale_cycles": "INTEGER DEFAULT 0",
            "last_stale_at": "TEXT",
            "last_healthy_at": "TEXT",
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "ticker_info_cache", col, coltype)

    def _migrate_screener_outcome_columns(self, conn):
        """Screener learning follow-up (2026-07-14): screener_candidates
        previously only recorded DISCOVERY-time stats (times_seen/best_score,
        from engine/screener.py's own ranking pass) - never what happened
        once the REAL scoring engine (rules/swing_buy_rules.py, via
        scheduler.py's _evaluate_ticker) actually looked at the candidate.
        That meant a chronically-stale-data or never-qualifying ticker could
        keep winning quota slots forever just for reappearing. These columns
        close that loop - see db.record_screener_outcome() (write) and
        engine/screener.py's _persistence_bonus()/get_low_quality_screener_tickers()
        (read)."""
        new_cols = {
            "n_scored": "INTEGER DEFAULT 0",             # cycles this ticker was actually run through scoring
            "n_qualified": "INTEGER DEFAULT 0",           # of those, how many came back BUY
            "n_stale_data_blocked": "INTEGER DEFAULT 0",  # of those, how many hit veto #16 (STALE_DATA_CIRCUIT_BREAKER)
            "sum_buy_pct": "REAL DEFAULT 0.0",            # running sum, /n_buy_pct_samples = avg_buy_pct
            "n_buy_pct_samples": "INTEGER DEFAULT 0",     # only incremented when scoring actually produced a pct (not vetoed)
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "screener_candidates", col, coltype)

    def _migrate_ticker_info_risk_columns(self, conn):
        """sector/beta for the Portfolio Risk Manager (engine/portfolio_risk.py) -
        populated the SAME opportunistic way company_name/last_price already
        are (scheduler.py's per-cycle db.upsert_ticker_info call, zero extra
        MCP calls since td.sector/td.beta are already fetched by
        engine/ticker_analyzer.py for every watchlist ticker). Lets the
        Portfolio Risk Manager look up an OPEN position's sector/beta without
        re-fetching it - open positions are often off the current watchlist
        (e.g. confirm_fill.py'd manually), so this cache is the only place
        that data persists across cycles for those tickers."""
        # §18 adds `industry`: sector alone left the concentration check too
        # coarse to bind. "Technology" covers a semiconductor foundry and a
        # payments processor, which do not move together.
        new_cols = {"sector": "TEXT", "beta": "REAL", "industry": "TEXT"}
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "ticker_info_cache", col, coltype)

    def _migrate_decision_context_columns(self, conn):
        """Everything computed about a scored candidate beyond the bucket
        breakdown (_migrate_signal_detail_columns below) - threshold math,
        EV lookup, execution quality, suggested position size, portfolio
        risk, and the regime/asset-class snapshot - so a past signal's full
        "why" is reconstructable from this table alone. See
        analytics/decision_replay.py. All nullable - a vetoed/already-open
        signal never computed most of these, and older rows predate this
        migration entirely."""
        new_cols = {
            "threshold_breakdown": "TEXT",   # JSON - rules/dynamic_thresholds.py's calculate() result
            "ev_result": "TEXT",             # JSON - engine/ev_engine.py's get_ev_for_signal() result
            "execution_quality": "TEXT",     # JSON - rules/execution_quality.py's ExecutionQualityResult
            "position_size": "TEXT",         # JSON - engine/position_sizing.py's PositionSizeResult
            "portfolio_risk": "TEXT",        # JSON - engine/portfolio_risk.py's PortfolioRiskResult
            "regime_snapshot": "TEXT",       # JSON - engine/regime_engine.py's RegimeState at decision time
            "asset_class": "TEXT",           # "STOCK" | "ETF" - rules/swing_buy_rules.py's _detect_asset_class()
            "probabilistic_decision": "TEXT",  # JSON - rules/probabilistic_decision.py's decide() result
                                                # (2026-07-15) - the REAL basis for this signal's should_buy call
                                                # whenever mode="probabilistic"; None for signals logged before
                                                # this migration, or where the module wasn't reached (vetoed/
                                                # already-open tickers never call score() at all).
            "trade_mode": "TEXT",              # "DAY" | "SWING" | "HYBRID" (2026-07-22, EV mode-keying follow-up) -
                                                # the resolved mode this signal was evaluated/scored under: for a
                                                # BUY, scheduler.py's effective_mode (a HYBRID leg already
                                                # classified DAY/SWING by _classify_hybrid_leg); for a HOLD/veto/
                                                # already-open row (no classification ever runs), the account's
                                                # raw configured trading_mode.upper() instead - still tells you
                                                # what mode config was active. Without this column there was NO
                                                # way to segment historical `signals` rows by DAY vs SWING vs
                                                # HYBRID after the fact - every future calibration/analytics pass
                                                # (bucket-weight re-tuning, HYBRID classification-threshold
                                                # tuning) needs exactly this to compare cohorts. Nullable/None for
                                                # every row logged before this migration.
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "signals", col, coltype)

    def _migrate_signal_detail_columns(self, conn):
        """The rule-by-rule/bucket-by-bucket breakdown behind a BUY/HOLD/SELL
        decision was being computed every cycle (rules/swing_buy_rules.py's
        SwingScoreResult, rules/hard_vetoes.py's VetoResult) but never
        persisted past building output/trade_prompt.md - the signals table
        only stored the final buy_pct number. These columns store the JSON
        breakdown so the UI's Signals tab can show WHY a signal fired, not
        just its final score, for any past signal - not just the current
        cycle's trade_prompt.md file."""
        new_cols = {
            "rules_fired": "TEXT",    # JSON list of rule tags that scored points (BUY/HOLD path)
            "rules_failed": "TEXT",   # JSON list of {name, detail} - unqualified buckets, or the single veto/already-open reason
            "bucket_scores": "TEXT",  # JSON list of {name, weight, points, max_points, min_pct, qualified, rules_fired} - null if vetoed/already-open (never scored)
        }
        for col, coltype in new_cols.items():
            self._add_column_if_missing(conn, "signals", col, coltype)

    def _migrate_trade_snapshot_column(self, conn):
        """Links a `trades` row (a confirmed real fill from confirm_fill.py) to
        the indicator/rule snapshot captured at that moment, stored in the
        already-existing-but-previously-unused `trade_snapshots` table (see
        its schema comment - it was built early on but nothing ever called
        save_trade_snapshot() until confirm_fill.py was wired to use it)."""
        self._add_column_if_missing(conn, "trades", "snapshot_id", "TEXT")

    def _migrate_cycle_trigger_column(self, conn):
        """'scheduler' (the normal Mon-Fri 9:30-16:00 ET cron job) vs 'manual'
        (server.py's /api/cycle/run_now, an on-demand override for testing or
        catching up outside market hours) - so the UI/Journal can tell the two
        apart instead of every cycle row looking like it came from the
        automated loop."""
        self._add_column_if_missing(conn, "cycles", "triggered_by", "TEXT DEFAULT 'scheduler'")

    @contextmanager
    def _conn(self):
        """2026-07-21 (Postgres migration): hands out a pooled connection
        wrapped to look like a sqlite3 connection to every caller below (see
        module docstring / _PGConnWrapper). This IS the fix for the old
        SQLite version's whole "OS-level file-open stall" problem class this
        method used to carry a 4-attempt retry ladder for
        (iCloud/Spotlight/AV interference on the .db file, see git history) -
        pool.getconn() hands back an already-established TCP connection from
        a small in-process pool, no filesystem open() syscall involved, so
        that entire failure mode cannot happen here anymore. Auto-commits on
        clean exit, rolls back on exception, always returns the connection
        to the pool (not a real close - that's the performance win: the
        actual TCP connection stays open and gets reused by the next call)."""
        pool = _get_pool()
        pg_conn = pool.getconn()
        wrapper = _PGConnWrapper(pg_conn)
        try:
            yield wrapper
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise
        finally:
            pool.putconn(pg_conn)

    # ---------- logs ----------
    def log(self, level: str, message: str):
        with self._conn() as conn:
            conn.execute("INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)",
                         (datetime.utcnow().isoformat(), level, message))

    def recent_logs(self, limit: int = 20):
        with self._conn() as conn:
            cur = conn.execute("SELECT timestamp, level, message FROM logs ORDER BY id DESC LIMIT ?", (limit,))
            return list(reversed(cur.fetchall()))

    # ---------- real-time UI push (cross-process outbox) ----------
    def log_ui_event(self, event_type: str, payload: dict):
        """Called by scheduler.py; polled by server.py (a separate process -
        see ui_events' schema comment). Prunes anything older than 1 hour on
        every insert so this table doesn't grow forever - events are for live
        push, not history (learning_runs/signals/trades are the durable
        history tables). Cutoff is computed in Python (not SQLite's datetime())
        so the string format matches exactly what was inserted (isoformat()
        uses 'T' as the date/time separator; SQLite's datetime() uses a space -
        mixing the two breaks lexicographic string comparison for same-day rows)."""
        import json
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        body = json.dumps(payload, default=str)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ui_events (created_at, event_type, payload) VALUES (?,?,?)",
                (datetime.utcnow().isoformat(), event_type, body),
            )
            conn.execute("DELETE FROM ui_events WHERE created_at < ?", (cutoff,))

            # §47.3 (Phase 3): push, alongside the poll.
            #
            # This outbox already had two consumers - scheduler.py writes,
            # server.py polls with get_ui_events_since(). The host agent
            # (scripts/tp_agent.py) is simply a third, and it is the process
            # that owns everything the containerised engine cannot reach: the
            # notification centre, the wakelock, the keyring. No new
            # transport, no new protocol, no change to the scheduler.
            #
            # NOTIFY rather than another poller because a one-second polling
            # floor on a kill-switch alert is a second too many. It is also
            # TRANSACTIONAL: the notification fires only if this INSERT
            # commits, so the agent can never be told about an event that was
            # rolled back - which a side-channel like a file or a socket
            # could not promise.
            #
            # Best-effort by design. A missing LISTENer, a payload above
            # Postgres's 8000-byte NOTIFY limit, or a SQLite-backed test
            # database must not fail the write that matters.
            try:
                conn.execute("SELECT pg_notify('tp_events', ?)",
                             (json.dumps({"type": event_type, "payload": payload},
                                         default=str)[:7900],))
            except Exception as e:
                logger.debug(f"pg_notify skipped for {event_type}: {e}")

    def get_ui_events_since(self, last_id: int, limit: int = 100) -> list:
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM ui_events WHERE id > ? ORDER BY id ASC LIMIT ?", (last_id, limit)
            )
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["payload"] = json.loads(r["payload"])
            return rows

    def get_latest_ui_event_id(self) -> int:
        """Used by server.py on startup so it doesn't replay old events to a
        freshly-connecting poller (starts watching from 'now' forward)."""
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(id) FROM ui_events").fetchone()
            return row[0] or 0

    # ---------- portfolio rotation (engine/rotation.py) ----------

    def log_rotation(self, book: str, candidate_ticker: str, candidate_score,
                     victim_ticker: str, victim_health, victim_days_held,
                     reason: str = ""):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO rotation_log (executed_at, book, candidate_ticker,
                   candidate_score, victim_ticker, victim_health,
                   victim_days_held, reason) VALUES (?,?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), book, candidate_ticker,
                 candidate_score, victim_ticker, victim_health,
                 victim_days_held, reason),
            )

    def count_recent_rotations(self, days: int = 7, simulated: bool = True) -> int:
        """Rotations executed in the last N days for one book - the weekly
        rotation budget check. Cutoff computed in Python for the same
        isoformat-vs-datetime() string-comparison reason as log_ui_event."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        book = "PAPER" if simulated else "LIVE"
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM rotation_log WHERE book = ? AND executed_at >= ?",
                (book, cutoff),
            ).fetchone()
            return row[0] or 0

    # ---------- latest regime snapshot (cross-process) ----------
    def save_latest_regime(self, regime: dict, market_mood: dict = None):
        """regime: vars(RegimeState) from engine/regime_engine.py - see
        latest_regime's schema comment for why this exists (cross-process,
        current_state() is a same-process-only singleton).

        market_mood: OPTIONAL (2026-07-14, News tab follow-up) - fear/greed
        score+rating, VIX level, and macro-blackout proximity are already
        computed every cycle by engine/market_context.py's MarketContext.fetch()
        (scheduler.py's `mkt`), but were never persisted anywhere - server.py
        (a separate process) had no way to show "current market mood" at all.
        Reuses the SAME cross-process singleton-row pattern as the regime
        fields above rather than adding a whole new table, since this is
        the same "one current snapshot, overwritten every cycle" shape.
        None-safe: omitting it (or passing {}) leaves those columns
        unchanged, so callers that don't have `mkt` in scope still work."""
        market_mood = market_mood or {}
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO latest_regime
                (id, updated_at, dominant_regime, bull_pct, bear_pct, choppy_pct,
                 transition_probability, crisis_active, confidence_gap, confidence_level,
                 confidence_score, regime_version, fear_greed_score, fear_greed_rating,
                 vix_level, hours_to_next_macro, blackout_active, blackout_reason)
                VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at, dominant_regime=excluded.dominant_regime,
                    bull_pct=excluded.bull_pct, bear_pct=excluded.bear_pct, choppy_pct=excluded.choppy_pct,
                    transition_probability=excluded.transition_probability, crisis_active=excluded.crisis_active,
                    confidence_gap=excluded.confidence_gap, confidence_level=excluded.confidence_level,
                    confidence_score=excluded.confidence_score, regime_version=excluded.regime_version,
                    fear_greed_score=excluded.fear_greed_score, fear_greed_rating=excluded.fear_greed_rating,
                    vix_level=excluded.vix_level, hours_to_next_macro=excluded.hours_to_next_macro,
                    blackout_active=excluded.blackout_active, blackout_reason=excluded.blackout_reason""",
                (datetime.utcnow().isoformat(), regime.get("dominant_regime"),
                 regime.get("bull_pct"), regime.get("bear_pct"), regime.get("choppy_pct"),
                 regime.get("transition_probability"), int(bool(regime.get("crisis_active"))),
                 regime.get("confidence_gap"), regime.get("confidence_level"),
                 regime.get("confidence_score"), regime.get("regime_version"),
                 market_mood.get("fear_greed_score"), market_mood.get("fear_greed_rating"),
                 market_mood.get("vix_level"), market_mood.get("hours_to_next_macro"),
                 int(bool(market_mood.get("blackout_active"))) if market_mood.get("blackout_active") is not None else None,
                 market_mood.get("blackout_reason")),
            )

    def get_latest_regime(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM latest_regime WHERE id = 1").fetchone()
            if not row:
                return None
            d = dict(row)
            d["crisis_active"] = bool(d["crisis_active"])
            if d.get("blackout_active") is not None:
                d["blackout_active"] = bool(d["blackout_active"])
            return d

    # ---------- news headlines (per-ticker, real yfinance data already
    # fetched every cycle for the SENTIMENT_MACRO bucket's news_multiplier -
    # 2026-07-14: previously scored and then thrown away, never persisted or
    # shown anywhere) ----------
    def record_news_items(self, ticker: str, company_name: str, headlines: list, sentiment_score: float):
        """Upserts each headline. UNIQUE(ticker, headline) means re-fetching
        the same headline on a later cycle (very common - yfinance's news
        feed doesn't turn over every 15 minutes) just refreshes last_seen_at
        instead of creating a duplicate row - the News tab shows each real
        story once, not once per cycle it happened to still be in the feed."""
        if not headlines:
            return
        now = datetime.utcnow().isoformat()
        sentiment_label = (
            "bullish" if sentiment_score >= 0.65 else
            "bearish" if sentiment_score <= 0.35 else
            "neutral"
        )
        with self._conn() as conn:
            for headline in headlines:
                if not headline:
                    continue
                conn.execute(
                    """INSERT INTO news_items
                    (ticker, company_name, headline, sentiment_score, sentiment_label,
                     first_seen_at, last_seen_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(ticker, headline) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        sentiment_score=excluded.sentiment_score,
                        sentiment_label=excluded.sentiment_label,
                        company_name=COALESCE(excluded.company_name, news_items.company_name)""",
                    (ticker, company_name or None, headline, sentiment_score, sentiment_label, now, now),
                )

    def get_recent_news(self, hours: int = 72, limit: int = 100, notable_only: bool = False):
        """Most-recently-SEEN first (not first-seen) - a headline still
        showing up in today's feed is more relevant than one that first
        appeared 3 days ago and hasn't resurfaced since, even if the story
        itself is older. notable_only restricts to bullish/bearish
        (excludes neutral) - used by the News tab's "market-moving only"
        filter."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        query = "SELECT * FROM news_items WHERE last_seen_at >= ?"
        params = [cutoff]
        if notable_only:
            query += " AND sentiment_label != 'neutral'"
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def prune_old_news(self, days: int = 7):
        """Keeps news_items from growing forever - called once per cycle,
        same convention as prune_stale_screener_candidates()."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM news_items WHERE last_seen_at < ?", (cutoff,))

    # ---------- cross-process cycle-running status ----------
    def set_cycle_running(self, triggered_by: str):
        """Called at the very start of engine/cycle_supervisor.py's
        run_supervised() (wrapped in try/finally so set_cycle_finished()
        always runs even on an exception) - works for BOTH the
        cron-triggered scheduler.py process and server.py's manual
        /api/cycle/run_now, since both ultimately funnel through
        scheduler.py's run_cycle() -> run_supervised(). triggered_by:
        "scheduler" | "manual" (matches log_cycle's existing convention).
        Clears pid/kill_reason from any PREVIOUS cycle so a fresh row never
        shows a stale child pid or an old kill reason."""
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, is_running, started_at, triggered_by, finished_at,
                    stage, tickers_total, tickers_done, pid, kill_reason)
                VALUES (1, 1, ?, ?, NULL, 'starting', NULL, NULL, NULL, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    is_running=1, started_at=excluded.started_at, triggered_by=excluded.triggered_by,
                    finished_at=NULL, stage='starting', tickers_total=NULL, tickers_done=NULL,
                    pid=NULL, kill_reason=NULL""",
                (now, triggered_by),
            )

    def set_cycle_pid(self, pid: int):
        """Records the CHILD PROCESS pid actually running this cycle's body
        (engine/cycle_supervisor.py's run_supervised(), right after Popen).
        This is what makes /api/cycle/cancel a real, immediate, cross-process
        hard kill instead of a cooperative flag: any process can read this
        pid back out of the shared DB row and signal it directly."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, pid) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET pid=excluded.pid""",
                (pid,),
            )

    def set_cycle_finished(self):
        """Normal end-of-cycle marker - deliberately does NOT touch
        kill_reason (see mark_cycle_killed()): this is called unconditionally
        in run_supervised()'s finally block even after a hard-kill already
        recorded WHY, and a normal cycle's kill_reason was already cleared
        fresh at set_cycle_running() time, so leaving it alone here can never
        show a stale reason for a cycle that actually finished, nor erase a
        real one for a cycle that didn't."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, is_running, finished_at, pid) VALUES (1, 0, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET is_running=0, finished_at=excluded.finished_at, pid=NULL""",
                (datetime.utcnow().isoformat(),),
            )

    def mark_cycle_killed(self, expected_pid: int, reason: str) -> bool:
        """Hard-kill counterpart to set_cycle_finished() - called by
        engine/cycle_supervisor.py right after force-killing a cycle's
        process group, whether that's the 15-min auto-kill or an immediate
        /api/cycle/cancel. Guarded by expected_pid: only updates the row if
        the pid we just killed is STILL the one on record, so a kill that
        resolves late (e.g. cancel racing the auto-kill's own cleanup, or a
        brand-new cycle having already started in that same small window)
        can never clobber a different, newer cycle's status. Returns False
        (no-op) if the pid no longer matches - caller can treat that as
        "already handled by something else." """
        with self._conn() as conn:
            row = conn.execute("SELECT pid FROM cycle_status WHERE id = 1").fetchone()
            if not row or row[0] != expected_pid:
                return False
            conn.execute(
                """UPDATE cycle_status SET is_running = 0, finished_at = ?, pid = NULL,
                   kill_reason = ? WHERE id = 1""",
                (datetime.utcnow().isoformat(), reason),
            )
            return True

    # ---------- full-market universe (2026-07-15g, universe sweep) ----------
    # Answers "will it look at ALL stocks, not just the movers?" - a
    # persistent registry of every known US-equity symbol (Alpaca's free
    # assets endpoint when configured: ~10k active equities; plus organic
    # accumulation from every screener source and the ticker cache). The
    # sweep source (engine/screener.py's _screen_universe_sweep) draws the
    # least-recently-examined batch each cycle, so over days the ENTIRE
    # eligible market rotates through the standard quality gate and scoring
    # - not just whatever happened to be moving that day.
    def _ensure_universe_table(self, conn):
        conn.execute(
            """CREATE TABLE IF NOT EXISTS universe (
                symbol TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_swept_at TEXT,
                source TEXT
            )""")

    def upsert_universe_symbols(self, symbols: list, source: str):
        if not symbols:
            return
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            self._ensure_universe_table(conn)
            conn.executemany(
                """INSERT INTO universe (symbol, first_seen_at, source)
                VALUES (?, ?, ?) ON CONFLICT(symbol) DO NOTHING""",
                [(s.upper(), now, source) for s in symbols if s])

    def get_universe_sweep_batch(self, n: int) -> list:
        """Least-recently-examined first; never-examined before everything."""
        with self._conn() as conn:
            self._ensure_universe_table(conn)
            rows = conn.execute(
                """SELECT symbol FROM universe
                ORDER BY (last_swept_at IS NOT NULL) ASC,
                         COALESCE(last_swept_at, '0') ASC
                LIMIT ?""", (int(n),)).fetchall()
            return [r[0] for r in rows]

    def mark_universe_swept(self, symbols: list):
        if not symbols:
            return
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            self._ensure_universe_table(conn)
            conn.executemany("UPDATE universe SET last_swept_at = ? WHERE symbol = ?",
                             [(now, s.upper()) for s in symbols])

    def universe_count(self) -> int:
        with self._conn() as conn:
            self._ensure_universe_table(conn)
            return conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0]

    def get_known_tickers_for_universe_seed(self) -> list:
        """Organic seed: every symbol this platform has ever cached info for."""
        with self._conn() as conn:
            try:
                return [r[0] for r in conn.execute("SELECT ticker FROM ticker_info_cache")]
            except Exception:
                return []

    # ---------- data-source health (2026-07-15) ----------
    # Written by mcp_clients/base.py's SourceCircuitBreaker (finviz/maverick/
    # scanner + the market-data providers) and engine/ticker_analyzer.py
    # (yfinance) - read by server.py's /api/sources for the Monitor tab's
    # "Data Sources" panel, so which MCP/API is healthy vs down is visible
    # in the app instead of requiring log archaeology.
    def upsert_source_health(self, name: str, success: bool, error: str = "",
                              consecutive_failures: int = 0, breaker_open_until: float = 0.0):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS source_health (
                    name TEXT PRIMARY KEY,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER DEFAULT 0,
                    breaker_open_until REAL DEFAULT 0,
                    updated_at TEXT
                )""")
            if success:
                conn.execute(
                    """INSERT INTO source_health (name, last_success_at, consecutive_failures,
                        breaker_open_until, updated_at)
                    VALUES (?, ?, 0, 0, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        last_success_at=excluded.last_success_at, consecutive_failures=0,
                        breaker_open_until=0, updated_at=excluded.updated_at""",
                    (name, now, now))
            else:
                conn.execute(
                    """INSERT INTO source_health (name, last_failure_at, last_error,
                        consecutive_failures, breaker_open_until, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        last_failure_at=excluded.last_failure_at, last_error=excluded.last_error,
                        consecutive_failures=excluded.consecutive_failures,
                        breaker_open_until=excluded.breaker_open_until,
                        updated_at=excluded.updated_at""",
                    (name, now, (error or "")[:300], consecutive_failures, breaker_open_until, now))

    def get_source_health(self) -> list:
        with self._conn() as conn:
            try:
                cur = conn.execute("SELECT * FROM source_health ORDER BY name")
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            except Exception:
                return []  # table not created yet (no source has reported)

    # ---------- analyst estimate revision tracking (2026-07-16) ----------
    # engine/rules_catalog.py's analyst_estimate_raised rule used to be a
    # genuine PLACEHOLDER: no data source at all. FMP's free-tier
    # /stable/analyst-estimates (mcp_clients/market_data.py's
    # FMPProvider.get_consensus_eps) gives a real current consensus EPS, but
    # only a SNAPSHOT - detecting a "raise" needs a point-in-time history to
    # diff against, which is what this table is for: one row per ticker per
    # day, so a later cycle can compare today's consensus against an
    # older stored reading.
    def check_and_record_estimate_snapshot(self, ticker: str, consensus_eps: float,
                                            lookback_days: int = 30,
                                            raise_threshold: float = 0.01) -> tuple:
        """Records today's consensus_eps reading (idempotent - one row per
        ticker per calendar day) and returns (raised, detail).

        raised is None - genuinely unknown, NOT "not raised" - when there's
        no snapshot old enough to compare against yet (first time this
        ticker's been seen, or fewer than lookback_days of history exist).
        This matters: engine/ticker_data_adapter.py treats None the same as
        False (no credit), so a ticker can never earn analyst_estimate_raised
        points off an absent baseline - only off a REAL, measured increase.
        Expect every ticker to return None for its first ~30 days after this
        was wired up; that's the table filling in, not a bug.

        detail (2026-07-21, external review - "label it explicitly...
        WARMING_UP... do not classify it as simply False. A genuine
        no-revision observation and insufficient stored history are
        analytically different. Also record: observed EPS estimate, prior
        EPS estimate, percent change, source, snapshot age") is a dict,
        always present, so callers/logging never have to re-derive "why is
        this None":
          status: "WARMING_UP" (no old-enough baseline yet) or "MEASURED"
          score_effect: 0 for WARMING_UP - matches the None->no-credit
              behavior in ticker_data_adapter.py; for MEASURED, the real
              analyst_estimate_raised point value if raised else 0 (display
              only - rules/swing_buy_rules.py stays the single source of
              truth for the actual scoring value)
          data_availability: "insufficient_history" or "ok"
          observed_eps / prior_eps / pct_change: None while WARMING_UP
          source: "fmp_stable_analyst_estimates"
          snapshot_age_days: None while WARMING_UP, else the real gap in
              days between today's reading and the compared snapshot
          analyst_count_change: always None - FMP's free consensus_eps
              snapshot doesn't carry an analyst count; genuinely not
              sourced, not a bug (same "field is real or omitted, never
              faked" convention as the rest of this codebase's PLACEHOLDER
              fields). A 1% move from a thin consensus can't yet be told
              apart from the same move on a broad one - future work, not
              this pass.
        """
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cutoff = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS estimate_snapshots (
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    consensus_eps REAL,
                    PRIMARY KEY (ticker, date)
                )""")
            conn.execute(
                """INSERT INTO estimate_snapshots (ticker, date, consensus_eps)
                VALUES (?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET consensus_eps=excluded.consensus_eps""",
                (ticker, today, consensus_eps))
            row = conn.execute(
                """SELECT consensus_eps, date FROM estimate_snapshots
                   WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1""",
                (ticker, cutoff)).fetchone()
            _SOURCE = "fmp_stable_analyst_estimates"
            if row is None or row[0] is None or row[0] <= 0:
                return None, {
                    "status": "WARMING_UP",
                    "score_effect": 0,
                    "data_availability": "insufficient_history",
                    "observed_eps": consensus_eps,
                    "prior_eps": None,
                    "pct_change": None,
                    "source": _SOURCE,
                    "snapshot_age_days": None,
                    "analyst_count_change": None,
                }
            prior_eps, prior_date = row[0], row[1]
            pct_change = (consensus_eps - prior_eps) / abs(prior_eps)
            raised = pct_change > raise_threshold
            try:
                age_days = (datetime.strptime(today, "%Y-%m-%d")
                            - datetime.strptime(prior_date, "%Y-%m-%d")).days
            except Exception:
                age_days = None
            return raised, {
                "status": "MEASURED",
                "score_effect": 6 if raised else 0,
                "data_availability": "ok",
                "observed_eps": consensus_eps,
                "prior_eps": prior_eps,
                "pct_change": round(pct_change * 100, 2),
                "source": _SOURCE,
                "snapshot_age_days": age_days,
                "analyst_count_change": None,
            }

    # ---------- cooperative cycle cancellation (2026-07-15) ----------
    # Trinath: "Is there a way to kill a running cycle" - there wasn't.
    # server.py and scheduler.py are separate processes, so the cancel signal
    # travels through this table (same pattern as ui_events/latest_regime):
    # POST /api/cycle/cancel sets the flag; scheduler.py checks it between
    # ticker completions and aborts the remaining work. Running MCP calls
    # can't be force-killed mid-flight, but nothing new is started, so the
    # cycle winds down within roughly one ticker's duration.
    def request_cycle_cancel(self):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, cancel_requested) VALUES (1, 1)
                ON CONFLICT(id) DO UPDATE SET cancel_requested=1""")

    def clear_cycle_cancel(self):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, cancel_requested) VALUES (1, 0)
                ON CONFLICT(id) DO UPDATE SET cancel_requested=0""")

    def is_cycle_cancel_requested(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT cancel_requested FROM cycle_status WHERE id=1").fetchone()
            return bool(row and row[0])

    def set_cycle_stage(self, stage: str, tickers_total: int = None, tickers_done: int = None):
        """Progress-bar follow-up (2026-07-14): Trinath asked for a real 0-100%
        progress indicator instead of just an elapsed-time counter. A single
        percentage can't be computed from elapsed time alone (cycle length
        varies a lot with watchlist size/screener activity/MCP latency), so
        instead this tracks which named STAGE of _run_cycle_impl() is
        currently running (market_context -> screener -> ticker_analysis ->
        finalizing) - the UI maps each stage to an approximate cumulative %
        band (see ui/index.html's STAGE_BANDS) and, for ticker_analysis
        specifically, uses the REAL tickers_done/tickers_total fraction
        within that band since that's the one stage with a genuine, easily
        counted unit of work. tickers_total/tickers_done are only meaningful
        during the ticker_analysis stage - left NULL/unset otherwise.
        Called from a single scheduler.py process per cycle, but still
        lock-guarded like every other write here for consistency."""
        with self._conn() as conn:
            if tickers_total is not None:
                conn.execute(
                    """INSERT INTO cycle_status (id, stage, tickers_total, tickers_done)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        stage=excluded.stage, tickers_total=excluded.tickers_total,
                        tickers_done=excluded.tickers_done""",
                    (stage, tickers_total, tickers_done if tickers_done is not None else 0),
                )
            else:
                conn.execute(
                    """INSERT INTO cycle_status (id, stage) VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET stage=excluded.stage""",
                    (stage,),
                )

    def increment_cycle_tickers_done(self):
        """Called once per completed ticker inside scheduler.py's per-ticker
        ThreadPoolExecutor loop (from the main thread as each future
        resolves via as_completed(), not from the worker threads themselves -
        so this is never called concurrently and doesn't strictly need the
        lock for correctness, but takes it anyway for consistency with every
        other write in this class). A plain UPDATE ... SET tickers_done =
        tickers_done + 1 rather than read-modify-write from Python, so it's
        atomic even if that assumption ever changes."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE cycle_status SET tickers_done = COALESCE(tickers_done, 0) + 1 WHERE id = 1"
            )

    def get_cycle_status(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM cycle_status WHERE id = 1").fetchone()
            if not row:
                return {"is_running": False, "started_at": None, "triggered_by": None,
                         "finished_at": None, "next_run_at": None, "stage": None,
                         "tickers_total": None, "tickers_done": None,
                         "pid": None, "kill_reason": None}
            d = dict(row)
            d["is_running"] = bool(d["is_running"])
            return d

    def set_next_cycle_time(self, next_run_at_iso: str):
        """2026-07-14: Trinath asked why the UI never shows when the next scan
        cycle will fire - server.py is a separate process from scheduler.py's
        own APScheduler instance, so it has no way to introspect that
        scheduler's internal job state directly; this is the same
        cross-process-bridge-via-DB pattern cycle_status/latest_regime/
        ui_events already use for the same reason. scheduler.py calls this
        from an APScheduler event listener (see start()) every time the
        cron job's next_run_time is (re)computed - once right when the
        scheduler starts, and again after every firing - so this value is
        always the SAME thing APScheduler itself believes is next, not a
        separately-reimplemented cron calculation that could drift out of
        sync with the real trigger config."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycle_status (id, next_run_at) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET next_run_at=excluded.next_run_at""",
                (next_run_at_iso,),
            )

    def clear_stale_cycle(self, max_age_minutes: int = 20) -> bool:
        """Self-healing watchdog (2026-07-14, after a real cycle got stuck
        showing "running" for 23+ minutes with the process silently hung -
        see mcp_clients/base.py's call_tool() docstring for the root cause
        that fix addresses). is_running=1 can only ever be cleared by
        set_cycle_finished()/mark_cycle_killed() - if the WHOLE PROCESS
        hangs or gets killed before either of those run, this row stays
        stuck at is_running=1 forever, which is misleading in the UI AND can
        block a genuinely-new cycle from being understood as "not actually
        running" by anything that checks this flag.

        2026-07-22: engine/cycle_supervisor.py's hard 15-min process-group
        kill (see its module docstring) should make this row getting stuck
        far rarer than before - that supervisor now GUARANTEES
        set_cycle_finished()/mark_cycle_killed() runs within a bounded time
        for every cycle, scheduled or manual. This stays in place as a
        defense-in-depth fallback (e.g. the whole scheduler.py process itself
        being killed between spawning the child and its own finally block
        running) rather than the primary fix it used to be.

        Called at the very start of run_supervised(), BEFORE
        set_cycle_running() - if the current row claims a cycle has been
        running for longer than max_age_minutes (config:
        trading.hard_kill_minutes, default 15), force-clears it before
        proceeding. Returns True if it actually cleared something (so the
        caller can log it - a cleared stale cycle is worth knowing about,
        not something to silently paper over)."""
        with self._conn() as conn:
            row = conn.execute("SELECT is_running, started_at FROM cycle_status WHERE id = 1").fetchone()
            if not row or not row[0] or not row[1]:
                return False
            try:
                started = datetime.fromisoformat(row[1])
            except (ValueError, TypeError):
                return False
            age_minutes = (datetime.utcnow() - started).total_seconds() / 60
            if age_minutes < max_age_minutes:
                return False
            conn.execute(
                "UPDATE cycle_status SET is_running = 0, finished_at = ? WHERE id = 1",
                (datetime.utcnow().isoformat(),),
            )
            return True

    # ---------- ticker info cache (company names, validation) ----------
    def upsert_ticker_info(self, ticker: str, company_name: str = None, last_price: float = None,
                            valid: bool = True, sector: str = None, beta: float = None,
                            industry: str = None):
        """Called both opportunistically (every scan cycle, for whatever's
        already been fetched) and explicitly (ticker validation on add) - see
        ticker_info_cache's schema comment. Only overwrites company_name/
        sector/industry/beta when a non-empty/non-None value is given, so an
        opportunistic scheduler.py call missing one field (e.g. yfinance had
        no longName for this ticker) doesn't blank out a value a previous
        call already found.

        `industry` added for §18. "N/A" is treated as absent for the same
        reason the analyzer refuses to let finviz's "N/A" clobber a real
        yfinance sector: a placeholder string that overwrites a real value is
        worse than no value, because it looks like an answer.
        """
        sector = None if sector in ("", "N/A") else sector
        industry = None if industry in ("", "N/A") else industry
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT company_name, sector, beta, industry FROM ticker_info_cache WHERE ticker = ?",
                (ticker,)).fetchone()
            name = company_name or (existing[0] if existing else None)
            sec = sector or (existing[1] if existing else None)
            bta = beta if beta is not None else (existing[2] if existing else None)
            ind = industry or (existing[3] if existing else None)
            conn.execute(
                """INSERT INTO ticker_info_cache
                    (ticker, company_name, last_price, valid, updated_at, sector, beta, industry)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET
                    company_name=excluded.company_name, last_price=excluded.last_price,
                    valid=excluded.valid, updated_at=excluded.updated_at,
                    sector=excluded.sector, beta=excluded.beta,
                    industry=excluded.industry""",
                (ticker, name, last_price, int(bool(valid)), datetime.utcnow().isoformat(),
                 sec, bta, ind),
            )

    def get_ticker_info(self, ticker: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM ticker_info_cache WHERE ticker = ?", (ticker,)).fetchone()
            return dict(row) if row else None

    def get_ticker_info_bulk(self, tickers: list) -> dict:
        """{ticker: {company_name, sector, beta, last_price, ...}} for a batch
        of tickers in one query - used by engine/portfolio_risk.py to look up
        every open position's sector/beta without N separate round-trips."""
        if not tickers:
            return {}
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in tickers)
            cur = conn.execute(
                f"SELECT * FROM ticker_info_cache WHERE ticker IN ({placeholders})", tickers
            )
            return {row["ticker"]: dict(row) for row in cur.fetchall()}

    # ---------- ticker data health (Data Provenance Circuit Breaker) ----------
    def record_ticker_data_health(self, ticker: str, is_stale_cycle: bool) -> int:
        """Called once per analyzed ticker per cycle (scheduler.py's
        _evaluate_ticker) - tracks CONSECUTIVE cycles this ticker's data has
        tripped (or nearly tripped) rules/hard_vetoes.py's veto #16
        (STALE_DATA_CIRCUIT_BREAKER), independent of whether that veto
        actually fired this cycle (an earlier veto, or already-open status,
        can short-circuit before veto #16 is even evaluated - but the
        underlying data quality is the same either way, so this is computed
        directly from stale_indicators/breadth_stale, not from the veto
        result). Resets to 0 on any clean cycle. Returns the NEW consecutive
        count so the caller can decide whether to alert (see
        data_quality.consecutive_stale_alert_cycles)."""
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT consecutive_stale_cycles FROM ticker_info_cache WHERE ticker = ?", (ticker,)
            ).fetchone()
            current = (row[0] if row else 0) or 0
            new_count = (current + 1) if is_stale_cycle else 0
            if row:
                if is_stale_cycle:
                    conn.execute(
                        "UPDATE ticker_info_cache SET consecutive_stale_cycles = ?, last_stale_at = ? WHERE ticker = ?",
                        (new_count, now, ticker),
                    )
                else:
                    conn.execute(
                        "UPDATE ticker_info_cache SET consecutive_stale_cycles = 0, last_healthy_at = ? WHERE ticker = ?",
                        (now, ticker),
                    )
            else:
                # Shouldn't normally happen - upsert_ticker_info() is called earlier
                # in the same cycle - but insert defensively rather than lose the signal.
                conn.execute(
                    """INSERT INTO ticker_info_cache (ticker, valid, updated_at, consecutive_stale_cycles,
                       last_stale_at, last_healthy_at) VALUES (?,1,?,?,?,?)""",
                    (ticker, now, new_count, now if is_stale_cycle else None, None if is_stale_cycle else now),
                )
            return new_count

    def get_unhealthy_tickers(self, min_consecutive: int = 1, max_age_minutes: int = None) -> list:
        """Every ticker currently on a stale-data streak of at least
        min_consecutive cycles - used by server.py's /api/ticker/health for
        the Control tab's watchlist-chip warning badge (called with
        max_age_minutes=None there - show every currently-unhealthy ticker,
        no matter how old the streak, for genuine visibility).

        max_age_minutes (2026-07-14 fix - "screener catch-22"): engine/
        screener.py's _pre_filter() ALSO calls this to exclude unhealthy
        candidates from being re-selected - but a candidate that's excluded
        never gets re-evaluated by scheduler.py's per-ticker loop, which is
        the ONLY thing that can reset consecutive_stale_cycles back to 0 (via
        record_ticker_data_health() on a clean cycle). Without a recency
        cutoff, a ticker that went stale once (e.g. during a thin pre-market
        window) would be locked out of the screener FOREVER - it can never
        get the clean cycle it needs to prove it's recovered, since it's
        never looked at again. Confirmed happening in production: 11+
        tickers stuck at exactly consecutive_stale_cycles=3 with a
        last_stale_at from early in the session, hours before being checked.
        Passing max_age_minutes restricts the exclusion to streaks that went
        stale RECENTLY (last_stale_at within the window) - once the cooldown
        elapses with no fresh (re-staling) evaluation, the ticker quietly
        drops out of this query and gets one more shot at the screener. If
        it's still bad, the next stale evaluation just refreshes
        last_stale_at and the cooldown restarts - self-healing either way."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if max_age_minutes is not None:
                cutoff = (datetime.utcnow() - timedelta(minutes=max_age_minutes)).isoformat()
                cur = conn.execute(
                    """SELECT ticker, consecutive_stale_cycles, last_stale_at, last_healthy_at
                       FROM ticker_info_cache WHERE consecutive_stale_cycles >= ? AND last_stale_at >= ?
                       ORDER BY consecutive_stale_cycles DESC""",
                    (min_consecutive, cutoff),
                )
            else:
                cur = conn.execute(
                    """SELECT ticker, consecutive_stale_cycles, last_stale_at, last_healthy_at
                       FROM ticker_info_cache WHERE consecutive_stale_cycles >= ?
                       ORDER BY consecutive_stale_cycles DESC""",
                    (min_consecutive,),
                )
            return [dict(r) for r in cur.fetchall()]

    # ---------- portfolio risk manager ----------
    def log_portfolio_risk(self, ticker: str, sector: str, themes: list, sector_exposure_pct: float,
                            theme_exposure_pct: float, portfolio_beta: float, max_pairwise_correlation: float,
                            high_vol_position_count: int, size_multiplier: float, blocked: bool, reasons: list):
        import json
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO portfolio_risk_log
                (timestamp, ticker, sector, themes, sector_exposure_pct, theme_exposure_pct, portfolio_beta,
                 max_pairwise_correlation, high_vol_position_count, size_multiplier, blocked, reasons)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), ticker, sector, json.dumps(themes or []),
                 sector_exposure_pct, theme_exposure_pct, portfolio_beta, max_pairwise_correlation,
                 high_vol_position_count, size_multiplier, int(bool(blocked)), json.dumps(reasons or [])),
            )

    def get_recent_portfolio_risk_log(self, limit: int = 50) -> list:
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM portfolio_risk_log ORDER BY id DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["themes"] = json.loads(r["themes"]) if r.get("themes") else []
                r["reasons"] = json.loads(r["reasons"]) if r.get("reasons") else []
            return rows

    # ---------- screener candidate persistence/aging (engine/screener.py) ----------
    def upsert_screener_candidate(self, ticker: str, mode: str, score: float,
                                   source: str = None, decomposition: dict = None):
        """Called once per ticker per screener run (engine/screener.py's
        run_screener). Bumps times_seen/last_score every call; best_score only
        ever increases. first_seen_at is set once and never overwritten.

        source/decomposition (2026-07-15f, review round 5): which discovery
        source surfaced the candidate this cycle, and the Discovery Score's
        component breakdown (rs_20d/50d/100d, trend_aligned, persistence) -
        so future outcome analysis can attribute good/bad picks to specific
        sources and components instead of one opaque number."""
        import json
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            self._add_column_if_missing(conn, "screener_candidates", "last_source", "TEXT")
            self._add_column_if_missing(conn, "screener_candidates", "last_decomposition", "TEXT")
            conn.execute(
                """INSERT INTO screener_candidates (ticker, mode, first_seen_at, last_seen_at,
                    times_seen, last_score, best_score, last_source, last_decomposition)
                VALUES (?,?,?,?,1,?,?,?,?)
                ON CONFLICT(ticker, mode) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    times_seen=screener_candidates.times_seen + 1,
                    last_score=excluded.last_score,
                    best_score=GREATEST(screener_candidates.best_score, excluded.best_score),
                    last_source=excluded.last_source,
                    last_decomposition=excluded.last_decomposition""",
                (ticker, mode, now, now, score, score, source,
                 json.dumps(decomposition) if decomposition else None),
            )

    def get_screener_history(self, ticker: str, mode: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM screener_candidates WHERE ticker = ? AND mode = ?", (ticker, mode)
            ).fetchone()
            return dict(row) if row else None

    def get_most_discovered_tickers(self, mode: str = "swing", limit: int = 60,
                                     min_scored: int = 1) -> list:
        """Returns up to `limit` tickers from screener_candidates, ordered by
        times_seen DESCENDING - i.e. the names the LIVE discovery sources
        (rs_gainers/volume_surge/sector_leaders/etc. - config.yaml's
        screener.sources) have organically surfaced most often.

        Used by engine/backtest_loop.py's resolve_backtest_tickers() to build
        an automatic backtest ticker universe (2026-07-24, zero-trades
        follow-up: "pick tickers automatically instead of a hand-curated
        list"). Deliberately ordered/filtered by DISCOVERY FREQUENCY, never
        by score/qualify-rate - a backtest universe selected because those
        tickers already cleared 50%+ historically would trivially "clear
        50%+" again over that same window (the exact look-ahead/selection
        bias this method exists to avoid; see that function's docstring).
        times_seen only reflects that the live system found a ticker worth
        re-scanning often, which is knowable without knowing how it later
        scored - so this is a legitimate universe, not a curated winner list.
        min_scored (default 1) drops pure one-off appearances (discovered
        once, never actually scored) that add noise without adding history.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT ticker FROM screener_candidates "
                "WHERE mode = ? AND n_scored >= ? "
                "ORDER BY times_seen DESC LIMIT ?",
                (mode, min_scored, limit),
            )
            return [r["ticker"] for r in cur.fetchall()]

    def prune_stale_screener_candidates(self, mode: str, stale_after_days: int = 5):
        """Drops rows a ticker hasn't appeared in for a while, so a stock that
        was hot once in March doesn't keep getting a persistence bonus in
        June. Called at the start of each run_screener() call. This also
        resets that ticker's outcome-tracking stats (n_scored/n_qualified/
        n_stale_data_blocked below) if it's re-discovered later - "innocent
        until proven guilty again" rather than a permanent blocklist entry."""
        cutoff = (datetime.utcnow() - timedelta(days=stale_after_days)).isoformat()
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM screener_candidates WHERE mode = ? AND last_seen_at < ?", (mode, cutoff)
            )

    def record_screener_outcome(self, ticker: str, mode: str, qualified: bool,
                                 stale_data_blocked: bool, buy_pct: float = None):
        """Called once per cycle a SCREENER-sourced ticker is actually run
        through scoring (scheduler.py's _evaluate_ticker, only when the
        ticker came from engine/screener.py's candidate list, not the manual
        watchlist). This is the missing feedback loop: without it, the
        screener only ever knew "how often did this ticker get discovered"
        (times_seen/best_score), never "was it actually any good" - see
        engine/screener.py's _persistence_bonus() and
        get_low_quality_screener_tickers() for how this gets used."""
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT n_scored, n_qualified, n_stale_data_blocked, sum_buy_pct, n_buy_pct_samples "
                "FROM screener_candidates WHERE ticker = ? AND mode = ?", (ticker, mode),
            ).fetchone()
            if row is None:
                # Being scored without a discovery-time row (shouldn't normally
                # happen - discovery always runs first and upserts one) - insert
                # defensively rather than silently lose this outcome.
                conn.execute(
                    """INSERT INTO screener_candidates
                       (ticker, mode, first_seen_at, last_seen_at, times_seen, last_score, best_score)
                       VALUES (?,?,?,?,0,0,0)""",
                    (ticker, mode, now, now),
                )
                n_scored, n_qualified, n_stale, sum_pct, n_pct = 0, 0, 0, 0.0, 0
            else:
                n_scored, n_qualified, n_stale, sum_pct, n_pct = row

            n_scored = (n_scored or 0) + 1
            n_qualified = (n_qualified or 0) + (1 if qualified else 0)
            n_stale = (n_stale or 0) + (1 if stale_data_blocked else 0)
            sum_pct = sum_pct or 0.0
            n_pct = n_pct or 0
            if buy_pct is not None:
                sum_pct += buy_pct
                n_pct += 1

            conn.execute(
                """UPDATE screener_candidates SET n_scored = ?, n_qualified = ?, n_stale_data_blocked = ?,
                   sum_buy_pct = ?, n_buy_pct_samples = ? WHERE ticker = ? AND mode = ?""",
                (n_scored, n_qualified, n_stale, sum_pct, n_pct, ticker, mode),
            )

    def get_low_quality_screener_tickers(self, mode: str, min_track_record: int = 5,
                                          max_qualify_rate: float = 0.05,
                                          min_stale_block_rate: float = 0.5) -> list:
        """Screener candidates with enough scored history (>= min_track_record
        cycles) that have PROVEN to be low quality - either they almost never
        qualify as a real BUY, or they're chronically blocked by stale/
        fallback data (rules/hard_vetoes.py's veto #16), even outside an
        active streak (see get_unhealthy_tickers() for the CURRENT-streak
        version). Used by engine/screener.py's _pre_filter() to stop
        re-surfacing a ticker the system has already learned isn't worth
        another look, until prune_stale_screener_candidates() resets it
        (i.e. it stops being discovered for a while, then comes back fresh)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM screener_candidates WHERE mode = ? AND n_scored >= ?",
                (mode, min_track_record),
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                n_scored = d["n_scored"] or 1
                qualify_rate = (d["n_qualified"] or 0) / n_scored
                stale_rate = (d["n_stale_data_blocked"] or 0) / n_scored
                if qualify_rate <= max_qualify_rate or stale_rate >= min_stale_block_rate:
                    d["qualify_rate"] = round(qualify_rate, 3)
                    d["stale_block_rate"] = round(stale_rate, 3)
                    out.append(d)
            return out

    def get_all_ticker_names(self) -> dict:
        """{ticker: company_name} for every cached ticker with a known name -
        used by the UI to power hover tooltips without a live call per ticker."""
        with self._conn() as conn:
            cur = conn.execute("SELECT ticker, company_name FROM ticker_info_cache WHERE company_name IS NOT NULL AND company_name != ''")
            return {row[0]: row[1] for row in cur.fetchall()}

    # ---------- signals ----------
    @staticmethod
    def _decision_context_json(obj):
        """Serializes any of the decision-context objects (dataclass instance,
        plain dict, or None) to a JSON string for the signals table's TEXT
        columns - see _migrate_decision_context_columns. Dataclasses
        (PositionSizeResult, PortfolioRiskResult, ExecutionQualityResult,
        RegimeState) are converted via dataclasses.asdict(); dicts
        (threshold_result from rules/dynamic_thresholds.py, ev_result from
        engine/ev_engine.py) are passed through as-is."""
        import dataclasses
        import json
        if obj is None:
            return None
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            obj = dataclasses.asdict(obj)
        return json.dumps(obj, default=str)

    def log_signal(self, ticker: str, td, buy_result, sell_result=None, score_result=None,
                    threshold_result=None, ev_result=None, execution_quality=None,
                    position_size=None, portfolio_risk=None, regime=None, asset_class=None,
                    probabilistic_decision=None, trade_mode=None) -> int:
        """score_result: rules/swing_buy_rules.py's SwingScoreResult, when the
        ticker was actually scored this cycle (i.e. not vetoed, not already
        an open position) - gives the full 6-bucket breakdown. When it's None
        (vetoed / already-open / legacy buy_rules.py path), rules_failed still
        carries the single reason from BuyResultCompat (see
        rules/swing_buy_rules.py's from_veto()/already_open()).

        threshold_result/ev_result/execution_quality/position_size/
        portfolio_risk/regime/asset_class: the rest of a scored cycle's
        decision context (see _migrate_decision_context_columns) - all
        optional/None for vetoed or already-open tickers where these were
        never computed. Persisting these is what makes
        analytics/decision_replay.py's replay_signal() possible without
        re-deriving anything live.

        trade_mode: OPTIONAL "DAY"/"SWING"/"HYBRID" (2026-07-22, EV mode-keying
        follow-up) - see _migrate_decision_context_columns' trade_mode comment
        for why this exists and what the caller should pass."""
        import json

        if sell_result is not None and sell_result.should_sell:
            signal_label = "SELL"
        elif buy_result is not None and buy_result.should_buy:
            signal_label = "BUY"
        else:
            signal_label = "HOLD"

        rules_fired = [r.name for r in (getattr(buy_result, "rules_passed", None) or [])]
        rules_failed = [{"name": r.name, "detail": r.detail}
                         for r in (getattr(buy_result, "rules_failed", None) or [])]
        bucket_scores = None
        if score_result is not None:
            bucket_scores = [
                {
                    "name": b.name, "weight": b.weight, "points": b.points, "max_points": b.max_points,
                    "min_pct": b.min_pct, "qualified": b.qualified, "rules_fired": b.rules_fired,
                    # qual_mult/checklist/contribution_pct - added so the UI/journal can show
                    # the same rich raw/normalized/weight breakdown as a live cycle's
                    # trade_prompt.md, not just a flattened points/max number, for any
                    # PAST signal too (see rules/swing_buy_rules.py's DIAGNOSTICS NOTE).
                    "qual_mult": getattr(b, "qual_mult", 1.0),
                    "checklist": getattr(b, "checklist", []),
                    "contribution_pct": round(
                        (b.points / b.max_points) * b.weight * getattr(b, "qual_mult", 1.0) * 100
                        if b.max_points else 0.0, 2,
                    ),
                }
                for b in score_result.buckets
            ]

        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                (timestamp, ticker, signal, confidence, price, buy_score, buy_pct,
                 sell_triggered_rule, sell_reason, data_quality, rules_fired, rules_failed, bucket_scores,
                 threshold_breakdown, ev_result, execution_quality, position_size, portfolio_risk,
                 regime_snapshot, asset_class, probabilistic_decision, trade_mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id""",
                (
                    datetime.utcnow().isoformat(), ticker, signal_label,
                    getattr(buy_result, "pct_score", None) if buy_result else None,
                    getattr(td, "price", None),
                    getattr(buy_result, "score", None) if buy_result else None,
                    getattr(buy_result, "pct_score", None) if buy_result else None,
                    getattr(sell_result, "triggered_rule", None) if sell_result else None,
                    getattr(sell_result, "reason", None) if sell_result else None,
                    getattr(td, "data_quality", None),
                    json.dumps(rules_fired),
                    json.dumps(rules_failed),
                    json.dumps(bucket_scores) if bucket_scores is not None else None,
                    self._decision_context_json(threshold_result),
                    self._decision_context_json(ev_result),
                    self._decision_context_json(execution_quality),
                    self._decision_context_json(position_size),
                    self._decision_context_json(portfolio_risk),
                    self._decision_context_json(regime),
                    asset_class,
                    self._decision_context_json(probabilistic_decision),
                    trade_mode,
                ),
            )
            today = date.today().isoformat()
            conn.execute(
                """INSERT INTO daily_stats (date, signals_generated) VALUES (?, 1)
                   ON CONFLICT(date) DO UPDATE SET signals_generated = daily_stats.signals_generated + 1""",
                (today,),
            )
            return cur.lastrowid

    @staticmethod
    def _parse_signal_json(row: dict) -> dict:
        import json
        for col, default in (("rules_fired", []), ("rules_failed", []), ("bucket_scores", None),
                              ("threshold_breakdown", None), ("ev_result", None), ("execution_quality", None),
                              ("position_size", None), ("portfolio_risk", None), ("regime_snapshot", None),
                              ("probabilistic_decision", None)):
            raw = row.get(col)
            row[col] = json.loads(raw) if raw else default
        return row

    def get_recent_signals(self, limit: int = 50):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
            return [self._parse_signal_json(dict(r)) for r in cur.fetchall()]

    def latest_signal(self, ticker: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM signals WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,))
            row = cur.fetchone()
            return self._parse_signal_json(dict(row)) if row else None

    def get_signal_by_id(self, signal_id: int):
        """Looks up a single signals row by its own id - used by
        analytics/decision_replay.py when a caller already knows the exact
        signal (e.g. from a UI link) rather than searching by ticker/date."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
            return self._parse_signal_json(dict(row)) if row else None

    def find_signal(self, ticker: str, date: str = None):
        """analytics/decision_replay.py's main lookup path: 'reconstruct what
        happened for TICKER on DATE'. date is a YYYY-MM-DD string matched
        against the timestamp's date portion; when omitted, returns the most
        recent signal for that ticker (same as latest_signal). When multiple
        signals exist for the ticker on that date (multiple scan cycles),
        returns the last one of the day - the final decision made."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if date:
                cur = conn.execute(
                    "SELECT * FROM signals WHERE ticker = ? AND substr(timestamp, 1, 10) = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (ticker, date),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM signals WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,)
                )
            row = cur.fetchone()
            return self._parse_signal_json(dict(row)) if row else None

    # ---------- weight-change provenance ("git commit for trading logic") ----------
    def log_weight_change_provenance(self, id: str, bucket: str, mode: str, old_weight: float,
                                      new_weight: float, strategy_version=None, config_hash: str = None,
                                      feature_ranking=None, walk_forward_report=None,
                                      champion_challenge_id: str = None, trade_count: int = None,
                                      decision: str = "", decision_reason: str = ""):
        """Called from learning/bayesian_updater.py's apply_bucket_weight_to_config()
        on every attempted weight change - accepted (shadow-validated or
        force-applied) AND rejected (blocked by ShadowValidationRequired) -
        so the rejection itself is on record too, not just successful
        changes. id: caller-generated (e.g. f"{bucket}-{mode}-{timestamp}")
        so it's stable/referenceable before the row exists."""
        import json
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO weight_change_log
                (id, created_at, bucket, mode, old_weight, new_weight, strategy_version, config_hash,
                 feature_ranking, walk_forward_report, champion_challenge_id, trade_count, decision,
                 decision_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    decision=excluded.decision, decision_reason=excluded.decision_reason""",
                (id, datetime.utcnow().isoformat(), bucket, mode, old_weight, new_weight,
                 json.dumps(strategy_version) if strategy_version is not None else None,
                 config_hash,
                 json.dumps(feature_ranking) if feature_ranking is not None else None,
                 json.dumps(walk_forward_report) if walk_forward_report is not None else None,
                 champion_challenge_id, trade_count, decision, decision_reason),
            )

    @staticmethod
    def _parse_weight_change_row(row: dict) -> dict:
        import json
        for col in ("strategy_version", "feature_ranking", "walk_forward_report"):
            raw = row.get(col)
            row[col] = json.loads(raw) if raw else None
        return row

    def get_weight_change_provenance(self, id: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM weight_change_log WHERE id = ?", (id,)).fetchone()
            return self._parse_weight_change_row(dict(row)) if row else None

    def get_weight_change_history(self, bucket: str = None, mode: str = None, limit: int = 50):
        """Answers 'why did we change TREND from 21% to 23%' six months from
        now - full provenance trail, optionally filtered to one bucket and/or
        mode, newest first."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            clauses, params = [], []
            if bucket:
                clauses.append("bucket = ?")
                params.append(bucket)
            if mode:
                clauses.append("mode = ?")
                params.append(mode)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            cur = conn.execute(
                f"SELECT * FROM weight_change_log {where} ORDER BY created_at DESC LIMIT ?", params
            )
            return [self._parse_weight_change_row(dict(r)) for r in cur.fetchall()]

    # ---------- cycles ----------
    def log_cycle(self, cycle_num: int, ticker_count: int, blocked: bool = False,
                  reason: str = "", duration: float = None, triggered_by: str = "scheduler"):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO cycles (timestamp, cycle_num, ticker_count, blocked, reason, duration, triggered_by)
                VALUES (?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), cycle_num, ticker_count, int(blocked), reason, duration, triggered_by),
            )
            today = date.today().isoformat()
            conn.execute(
                """INSERT INTO daily_stats (date, cycles_run) VALUES (?, 1)
                   ON CONFLICT(date) DO UPDATE SET cycles_run = daily_stats.cycles_run + 1""",
                (today,),
            )

    def get_last_cycle(self):
        """Most recent cycles-table row, used by server.py's /api/status to
        show 'last scanned at HH:MM' / 'N cycles today' in the UI."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def increment_cycle(self) -> int:
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO daily_stats (date, cycles_run) VALUES (?, 1)
                   ON CONFLICT(date) DO UPDATE SET cycles_run = daily_stats.cycles_run + 1""",
                (today,),
            )
            row = conn.execute("SELECT cycles_run FROM daily_stats WHERE date = ?", (today,)).fetchone()
            return row[0] if row else 1

    # ---------- trades ----------
    def log_trade(self, ticker: str, side: str, amount: float, shares: float = None,
                  fill_price: float = None, order_id: str = None, status: str = "unknown",
                  snapshot_id: str = None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trades (timestamp, ticker, side, amount, shares, fill_price, order_id, status, snapshot_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), ticker, side, round(amount, 2) if amount else amount,
                 shares, fill_price, order_id, status, snapshot_id),
            )
            today = date.today().isoformat()
            conn.execute(
                """INSERT INTO daily_stats (date, trades_placed) VALUES (?, 1)
                   ON CONFLICT(date) DO UPDATE SET trades_placed = daily_stats.trades_placed + 1""",
                (today,),
            )

    def get_recent_trades(self, limit: int = 20):
        """Includes the parsed snapshot inline (not just snapshot_id) so the
        UI's Journal tab can render the indicator/rule detail without a
        second round-trip per row - these are small JSON blobs, not worth a
        separate fetch-on-expand endpoint."""
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["snapshot"] = None
                if r.get("snapshot_id"):
                    snap = self.get_trade_snapshot(r["snapshot_id"])
                    if snap and snap.get("data"):
                        try:
                            r["snapshot"] = json.loads(snap["data"])
                        except (ValueError, TypeError):
                            r["snapshot"] = None
            return rows

    # ---------- positions ----------
    # `simulated` filter convention (paper trading, 2026-07-16): None = both
    # books (back-compat - callers that existed before paper trading see the
    # union), False = real book only (confirm_fill.py-managed), True =
    # simulated/WATCH-mode book only. COALESCE(simulated, 0) because rows
    # created before the migration have NULL.
    def get_open_position(self, ticker: str, simulated: bool = None):
        q = "SELECT * FROM positions WHERE ticker = ? AND status = 'open'"
        if simulated is not None:
            q += f" AND COALESCE(simulated, 0) = {1 if simulated else 0}"
        q += " ORDER BY id DESC LIMIT 1"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(q, (ticker,)).fetchone()
            return dict(row) if row else None

    def open_position(self, ticker: str, entry_price: float, shares: float, dollar_amount: float,
                       pattern_id: int = None, simulated: bool = False, entry_time: str = None,
                       trade_mode: str = None):
        """Unconditional open. Returns the new row id, or None if this book
        already holds an open position in this ticker.

        The ON CONFLICT clause was added with §14's unique index. It is
        deliberately DO NOTHING rather than letting the constraint raise,
        because of where the live callers sit: engine/live_trader.py calls
        this AFTER a real order has filled, and confirm_fill.py after a human
        has confirmed one. An exception there would mean a fill that happened
        and was never recorded, which is a strictly worse outcome than the
        duplicate the index exists to prevent - the account would hold shares
        the system had no row for.

        So the collision is survivable and LOUD instead. Callers that care -
        anything that needs to know it actually got the position - should use
        try_open_position(), which takes the lock, enforces the cap and settles
        the purse in the same transaction.
        """
        with self._conn() as conn:
            row = conn.execute(
                """INSERT INTO positions
                (ticker, entry_price, entry_time, shares, dollar_amount, trail_high, status, pattern_id, simulated, trade_mode)
                VALUES (?,?,?,?,?,?, 'open', ?, ?, ?)
                ON CONFLICT DO NOTHING
                RETURNING id""",
                (ticker, entry_price, entry_time or datetime.utcnow().isoformat(), shares,
                 dollar_amount, entry_price, pattern_id, 1 if simulated else 0,
                 trade_mode.upper() if trade_mode else None),
            ).fetchone()
        if row is None:
            logger.critical(
                f"open_position({ticker}, simulated={simulated}, "
                f"trade_mode={trade_mode}): this book ALREADY holds an open "
                f"{ticker} - no row written. If a real fill preceded this "
                f"call, the account now holds more {ticker} than the existing "
                f"position row records. Reconcile by hand; "
                f"scripts/reconcile.py will not catch this one, because the "
                f"row that is there looks perfectly consistent.")
            return None
        return row[0] if not isinstance(row, dict) else row.get("id")

    # ── §14 (Phase 2): opening a position is ONE transaction ────────────────
    #
    # execute_buy() used to read get_open_position() at line 113 and call
    # open_position() at line 215 - two separate auto-committed transactions,
    # running inside a ThreadPoolExecutor with cycle_max_parallel_tickers: 6.
    # The max_positions count had the same shape, and so did the cash check.
    # A repo-wide grep for CREATE UNIQUE across this file returned exactly one
    # hit, on news_items(ticker, headline). Nothing protected positions.
    #
    # Two workers processing the same ticker in one cycle, or six workers each
    # reading open_count = 24 against a cap of 25, all passed the check and all
    # wrote. The window widened on 17 July, when the process-level lock that
    # once wrapped every _conn() call was removed to fix an unrelated hang -
    # the right call for the hang, but it took away incidental serialisation
    # without replacing it with real protection.
    #
    # Application-level checks cannot fix this. Only the database can.

    def try_debit_paper_cash(self, amount: float, conn=None) -> bool:
        """Conditional debit. Returns False if the purse could not cover it.

        The WHERE clause does the check, so the read and the write are the
        same statement and cannot be interleaved. execute_buy() previously
        read account['cash'], compared it to the amount, and called
        adjust_paper_cash(-amount) some 25 lines later: two concurrent buys
        could both see $100 and both spend it.

        `conn` lets a caller run this inside an existing transaction - which
        is how try_open_position() makes the debit and the insert succeed or
        fail together, rather than leaving a position that was never paid for
        or cash that bought nothing.
        """
        if amount is None or amount <= 0:
            return False
        sql = ("UPDATE paper_account SET cash = cash - ?, updated_at = ? "
               "WHERE id = 1 AND cash >= ?")
        params = (amount, datetime.utcnow().isoformat(), amount)
        if conn is not None:
            return conn.execute(sql, params).rowcount == 1
        with self._conn() as c:
            return c.execute(sql, params).rowcount == 1

    def try_open_position(self, *args, **kwargs):
        """Open a position, or return None. One transaction.

        None means "another worker won" - the duplicate check, the cap check,
        the cash debit and the insert cannot be interleaved, so the loser of
        a race gets an empty result rather than an exception. That matters:
        catching IntegrityError as flow control would put the normal path
        inside an exception handler.

        Three protections, because there are three distinct ways to get this
        wrong and no single mechanism covers all of them:

          A transaction-scoped ADVISORY LOCK serialises position-opening for
          this book, which is what makes the duplicate check and the cap check
          below mean anything. Note this is NOT the SELECT ... FOR UPDATE the
          remediation plan specifies, and the difference is load-bearing: FOR
          UPDATE locks the rows it finds, so it serialises correctly at
          24-of-25 but not at 0-of-25, where there are no rows to lock and six
          workers can all insert. An advisory lock is the one mechanism that
          still holds when the table is empty - which is the state every
          trading day starts in.

          The partial UNIQUE INDEX uq_open_position_per_ticker_book (migration
          006) makes the invariant structural rather than procedural. Under
          the lock above it should never fire from this method; it is here for
          the paths that do not take the lock (open_position, account_sync,
          confirm_fill) and for the next one somebody writes. ON CONFLICT DO
          NOTHING ... RETURNING means the loser of such a race gets an empty
          result instead of an exception.

          The CASH is protected by try_debit_paper_cash's conditional UPDATE,
          run on this same connection so that if the insert then yields
          nothing, the rollback takes the debit with it and the purse is never
          charged for a position that does not exist.

        `excluded_modes` defaults to SEED only, matching execute_buy: SEED
        rows are an informational mirror of the real account, and counting
        them toward max_positions would starve out genuine signals just
        because the real book happens to hold a lot of names. SYNC cannot
        appear in the paper book; a live caller should pass
        MANAGED_EXCLUDED_MODES.
        """
        try:
            return self._try_open_position_txn(*args, **kwargs)
        except _PositionRaceLost:
            # Losing is a normal outcome, not an error. The exception exists
            # only so the transaction rolls back; it stops here.
            return None

    def _try_open_position_txn(self, ticker: str, entry_price: float, shares: float,
                               dollar_amount: float, pattern_id: int = None,
                               simulated: bool = False, entry_time: str = None,
                               trade_mode: str = None, max_positions: int = None,
                               excluded_modes: tuple = ("SEED",),
                               debit_paper_cash: float = None):
        """The transaction itself. See try_open_position() for the reasoning."""
        sim = 1 if simulated else 0
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row

            # Transaction-scoped, so it is released on commit OR rollback with
            # no explicit unlock. A lock that has to be unwound by hand is one
            # that leaks the first time something raises between the two calls.
            conn.execute("SELECT pg_advisory_xact_lock(?, ?)",
                         (_OPEN_POSITION_LOCK_KEY, sim))

            if conn.execute(
                    "SELECT 1 FROM positions WHERE ticker = ? AND status = 'open' "
                    "AND COALESCE(simulated, 0) = ? LIMIT 1",
                    (ticker, sim)).fetchone():
                return None

            if max_positions is not None:
                rows = conn.execute(
                    "SELECT trade_mode FROM positions "
                    "WHERE status = 'open' AND COALESCE(simulated, 0) = ?",
                    (sim,)).fetchall()
                excluded = tuple(m.upper() for m in (excluded_modes or ()))
                counted = [r for r in rows
                           if str(r["trade_mode"] or "").upper() not in excluded]
                if len(counted) >= max_positions:
                    return None

            if debit_paper_cash is not None and not self.try_debit_paper_cash(
                    debit_paper_cash, conn=conn):
                return None

            row = conn.execute(
                """INSERT INTO positions
                   (ticker, entry_price, entry_time, shares, dollar_amount,
                    trail_high, status, pattern_id, simulated, trade_mode)
                   VALUES (?,?,?,?,?,?, 'open', ?, ?, ?)
                   ON CONFLICT DO NOTHING
                   RETURNING *""",
                (ticker, entry_price, entry_time or datetime.utcnow().isoformat(),
                 shares, dollar_amount, entry_price, pattern_id, sim,
                 trade_mode.upper() if trade_mode else None)).fetchone()

            if row is None:
                # Only reachable if something outside this method inserted the
                # row while the lock was held - i.e. a path that bypasses the
                # lock. Roll back so the debit above does not stand alone, and
                # say so loudly: this is the index doing a job the lock was
                # supposed to have made unnecessary.
                logger.error(
                    f"try_open_position({ticker}, simulated={simulated}): the "
                    f"unique index rejected the insert while the advisory lock "
                    f"was held. Some other code path opens positions without "
                    f"taking it - find it.")
                raise _PositionRaceLost(ticker)

            return dict(row)

    def close_position(self, ticker: str, exit_price: float, simulated: bool = False) -> dict:
        """Returns the closed position's details (entry_price, entry_time, shares,
        pattern_id, pnl, pnl_pct) so the caller (confirm_fill.py or
        engine/paper_trader.py) can close the linked pattern_database entry with
        the real outcome. Returns {} if there was no open position for this
        ticker in the requested book. Defaults to the REAL book so
        confirm_fill.py's behavior is unchanged - a real sell must never close
        the simulated clone of the same ticker (and vice versa). Simulated
        closes stay OUT of daily_stats: that table feeds the risk engine's
        max_daily_loss guard and reports REAL money only."""
        sim_flag = 1 if simulated else 0
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM positions WHERE ticker = ? AND status = 'open' "
                "AND COALESCE(simulated, 0) = ? ORDER BY id DESC LIMIT 1",
                (ticker, sim_flag),
            ).fetchone()
            if not row:
                return {}
            row = dict(row)
            conn.execute(
                "UPDATE positions SET status = 'closed' WHERE ticker = ? AND status = 'open' "
                "AND COALESCE(simulated, 0) = ?", (ticker, sim_flag)
            )
            entry_price, shares = row["entry_price"], row["shares"]
            pnl = (exit_price - entry_price) * shares if entry_price and shares else 0.0
            pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0.0
            # §8 (Phase 2): BOTH books now record. This used to be
            # `if not simulated`, on the reasoning that daily_stats feeds the
            # risk engine's max_daily_loss guard and must report real money
            # only. That was sound while paper trading was a display feature.
            # It stopped being sound the moment paper became the primary
            # operating mode, because it meant the $500 daily loss limit read
            # $0.00 every day, forever, on the only book that was trading.
            #
            # Separate columns, not a shared one: a paper drawdown trips the
            # paper session's limit without contaminating the live ledger, so
            # the real-money-only guarantee on `realized_pnl` - which is what
            # the auditors of that column actually needed - is preserved
            # exactly.
            today = date.today().isoformat()
            pnl_col = "paper_realized_pnl" if simulated else "realized_pnl"
            win_col = "paper_winning_trades" if simulated else "winning_trades"
            conn.execute(
                f"""INSERT INTO daily_stats (date, {pnl_col}, {win_col}) VALUES (?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                      {pnl_col} = daily_stats.{pnl_col} + excluded.{pnl_col},
                      {win_col} = daily_stats.{win_col} + excluded.{win_col}""",
                (today, pnl, 1 if pnl > 0 else 0),
            )
            # §15: hold_hours is computed HERE and nowhere else.
            #
            # ADPT appeared in paper_trades as -1.88% over 6.34h and in
            # mae_mfe_data as -3.20% over 5.0h - the same trade, two answers.
            # The cause was two independent computations: close_position did
            # the arithmetic for the ledger, and the MAE/MFE path redid it
            # from a re-fetched row a few statements later, against a
            # different clock reading and sometimes a different row. One
            # definition, one caller, one answer.
            hold_hours = 0.0
            try:
                entry_dt = datetime.fromisoformat(row["entry_time"])
                hold_hours = (datetime.utcnow() - entry_dt).total_seconds() / 3600
            except (TypeError, ValueError):
                logger.warning(f"close_position({ticker}): unparseable entry_time "
                               f"{row.get('entry_time')!r} - hold_hours recorded as 0")
            return {
                "ticker": ticker, "entry_price": entry_price, "entry_time": row["entry_time"],
                "shares": shares, "pattern_id": row.get("pattern_id"),
                "exit_price": exit_price, "pnl": pnl, "pnl_pct": pnl_pct,
                "hold_hours": hold_hours,
                "trade_mode": row.get("trade_mode"),
            }

    def update_trail_high(self, ticker: str, new_high: float,
                          simulated: bool = None):
        """Ratchet the trailing high for the open position in `ticker`.

        §16, found while migrating update_position_by_ticker: this method had
        exactly the same defect and it was arguably the worse of the two,
        because it fires on EVERY cycle rather than once at entry. Unscoped,
        the paper book's trail_high was written onto the real row of the same
        ticker and vice versa - and trail_high is what the trailing stop is
        computed from, so the consequence is a real holding whose stop tracks
        a paper position's price history.

        scheduler.py made this concrete: it updates `paper_position` and
        `position` on consecutive lines, for the same ticker, through this
        same unscoped statement. Both writes hit both rows.

        Raises on simulated=None for the same reason as
        update_position_by_ticker: a silent default would leave an unmigrated
        caller writing to the real book, invisibly.
        """
        if simulated is None:
            raise ValueError(
                "update_trail_high requires simulated=True/False - an unscoped "
                "ratchet writes one book's high onto the other's trailing "
                "stop (§16).")
        with self._conn() as conn:
            conn.execute(
                "UPDATE positions SET trail_high = GREATEST(COALESCE(trail_high, 0), ?) "
                "WHERE ticker = ? AND status = 'open' AND COALESCE(simulated, 0) = ?",
                (new_high, ticker, 1 if simulated else 0),
            )

    def get_all_positions(self, simulated: bool = None):
        """EVERY open row, including unmanaged ones. simulated: None = both
        books (back-compat), False = real only, True = simulated (paper) only.

        Use this ONLY for display, reconciliation and position-count maths.
        Any code path that can CLOSE a position must call
        get_managed_positions() instead - see MANAGED_EXCLUDED_MODES."""
        q = "SELECT * FROM positions WHERE status = 'open'"
        if simulated is not None:
            q += f" AND COALESCE(simulated, 0) = {1 if simulated else 0}"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(q)
            return [dict(r) for r in cur.fetchall()]

    def get_managed_positions(self, simulated: bool = None) -> list:
        """Positions this ENGINE opened and may therefore close (§5, Phase 1).

        Excludes SYNC (engine/account_sync.py's import of real Robinhood
        holdings) and SEED (robinhood_sync.py's paper mirror of the real
        book) - both are informational, and neither represents a decision
        this system made. Their size bears no relation to
        trading.trade_size_usd, so a stop sized for a $100 engine entry
        would be liquidating thousands of dollars of unrelated capital: as
        of the 2026-07-24 audit the eight SYNC rows totalled ~$42,000 and
        carried LIVE stop machinery (KMB in TREND_FOLLOWING, RVI in
        PROFIT_PROTECT, SMFL with a stop exactly equal to its entry).

        Every automated exit path must use this, never get_all_positions().
        This is layer 1 of three; rules/sell_rules.py's evaluate() and
        engine/live_trader.py's execute_sell_live() repeat the check, because
        a single guard on a $42,000 exposure is a single point of failure.

        `simulated` follows get_all_positions' semantics exactly (None = both
        books) so this is a drop-in replacement at every call site. The
        remediation plan's sketch defaulted to False; defaulting to None here
        avoids silently narrowing the two callers that legitimately manage
        both books (engine/position_management.py's Loop B being the one that
        matters).
        """
        q = ("SELECT * FROM positions WHERE status = 'open' "
             "AND COALESCE(UPPER(trade_mode), '') NOT IN ('SYNC', 'SEED')")
        if simulated is not None:
            q += f" AND COALESCE(simulated, 0) = {1 if simulated else 0}"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(q)
            return [dict(r) for r in cur.fetchall()]

    def is_managed(self, ticker: str, simulated: bool = False) -> bool:
        """True when an open position exists for `ticker` AND this engine is
        allowed to close it. False for no position at all, and False for a
        SYNC/SEED row - callers must not treat 'not managed' as 'not held'."""
        pos = self.get_open_position(ticker, simulated=simulated)
        if not pos:
            return False
        return not is_unmanaged_mode(pos.get("trade_mode"))

    def get_open_position_by_pattern(self, pattern_id: int, simulated: bool = True):
        """Used by scheduler._close_due_patterns() to skip the time-based
        simulated close for any pattern whose outcome will instead come from
        a rule-driven paper-trade exit (the whole point of WATCH mode)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM positions WHERE pattern_id = ? AND status = 'open' "
                "AND COALESCE(simulated, 0) = ? LIMIT 1",
                (pattern_id, 1 if simulated else 0),
            ).fetchone()
            return dict(row) if row else None

    # ---------- paper trading (WATCH-mode purse + trade ledger) ----------
    def get_paper_account(self):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
            return dict(row) if row else None

    def init_paper_account(self, starting_cash: float):
        """Idempotent - keeps the existing purse if one is already seeded, so
        every WATCH-mode cycle can call this without resetting the account."""
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO paper_account (id, starting_cash, cash, realized_pnl, created_at, updated_at)
                   VALUES (1, ?, ?, 0, ?, ?) ON CONFLICT(id) DO NOTHING""",
                (starting_cash, starting_cash, now, now),
            )
        return self.get_paper_account()

    def adjust_paper_cash(self, delta: float, realized_pnl_delta: float = 0.0):
        with self._conn() as conn:
            conn.execute(
                "UPDATE paper_account SET cash = cash + ?, realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = 1",
                (delta, realized_pnl_delta, datetime.utcnow().isoformat()),
            )

    def credit_paper_capital(self, amount: float) -> dict:
        """Add (or withdraw, if negative) CAPITAL to the paper purse - a
        deposit, not a gain (2026-07-26, Akhil: raise the paper book to
        $10,000 so it trades more and produces a usable sample sooner).

        starting_cash and cash move together, by the same amount, in one
        statement. That is the whole point of this method existing rather than
        callers reaching for adjust_paper_cash():

          - scripts/reconcile.py's headline invariant is
            ``cash == starting_cash - net_buys_from_the_ledger``. Moving cash
            alone breaks it, and that check exists precisely to catch silent
            purse drift, so it would have started reporting a real-looking
            accounting fault every run.
          - paper_trader.snapshot() computes total_return_pct against
            starting_cash. A $9,000 deposit onto a $1,000 basis would read as
            roughly +900% return - a deposit is not performance.

        realized_pnl is deliberately untouched: capital in is not P&L.

        Does NOT write a paper_equity_history point. The next WATCH cycle
        writes one, and the step up it produces is handled - update_drawdown()
        measures against a running peak, so a jump raises the peak rather than
        registering as a fall (the mirror-image case, a re-seed DOWNWARD, is
        the one that caused the v1.3.1 false-drawdown incident; see
        _paper_epoch_start's docstring).
        """
        if not amount:
            return self.get_paper_account()
        with self._conn() as conn:
            conn.execute(
                "UPDATE paper_account SET starting_cash = starting_cash + ?, "
                "cash = cash + ?, updated_at = ? WHERE id = 1",
                (amount, amount, datetime.utcnow().isoformat()),
            )
        logger.info(f"paper account: capital {'credited' if amount > 0 else 'withdrawn'} "
                    f"${abs(amount):,.2f} (starting_cash and cash both moved; "
                    f"realized_pnl untouched)")
        return self.get_paper_account()

    def reset_paper_account(self):
        """Wipes the purse, ledger, equity curve and every simulated position
        (open or closed) - a clean slate for the next WATCH session. Real book
        and pattern_database untouched.

        §48 (Phase 2.5) added paper_equity_history to this list. It was left
        behind, which meant a "clean slate" account inherited the PREVIOUS
        account's equity curve - and that curve is the input to every drawdown
        figure. v1.3.1 exists because of what a mid-day re-seed did to exactly
        that arithmetic: a 1491 -> 1000 step read as a 33% intraday drawdown
        and, against the 2.0% cap, blocked entries for the rest of the day for
        an accounting event. The epoch guard added there is still correct and
        still needed for a re-SEED that is not a reset; this makes a RESET
        genuinely start from nothing, so the guard is belt-and-braces rather
        than load-bearing.

        Deliberately NOT deleted: pattern_database (the learning record,
        including closed outcomes) and mae_mfe_data. Those survive a reset by
        design - see scripts/assess_test_damage.py, which is the required step
        before running this. Note that mae_mfe_data surviving is a
        double-edged property: §49 found it still holding test residue
        precisely because nothing routinely clears it.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM paper_account")
            conn.execute("DELETE FROM paper_trades")
            conn.execute("DELETE FROM paper_equity_history")
            conn.execute("DELETE FROM positions WHERE COALESCE(simulated, 0) = 1")

    def remove_seed_positions(self) -> dict:
        """Undoes robinhood_sync.py's seed-paper command: deletes every
        trade_mode='SEED' row it cloned into the simulated book (real
        Robinhood holdings mirrored in for display), credits their cost
        basis back to paper_account.cash so the purse stays consistent, and
        removes the matching 'seeded_from_robinhood' ledger lines from
        paper_trades so the Journal doesn't show orphaned buys.

        Why this exists (2026-07-23, Trinath's ask): SEED positions are
        informational clones of a real account, not something the WATCH-mode
        engine is actually managing - but engine/paper_trader.py's
        max_positions/max_day_positions caps used to count ALL simulated
        open positions, SEED included, so a mirrored real portfolio could
        silently eat most or all of the 10-position budget and starve out
        genuine new WATCH signals. paper_trader.py now excludes trade_mode=
        'SEED' from those counts going forward regardless of whether this
        has been run; this method is for cleaning up SEED rows already
        sitting in the DB from a previous seed-paper run."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM positions WHERE COALESCE(simulated, 0) = 1 "
                "AND status = 'open' AND UPPER(COALESCE(trade_mode, '')) = 'SEED'"
            ).fetchall()
            rows = [dict(r) for r in rows]
            if not rows:
                return {"removed": 0, "cash_credited": 0.0}
            total_cost = sum((r.get("dollar_amount") or 0) for r in rows)
            tickers = [r["ticker"] for r in rows]
            conn.execute(
                "DELETE FROM positions WHERE COALESCE(simulated, 0) = 1 "
                "AND status = 'open' AND UPPER(COALESCE(trade_mode, '')) = 'SEED'"
            )
            conn.execute(
                "DELETE FROM paper_trades WHERE reason = 'seeded_from_robinhood'"
            )
            if total_cost:
                conn.execute(
                    "UPDATE paper_account SET cash = cash + ?, updated_at = ? WHERE id = 1",
                    (total_cost, datetime.utcnow().isoformat()),
                )
        return {"removed": len(rows), "cash_credited": round(total_cost, 2), "tickers": tickers}

    def remove_synced_positions(self) -> dict:
        """Real-book counterpart to remove_seed_positions(): deletes every
        trade_mode='SYNC' row engine/account_sync.py auto-imported from a
        Robinhood account (config.yaml account.auto_sync, once per cycle
        while enabled). Real money/actual Robinhood holdings are completely
        unaffected - account_sync.py is read-only against the brokerage, and
        so is this: it only removes the LOCAL tracking row, so the platform
        stops counting/health-scoring/stop-managing a position it never
        actually decided to enter itself.

        Why this exists (2026-07-23, Trinath's ask, same rationale as
        remove_seed_positions(): a SYNC row counted toward
        trading.max_positions in engine/live_trader.py exactly like a real
        self-initiated position, so an account holding several unrelated
        names could crowd out the real-book trading budget. live_trader.py
        now excludes trade_mode='SYNC' from that count going forward; this
        cleans up rows already imported from a prior sync. Doesn't disable
        account.auto_sync itself - flip that off in config.yaml/Control tab
        separately if you don't want this to happen again."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM positions WHERE COALESCE(simulated, 0) = 0 "
                "AND status = 'open' AND UPPER(COALESCE(trade_mode, '')) = 'SYNC'"
            ).fetchall()
            rows = [dict(r) for r in rows]
            if not rows:
                return {"removed": 0, "tickers": []}
            tickers = [r["ticker"] for r in rows]
            conn.execute(
                "DELETE FROM positions WHERE COALESCE(simulated, 0) = 0 "
                "AND status = 'open' AND UPPER(COALESCE(trade_mode, '')) = 'SYNC'"
            )
        return {"removed": len(rows), "tickers": tickers}

    def log_paper_trade(self, ticker: str, side: str, price: float, shares: float,
                         dollar_amount: float, reason: str = None, pattern_id: int = None,
                         pnl: float = None, pnl_pct: float = None, trade_mode: str = None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO paper_trades
                (ticker, side, price, shares, dollar_amount, reason, pattern_id, pnl, pnl_pct, created_at, trade_mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, side, price, shares, dollar_amount, reason, pattern_id,
                 pnl, pnl_pct, datetime.utcnow().isoformat(),
                 trade_mode.upper() if trade_mode else None),
            )

    def get_paper_trades(self, limit: int = 100):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def record_paper_equity(self, snap: dict):
        """One equity-curve point per WATCH cycle, from paper_trader.snapshot()."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO paper_equity_history
                (timestamp, total_value, cash, invested_cost, market_value,
                 unrealized_pnl, realized_pnl, n_open)
                VALUES (?,?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), snap.get("total_value"), snap.get("cash"),
                 snap.get("invested_cost"), snap.get("market_value"),
                 snap.get("unrealized_pnl"), snap.get("realized_pnl"), snap.get("n_open")),
            )
        # §11: recompute drawdown here rather than from a scheduled job. The
        # curve only moves when a point is appended, so the only moment the
        # figure can go stale is the moment immediately after this insert.
        # A separate job would add a second thing that has to be running for a
        # risk control to be current - and a risk control that silently stops
        # updating is worse than one that was never wired up, because the
        # dashboard keeps showing a number.
        try:
            self.update_drawdown(simulated=True)
        except Exception as e:
            # Never let a metric failure lose the equity point that was just
            # written - the point is the raw material, the metric is derived
            # and can be recomputed from it by scripts/backfill_drawdown.py.
            logger.warning(f"update_drawdown after equity insert failed: {e}")

    def update_drawdown(self, simulated: bool = True) -> dict:
        """Recompute today's drawdown for one book from its equity curve.

        Two numbers, because they answer different questions:

          intraday_dd - worst peak-to-trough WITHIN today. "How bad did it get
                        before it came back?" This is what a daily circuit
                        breaker should watch.
          running_dd  - drawdown from the all-time equity high. "How far from
                        my best am I?" This is what says whether the strategy
                        is still working.

        Only the paper book has an equity curve today (paper_equity_history);
        `simulated=False` is accepted and returns without writing, so the live
        columns stay honestly zero rather than being fed paper numbers. When a
        live curve exists, point this at it - the callers and the risk gate do
        not change.

        On the equity series: `total_value` from paper_trader.snapshot(), which
        carries unpriced positions at COST. That matters here for the same
        reason it matters in daily_loss_limit() - a quoteless cycle must not
        register as a portfolio that lost all its market value, which would
        manufacture a 100% drawdown and halt the session for entirely the
        wrong reason.

        Returns the computed figures (or {} when there was nothing to compute)
        so callers and tests can assert on them without a second read.
        """
        if not simulated:
            return {}

        today = date.today().isoformat()
        start_utc, end_utc = _local_day_window_utc()

        # The epoch bounds the INTRADAY window too, not just the peak below.
        #
        # Fixing only the peak left half the bug in place: a reset that happens
        # MID-DAY puts the discontinuity inside today's window, so the
        # peak-to-trough scan runs straight across it. A re-seed downward - say
        # 1491 back to a 1000 starting_cash - would read as a 33% intraday
        # drawdown and block entries for the rest of the day, for an accounting
        # event. The 2026-07-25 re-seed happened to step UP, which produces no
        # drawdown, so the live data did not expose this.
        #
        # max() rather than replacing the window: on any ordinary day the epoch
        # is older than midnight and the day window is what binds.
        epoch = self._paper_epoch_start()
        if epoch and epoch > start_utc:
            start_utc = epoch

        with self._conn() as conn:
            rows = conn.execute(
                """SELECT total_value FROM paper_equity_history
                    WHERE timestamp >= ? AND timestamp < ?
                      AND total_value IS NOT NULL
                    ORDER BY timestamp, id""",
                (start_utc, end_utc)).fetchall()
            if len(rows) < 2:
                # One point is a level, not a curve. Writing 0 here would be a
                # claim ("no drawdown today"), and it is not one we can make.
                return {}
            eq = [float(r[0]) for r in rows]

            peak, intraday_dd = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                if peak > 0:
                    intraday_dd = max(intraday_dd, (peak - v) / peak * 100)

            # The peak is taken over the CURRENT ACCOUNT EPOCH, not over the
            # whole table (2026-07-25, found by running the backfill against
            # real data).
            #
            # A re-seed replaces the purse while paper_equity_history survives,
            # so the curve can step discontinuously. (§48, Phase 2.5 narrowed
            # this: reset_paper_account() now clears the curve too, so a full
            # RESET no longer produces a discontinuity. A RE-SEED still does -
            # robinhood_sync can change the balance without a reset - so this
            # guard stays load-bearing for that case and belt-and-braces for
            # the other.) On this machine the curve ran at ~984 for eight days
            # and then jumped to 1491.54 when the account was re-seeded at a
            # higher balance. An all-table MAX makes that jump the all-time
            # high, so every subsequent day reads a ~34% running drawdown
            # against a 15% cap - and since a running breach trips the kill
            # switch, the next cycle would have halted trading entirely on the
            # strength of an accounting event.
            #
            # An equity series from a different starting balance is a
            # different series. Comparing them produces a drawdown that never
            # happened, which is the same class of error as the market_value
            # equity bug: a number that is arithmetically derived, obviously
            # wrong to a human, and completely invisible to the control that
            # consumes it.
            if epoch:
                peak_row = conn.execute(
                    "SELECT MAX(total_value) FROM paper_equity_history "
                    "WHERE timestamp >= ?", (epoch,)).fetchone()
            else:
                peak_row = conn.execute(
                    "SELECT MAX(total_value) FROM paper_equity_history").fetchone()
            all_time_peak = float(peak_row[0] or eq[-1])
            running_dd = (((all_time_peak - eq[-1]) / all_time_peak * 100)
                          if all_time_peak > 0 else 0.0)
            running_dd = max(0.0, running_dd)

            intraday_dd = round(intraday_dd, 3)
            running_dd = round(running_dd, 3)

            conn.execute(
                """INSERT INTO daily_stats
                    (date, paper_max_drawdown, paper_running_drawdown)
                    VALUES (?, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET
                      paper_max_drawdown = GREATEST(
                          COALESCE(daily_stats.paper_max_drawdown, 0),
                          excluded.paper_max_drawdown),
                      paper_running_drawdown = excluded.paper_running_drawdown""",
                (today, intraday_dd, running_dd))

        # GREATEST on the intraday figure, plain assignment on the running one,
        # and the asymmetry is the point. max_drawdown is a HIGH-WATER MARK for
        # the day: a 4% dip at 10am is a fact about today that stays true at
        # 3pm, and overwriting it the moment equity recovered would erase
        # precisely the number you most want to keep. running_drawdown is a
        # CURRENT distance from the all-time high, so the latest value is the
        # only correct one - a high-water mark there would be a permanent
        # record of the worst day the account ever had, which is a different
        # statistic and already recoverable from this table.
        return {"date": today, "paper_max_drawdown": intraday_dd,
                "paper_running_drawdown": running_dd}

    def _paper_epoch_start(self) -> str | None:
        """When the CURRENT paper account was created, as a naive-UTC string.

        The boundary for running drawdown (§11). paper_account is a single row
        that reset_paper_account() deletes and the next seed recreates, so
        created_at marks the start of the equity series that is actually
        comparable to today.

        §48 (Phase 2.5) made reset_paper_account() clear paper_equity_history
        as well, so after a RESET the boundary and the table agree and this is
        a no-op. It remains necessary for a RE-SEED - robinhood_sync can change
        the balance without deleting the account - and for any database that
        predates §48. Consulting the boundary rather than assuming the table is
        clean is the cheaper of the two mistakes.

        Returns None when there is no account or no created_at, in which case
        callers fall back to the whole table - a missing boundary should widen
        the window, not silently empty it.
        """
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT created_at FROM paper_account WHERE id = 1").fetchone()
            return row[0] if row and row[0] else None
        except Exception as e:
            logger.warning(f"could not read the paper-account epoch: {e}")
            return None

    def backfill_drawdown(self) -> int:
        """Recompute paper drawdown for EVERY day in paper_equity_history.

        The curve already holds real history, so the metric can start with a
        past instead of starting blank - which is what makes the caps in
        config.yaml settable from evidence rather than guessed. Idempotent:
        recomputed from the curve each time, so it can be re-run after any
        correction to the equity series. Returns the number of days written.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT timestamp, total_value FROM paper_equity_history
                    WHERE total_value IS NOT NULL
                    ORDER BY timestamp, id""").fetchall()

        by_day, running_peak, written = {}, 0.0, 0
        offset = datetime.utcnow() - datetime.now()   # naive UTC->local, as elsewhere
        # Same epoch boundary as update_drawdown. Points from before the
        # current account was created belong to a different equity series, and
        # carrying their peak forward manufactures a drawdown that never
        # happened - see update_drawdown for the 1491.54 case that found this.
        epoch = self._paper_epoch_start()
        epoch_day = None
        if epoch:
            try:
                epoch_day = (datetime.fromisoformat(epoch) - offset).date().isoformat()
            except (TypeError, ValueError):
                epoch_day = None
        for r in rows:
            try:
                local_day = (datetime.fromisoformat(r["timestamp"]) - offset).date().isoformat()
            except (TypeError, ValueError):
                continue
            # On the epoch DAY itself, drop points from before the reset. That
            # day is the only one whose points span two different accounts, so
            # scanning it whole would run the peak-to-trough calculation across
            # the discontinuity - a re-seed downward reading as a large
            # intraday drawdown that never happened.
            #
            # Earlier days keep all their points. They belong to the previous
            # account, and their intraday figures are self-contained and true
            # for it; dropping them would discard real history to fix a
            # boundary problem that only exists at the boundary.
            if epoch and local_day == epoch_day and r["timestamp"] < epoch:
                continue
            by_day.setdefault(local_day, []).append(float(r["total_value"]))

        for day in sorted(by_day):
            eq = by_day[day]
            if len(eq) < 2:
                continue
            peak, intraday_dd = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                if peak > 0:
                    intraday_dd = max(intraday_dd, (peak - v) / peak * 100)
            # The running peak RESETS at the epoch boundary. A re-seeded
            # account starts a new series; letting the old one's peak carry
            # across is what would have read a 34% drawdown on day one of the
            # new account and tripped the kill switch.
            if epoch_day and day == epoch_day:
                running_peak = 0.0
            # All-time peak AS OF that day, not as of now: a backfill that used
            # today's peak would report drawdowns the account had not yet had
            # any way of experiencing.
            running_peak = max(running_peak, max(eq))
            running_dd = (((running_peak - eq[-1]) / running_peak * 100)
                          if running_peak > 0 else 0.0)
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO daily_stats
                        (date, paper_max_drawdown, paper_running_drawdown)
                        VALUES (?, ?, ?)
                        ON CONFLICT(date) DO UPDATE SET
                          paper_max_drawdown = GREATEST(
                              COALESCE(daily_stats.paper_max_drawdown, 0),
                              excluded.paper_max_drawdown),
                          paper_running_drawdown = excluded.paper_running_drawdown""",
                    (day, round(intraday_dd, 3), round(max(0.0, running_dd), 3)))
            written += 1
        return written

    def get_paper_equity_history(self, limit: int = 500):
        """Oldest-first (chart-ready) equity curve points."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM paper_equity_history ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in reversed(cur.fetchall())]

    def get_position(self, position_id):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            return dict(row) if row else None

    def update_position(self, position_id, updates: dict):
        if not updates:
            return
        with self._conn() as conn:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE positions SET {set_clause} WHERE id = ?",
                         (*updates.values(), position_id))

    def update_position_by_ticker(self, ticker: str, updates: dict,
                                   simulated: bool = None):
        """Book-scoped update of the open position in `ticker`.

        `simulated` is REQUIRED in practice (§16, E-9). Without it this
        statement matched an open position in EITHER book, so a paper entry's
        stop could land on a real SYNC holding of the same ticker. That is not
        hypothetical: HCA was in the live watchlist and simultaneously an
        $8,553 SYNC position, so a $100 paper entry in HCA would have
        overwritten the real row's current_stop_price with a stop computed for
        a hundred-dollar trade.

        The default is None and None RAISES, rather than defaulting to False.
        A silent default would leave any unmigrated call site writing to the
        REAL book, which is the more dangerous of the two directions - the
        failure would be invisible and would cost real money. Raising makes an
        unmigrated caller fail loudly on its first execution instead.

        Kept as a keyword rather than made positional so the migration could be
        done call site by call site; all three (paper_trader, live_trader,
        confirm_fill) now pass it explicitly.
        """
        if simulated is None:
            raise ValueError(
                "update_position_by_ticker requires simulated=True/False - an "
                "unscoped update matches an open position in EITHER book and "
                "can write a paper entry's stop onto a real holding (§16).")
        if not updates:
            return
        with self._conn() as conn:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"""UPDATE positions SET {set_clause}
                     WHERE ticker = ? AND status = 'open'
                       AND COALESCE(simulated, 0) = ?""",
                (*updates.values(), ticker, 1 if simulated else 0),
            )

    # ---------- MAE/MFE learning ----------
    def query_mae_winners(self, setup_type: str, regime: str) -> list:
        """MAE values (as %) for historically winning trades of this setup_type/regime,
        used to flag when a live position's drawdown looks anomalous vs. past winners.

        §15: filtered to data_quality='ok'. mae_mfe_data contained rows that
        are not trades - NVDA at +6.67% held for 12 milliseconds, MU at
        +10.00% held for 10ms, a ticker literally named "AAA", all three with
        MAE and MFE of exactly 0.0. This method feeds a percentile comparison
        against "historical winners", and a synthetic +10% winner with zero
        excursion makes every real position's drawdown look anomalous."""
        with self._conn() as conn:
            cur = conn.execute(
                """SELECT mae_pct FROM mae_mfe_data
                   WHERE setup_type = ? AND regime = ? AND outcome_pct > 0
                     AND COALESCE(data_quality, 'ok') = 'ok'""",
                (setup_type, regime),
            )
            return [row[0] for row in cur.fetchall() if row[0] is not None]

    def get_recent_mae_mfe(self, limit: int = 500, include_quarantined: bool = False) -> list:
        """Every recorded mae_mfe_data row (real closed trades only - see
        engine/mae_mfe_engine.py's record_completed(), called from
        confirm_fill.py's sell path) - used by analytics/trade_attribution.py
        to join a closed trade's MAE/MFE behavior against its
        pattern_database entry features for win/loss-reason classification.

        §15: quarantined rows are excluded by default. `include_quarantined`
        exists for forensics - the rows were MARKED rather than deleted
        precisely so the evidence of how the contamination happened survives,
        and an audit that cannot see them defeats the point of keeping them."""
        q = "SELECT * FROM mae_mfe_data"
        if not include_quarantined:
            q += " WHERE COALESCE(data_quality, 'ok') = 'ok'"
        q += " ORDER BY recorded_at DESC LIMIT ?"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(q, (limit,))
            return [dict(r) for r in cur.fetchall()]

    # Process-lifetime cache for the pre-012 schema probe. None = not yet
    # asked. Deliberately a class attribute: Database() is constructed freely
    # all over this codebase and the answer is a property of the DATABASE, not
    # of any one handle to it.
    _MAE_ID_LEGACY = None

    def _mae_id_is_legacy_text(self) -> bool:
        """True when mae_mfe_data.id is still the pre-012 TEXT primary key.

        Asked once per process and cached. On failure it returns True - the
        conservative direction, because supplying an id that the new schema
        does not need is harmless (012 makes the column GENERATED BY DEFAULT,
        not ALWAYS, so an explicit value is still legal), whereas omitting one
        the old schema DOES need is a NOT NULL violation on every write.
        """
        if Database._MAE_ID_LEGACY is not None:
            return Database._MAE_ID_LEGACY
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'mae_mfe_data' AND column_name = 'id'"
                ).fetchone()
            dtype = (row[0] if row is not None and not hasattr(row, "keys")
                     else (row["data_type"] if row is not None else None))
            legacy = str(dtype or "").lower() in ("text", "character varying")
        except Exception as e:
            logger.warning(
                f"insert_mae_mfe: could not determine mae_mfe_data.id type "
                f"({e}) - assuming the pre-012 TEXT column and supplying an "
                f"id. Harmless if 012 has been applied.")
            legacy = True

        if legacy:
            logger.warning(
                "mae_mfe_data.id is still TEXT - migrations/012 has NOT been "
                "applied to this database. Excursion rows are being written "
                "with a generated uuid so nothing is lost, but trade_id has no "
                "foreign key yet, which is the constraint that stops one "
                "excursion attaching itself to five different tickers (§C1). "
                "Run ./scripts/phase2_5_cutover.sh.")
        Database._MAE_ID_LEGACY = legacy
        return legacy

    def insert_mae_mfe(self, data: dict):
        """§C1 (migrations/012): `id` is no longer supplied by this method. It
        was a uuid4 string minted here for no reason beyond the table having
        been created with a TEXT primary key - nothing in the repository ever
        referenced it (not a foreign key, not a WHERE clause, not read by
        get_recent_mae_mfe's SELECT * or by get_pattern_excursions). It is now
        a BIGINT identity column the database fills in.

        `trade_id` is now INTEGER with a real FK to positions(id) ON DELETE SET
        NULL, so passing a trade_id that names no position raises here instead
        of being stored and discovered months later during an excursion join.
        int() rather than str() for the same reason - a caller handing us
        something non-numeric should fail at the call site that knows what it
        meant, not silently write a row that can never be joined.
        """
        trade_id = data.get("trade_id")
        if trade_id is not None and trade_id != "":
            try:
                trade_id = int(trade_id)
            except (TypeError, ValueError):
                raise ValueError(
                    f"insert_mae_mfe: trade_id {trade_id!r} is not a position id. "
                    f"mae_mfe_data.trade_id references positions(id) as of "
                    f"migrations/012 - a non-numeric value cannot be stored.")
        else:
            trade_id = None

        cols = ["trade_id", "ticker", "setup_type", "regime", "mae_pct", "mfe_pct",
                "outcome_pct", "hold_hours", "recorded_at", "data_quality"]
        vals = [trade_id, data.get("ticker"), data.get("setup_type"),
                data.get("regime"), data.get("mae_pct"), data.get("mfe_pct"),
                data.get("outcome_pct"), data.get("hold_hours"),
                datetime.utcnow().isoformat(),
                # §15: the writer classifies. Defaulting to 'ok' here would
                # make the column a promise nothing checks - the caller
                # (engine/mae_mfe_engine.record_completed) applies the same
                # rules migration 007 applied to the existing rows.
                data.get("data_quality") or "ok"]

        # ── Pre-012 compatibility ───────────────────────────────────────────
        # CREATE TABLE IF NOT EXISTS is a no-op on a database that already has
        # this table, so a deployment that ships this code WITHOUT having run
        # migrations/012 still has `id TEXT PRIMARY KEY` - NOT NULL, no
        # default. Omitting id there is not a schema mismatch that shows up in
        # a test; it is every MAE/MFE write raising at 3pm on a Tuesday.
        #
        # So: supply a uuid when the old column is still in place, and say so
        # loudly enough that it cannot be forgotten. This removes the deploy
        # ORDER constraint entirely - code and migration can land in either
        # sequence - which matters for a system whose scheduler restarts on a
        # timer rather than when someone is watching.
        #
        # Detected once per process, not per insert. The schema does not
        # change under a running process; if someone applies 012 while the
        # scheduler is up, the next restart picks it up, and until then the
        # explicit id is still accepted (012 makes it GENERATED BY DEFAULT, not
        # ALWAYS, precisely so an explicit value stays legal).
        if self._mae_id_is_legacy_text():
            import uuid
            cols.insert(0, "id")
            vals.insert(0, str(uuid.uuid4()))

        placeholders = ",".join("?" * len(cols))
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO mae_mfe_data ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                tuple(vals),
            )

    # ---------- re-entry cooldown ----------
    def ticker_in_cooldown(self, ticker: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cooldown_until FROM re_entry_cooldowns WHERE ticker = ?", (ticker,)
            ).fetchone()
            if not row or not row[0]:
                return False
            return datetime.utcnow().isoformat() < row[0]

    def set_re_entry_cooldown(self, ticker: str, hours: float, exit_reason: str = ""):
        from datetime import timedelta
        now = datetime.utcnow()
        cooldown_until = (now + timedelta(hours=hours)).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO re_entry_cooldowns (ticker, exit_time, cooldown_until, exit_reason)
                   VALUES (?,?,?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     exit_time = excluded.exit_time,
                     cooldown_until = excluded.cooldown_until,
                     exit_reason = excluded.exit_reason""",
                (ticker, now.isoformat(), cooldown_until, exit_reason),
            )

    # ---------- misc lookups used by confirm_fill.py / hard_vetoes.py ----------
    def get_recent_signal(self, ticker: str):
        """Singular convenience wrapper around latest_signal() - same data,
        name matches what confirm_fill.py's Phase 4 wiring expects."""
        return self.latest_signal(ticker)

    def get_latest_health_score(self):
        """Average position_health_score across open positions, or None if
        there are no open positions or none have been scored yet. NOTE: this
        is NOT the full 11-metric Strategy Health Score from the original
        spec (win rate trend, drift detection, etc.) - that system was never
        built. This is a much smaller thing: the average of
        engine/position_health.py's per-position score, which IS built
        (Phase 3). server.py's UI header badge reads this."""
        positions = self.get_all_positions()
        scores = [p["position_health_score"] for p in positions if p.get("position_health_score") is not None]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def get_portfolio_heat(self) -> dict:
        """Approximate portfolio heat = sum of open positions' dollar risk
        (entry - stop, if a stop is set) as a % of total dollars deployed.
        NOT normalized against real account equity - treat current_heat_pct as
        directional, not exact.

        2026-07-26 (documentation audit): the reason this docstring used to
        give for that - "no Robinhood account-balance data source is wired
        into Python (by design)" - stopped being true a while ago.
        engine/account_sync.py reads equity and buying power,
        robinhood_sync.py reads portfolio value, and
        live_trader._buying_power() checks it before every buy. The
        denominator could be real equity today. It is not, because heat feeds
        position sizing, so changing it changes trade sizes on a live system -
        that is a deliberate decision, not a missing data source. Kept
        approximate on purpose until someone chooses to change it."""
        positions = self.get_all_positions()
        total_deployed = sum(p.get("dollar_amount") or 0 for p in positions)
        total_risk = 0.0
        for p in positions:
            entry = p.get("entry_price") or 0
            stop = p.get("current_stop_price")
            shares = p.get("shares") or 0
            if entry and stop and shares:
                total_risk += max(0.0, (entry - stop)) * shares
        current_heat_pct = (total_risk / total_deployed * 100) if total_deployed else 0.0
        return {"current_heat_pct": round(current_heat_pct, 2), "max_heat_pct": 7.0}

    def get_closed_trade_for_ticker(self, ticker: str, simulated: bool = None):
        """Most recently closed position for this ticker, shaped for
        mae_mfe_engine.record_completed() - entry/exit + MAE/MFE fields.

        simulated param added 2026-07-17 (wiring paper-trade closes into
        MAE/MFE recording, same as confirm_fill.py's real-trade path already
        does): without it, a ticker with BOTH a closed real position and a
        closed paper position could return whichever has the higher row id,
        not necessarily the book the caller just closed. Defaults to None
        (either book) so confirm_fill.py's existing call site - which has
        only ever dealt with the real book - keeps its exact prior behavior;
        engine/paper_trader.py passes simulated=True explicitly."""
        q = "SELECT * FROM positions WHERE ticker = ? AND status = 'closed'"
        params = [ticker]
        if simulated is not None:
            q += " AND COALESCE(simulated, 0) = ?"
            params.append(1 if simulated else 0)
        q += " ORDER BY id DESC LIMIT 1"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(q, params).fetchone()
            return dict(row) if row else None

    # ---------- daily stats ----------
    def get_daily_stats(self) -> dict:
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM daily_stats WHERE date = ?", (today,))
            row = cur.fetchone()
            return dict(row) if row else {
                "date": today, "cycles_run": 0, "signals_generated": 0, "trades_placed": 0,
                "winning_trades": 0, "realized_pnl": 0.0, "max_drawdown": 0.0, "kill_switch_triggered": 0,
                "paper_trades_placed": 0, "paper_winning_trades": 0, "paper_realized_pnl": 0.0,
                "running_drawdown": 0.0,
                "paper_max_drawdown": 0.0, "paper_running_drawdown": 0.0,
            }

    # ---------- per-book daily counters (§7, §8, Phase 2) ----------
    def record_trade_placed(self, simulated: bool):
        """Increment today's trade counter for the relevant book.

        Deliberately NOT log_trade(). The `trades` table is the real-fill
        ledger, and writing simulated fills into it would destroy the one
        clean separation the schema still has. This separates the LEDGER from
        the COUNTER: paper trading gets a budget without pretending its fills
        are real.

        Before this (§7), `trades_placed` was incremented only by
        live_trader.py and confirm_fill.py, so RiskEngine read 0 forever on a
        paper-only deployment - 31 buys across seven days against a 10/day
        cap, reporting "0 trades placed" every single day.
        """
        col = "paper_trades_placed" if simulated else "trades_placed"
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                f"""INSERT INTO daily_stats (date, {col}) VALUES (?, 1)
                    ON CONFLICT(date) DO UPDATE SET {col} = daily_stats.{col} + 1""",
                (today,),
            )

    def trades_placed_today(self, simulated: bool) -> int:
        """Today's trade count for the requested book. Callers use this rather
        than reading a column name, so neither book can accidentally read the
        other's budget."""
        stats = self.get_daily_stats() or {}
        key = "paper_trades_placed" if simulated else "trades_placed"
        return int(stats.get(key, 0) or 0)

    def realized_pnl_today(self, simulated: bool = False) -> float:
        """Today's realised P&L for the requested book, in LOCAL calendar days.

        rules/risk_rules.py has called this method since it was written; it
        never existed, so `trip_kill_switch_if_needed` raised AttributeError on
        its first statement and the automatic kill switch could never fire
        (§8, §9).

        Local days, not UTC: an evening close at 00:30 UTC belongs to the
        trading day that just ended, not to tomorrow. The paper branch
        delegates to paper_realized_pnl_today() rather than reimplementing that
        conversion - two implementations of the same window is how you get two
        different answers from the same data.
        """
        stats = self.get_daily_stats() or {}
        if not simulated:
            return float(stats.get("realized_pnl", 0.0) or 0.0)

        # The LEDGER is authoritative for paper. paper_trades is itemised and
        # is what the Journal shows; daily_stats.paper_realized_pnl is an
        # accumulator written by close_position for O(1) reads and for the
        # §7 backfill.
        ledger = self.paper_realized_pnl_today()

        # Two records of the same quantity is how S-2 happened on the live
        # book: `trades` and `realized_pnl` disagreed about 2026-07-24 and
        # nothing noticed, because nothing ever compared them. Comparing them
        # costs one float subtraction, so there is no reason not to.
        accumulator = float(stats.get("paper_realized_pnl", 0.0) or 0.0)
        if abs(ledger - accumulator) > 0.01:
            logger.warning(
                f"paper realised P&L disagrees for today: ledger(paper_trades)="
                f"{ledger:.2f} vs accumulator(daily_stats)={accumulator:.2f}. "
                f"Using the ledger. A close that skipped log_paper_trade, or a "
                f"log_paper_trade with no matching close, will do this.")
        return ledger

    def set_kill_switch(self, on: bool, reason: str = None):
        """Mirror the kill-switch state into today's daily_stats row.

        config.yaml remains the authority - rules/risk_rules.py writes there,
        and every gate reads there. This is the audit trail: "was the breaker
        tripped on the 24th?" is a question about a day, and config.yaml only
        ever holds the answer for today.
        """
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO daily_stats (date, kill_switch_triggered) VALUES (?, ?)
                   ON CONFLICT(date) DO UPDATE SET kill_switch_triggered = excluded.kill_switch_triggered""",
                (today, 1 if on else 0),
            )
        if reason:
            try:
                self.log("CRITICAL" if on else "WARNING", reason)
            except Exception:
                pass

    def paper_realized_pnl_today(self) -> float:
        """Sum of today's PAPER sell P/L, for the dashboard's Realized P&L
        tile (2026-07-16, Akhil's 'Realized P&L doesn't show values' report:
        daily_stats.realized_pnl is real-money-only by design - it feeds the
        risk engine's max_daily_loss guard - so a paper-only account showed
        $0.00 forever). Kept OUT of daily_stats on purpose; this is a
        display-only aggregate. created_at is stored as naive UTC isoformat,
        so local-time conversion happens before comparing calendar days - an
        evening close (00:xx UTC = same trading day locally) must not slip
        into tomorrow.

        2026-07-21 (Postgres migration): SQLite's date(created_at,'localtime')
        relied on SQLite reading the OS's local timezone directly - Postgres
        has no equivalent shorthand, and leaning on the Postgres SERVER's
        configured timezone would silently break this if that's ever set to
        anything other than the Mac's local zone. Computing the local-day
        window in Python instead (using the same naive local/UTC offset the
        app already assumes everywhere else) sidesteps the DB engine's
        timezone config entirely - just a plain string-range comparison
        against the already-UTC-isoformat created_at column, same as before."""
        # 2026-07-25 (§11): the window calculation moved to
        # _local_day_window_utc() so update_drawdown() shares it verbatim.
        # Behaviour is unchanged; see that helper's docstring for the reasoning
        # this one used to carry.
        start_utc, end_utc = _local_day_window_utc()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) FROM paper_trades "
                "WHERE side = 'sell' AND created_at >= ? AND created_at < ?",
                (start_utc, end_utc),
            ).fetchone()
            return round(row[0] or 0.0, 2)

    def get_realized_pnl_all_time(self) -> float:
        """All-time REAL realized P/L, for the Real Portfolio tab's summary
        card (2026-07-24, Paper/Real toggle). daily_stats.realized_pnl is
        real-money-only by design (see close_position()'s docstring above) and
        one row per calendar day, so summing across every row is the
        real-book equivalent of paper_account.realized_pnl's running total -
        there's no single cumulative column for the real book since real
        closes are recorded per-day, not per-account."""
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0.0) FROM daily_stats").fetchone()
            return round(row[0] or 0.0, 2)

    # ---------- trade snapshots (immutable - never UPDATE after INSERT) ----------
    def save_trade_snapshot(self, snapshot_id: str, signal_id, data_json: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO trade_snapshots (snapshot_id, signal_id, created_at, data) VALUES (?,?,?,?)",
                (snapshot_id, signal_id, datetime.utcnow().isoformat(), data_json),
            )

    def get_trade_snapshot(self, snapshot_id: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM trade_snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
            return dict(row) if row else None

    # ---------- pattern database ----------
    def add_pattern(self, ticker: str, mode: str, features: dict, trade_id: str = None,
                     config_fingerprint: str = None) -> int:
        """§17 (Phase 1): every pattern row is stamped with the build that
        produced it (app_version/engine_version from storage/version.py) and
        the fingerprint of the config values that can change a score or an
        exit. Provenance is written at INSERT and never updated - a row's
        origin is not a thing that changes."""
        import json
        from storage.version import app_version
        version = app_version()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO pattern_database
                   (trade_id, ticker, mode, recorded_at, features, is_closed,
                    app_version, engine_version, config_fingerprint)
                VALUES (?,?,?,?,?,0,?,?,?)
                RETURNING id""",
                (trade_id, ticker, mode, datetime.utcnow().isoformat(),
                 json.dumps(features), version, version,
                 config_fingerprint or "unstamped"),
            )
            return cur.lastrowid

    def link_pattern_to_trade(self, pattern_id: int, position_id) -> bool:
        """§51 (Phase 2.5): stamp the position a pattern actually became.

        pattern_database.trade_id has existed since the table was created and
        was NULL on every row, because the only writer - add_pattern(), via
        PatternDatabase.record_entry() - runs at SIGNAL time, and at signal time
        no position exists. There is exactly one moment when both ids are in
        scope: immediately after try_open_position()/open_position() returns.
        This is the call for that moment.

        Why it matters. engine/mae_mfe_engine.record_completed() writes
        mae_mfe_data.trade_id = the POSITION id, so the only route from a
        pattern to its true intraday excursion was the transitive one through
        positions.pattern_id - and that route is unsafe today, see
        get_pattern_excursions(). A direct, indexed link makes the join one hop
        and lets the integrity constraint live on the column being joined.

        Idempotent and non-fatal. A missing link costs one row of excursion
        analysis; raising here would cost a recorded fill. Returns True when a
        row was updated.
        """
        if pattern_id is None or position_id is None:
            return False
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE pattern_database SET trade_id = ? WHERE id = ?",
                    (str(position_id), pattern_id))
            return True
        except Exception as e:
            logger.warning(
                f"link_pattern_to_trade(pattern={pattern_id}, "
                f"position={position_id}) failed: {e}. The position and the "
                f"pattern are both recorded; only the join between them is "
                f"missing, so this trade will be absent from excursion "
                f"analysis rather than wrong in it.")
            return False

    def get_exit_kind_coverage(self, mode: str = None, since: str = None) -> dict:
        """How many closed patterns carry a countable `exit_kind`, and how many
        exist at all. Every consumer of exit_kind must show this beside its
        results (§50).

        WHY THIS IS MANDATORY RATHER THAN NICE. `exit_kind` is NULL wherever
        the exit could not be classified - see rules/common.py's classify_exit,
        which returns None rather than guessing, on the correct principle that
        a wrong kind is worse than a missing one. That means every GROUP BY
        exit_kind silently analyses a SUBSET, and the result looks exactly like
        a complete one. "trailing_stop: 60%" reads as a fact about the strategy
        when it may be a fact about 12 of 68 trades.

        A NOTE ON WHAT THE GAP ACTUALLY IS, because it is easy to get backwards.
        The gap is HISTORICAL, not a pending producer. Every live producer now
        emits a structured kind natively: rules/sell_rules.py carries
        `exit_kind` on its SellResult and every triggered hard check supplies
        one (§D); Loop B goes through exit_kind_for_loop_b_label; the price
        watch, rotation, time stops and manual confirms all pass fixed literals.
        So coverage should approach 100% for anything closed after §D, and the
        shortfall is rows closed before it. That distinction decides what to do
        about a low number: wait for it to age out, not go looking for an
        unwired producer.

        `unclassified_reasons` is included because it turns the number into an
        action. If the missing rows are dominated by one `exit_reason` prefix,
        that prefix is either a producer worth teaching or a token worth adding
        to classify_exit - and if they are scattered prose, they are history and
        nothing can be done but let them age out.
        """
        from rules.common import format_exit_kind_coverage

        where = ["is_closed = 1"]
        params = []
        if mode:
            where.append("mode = ?")
            params.append(mode)
        if since:
            where.append("recorded_at >= ?")
            params.append(since)
        clause = " AND ".join(where)

        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM pattern_database WHERE {clause}",
                tuple(params)).fetchone()[0]
            structured = conn.execute(
                f"SELECT COUNT(*) FROM pattern_database "
                f"WHERE {clause} AND exit_kind IS NOT NULL",
                tuple(params)).fetchone()[0]
            reasons = conn.execute(
                f"SELECT COALESCE(exit_reason, '(none)'), COUNT(*) "
                f"FROM pattern_database WHERE {clause} AND exit_kind IS NULL "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
                tuple(params)).fetchall()

        return {
            "structured": int(structured),
            "total": int(total),
            "missing": int(total) - int(structured),
            # None, not 0.0, when there is nothing to divide. A 0% coverage bar
            # against an empty book is a false alarm, and the difference
            # between "no structured exits" and "no exits" matters to anyone
            # reading this to decide whether a number is trustworthy yet.
            "pct": (round(structured / total * 100, 1) if total else None),
            "unclassified_reasons": [
                {"exit_reason": r[0], "n": int(r[1])} for r in reasons
            ],
            "label": format_exit_kind_coverage(structured, total),
        }

    def get_pattern_excursions(self, mode: str = None, since: str = None) -> list:
        """§51: the ONE sanctioned join from closed patterns to their MAE/MFE
        rows. Everything wanting true intraday excursions goes through here.

        THIS EXISTS BECAUSE THE OBVIOUS QUERY IS WRONG. mae_mfe_data.trade_id is
        TEXT, holds a stringified positions.id, and until migrations/010 had no
        unique constraint, no foreign key and no book scope. On the 2026-07-25
        snapshot, trade_id = '1' appeared fifteen times across five different
        tickers - test-suite residue that scripts/repair_test_damage.py never
        covered (§49). Joining pattern_database -> positions -> mae_mfe_data on
        it returned 37 rows for 23 closed patterns, and the surplus was not
        duplicate records of one trade: it was NVDA's excursion row attaching
        itself to ADPT's pattern. An AVG(mae_pct) over that join is wrong in a
        way nothing about the query looks wrong.

        Three defences, because the data cannot be trusted to be clean forever:

          1. Join on pattern_database.trade_id directly (one hop, indexed by
             migrations/010) rather than transitively through positions.
          2. Require ticker agreement. A row that claims a trade belonging to a
             different symbol is not a near-miss to be repaired, it is a
             collision, and it is dropped.
          3. Return at most one excursion per pattern. migrations/010 adds the
             unique index that should make this impossible to need; it is kept
             because a constraint added later is only as good as the last time
             someone re-applied it to a restored database.

        On top of those, §15's quarantine applies to BOTH sides. Migration 007
        marked contaminated mae_mfe_data rows rather than deleting them, so the
        evidence survived; a reader that ignores the mark gets the evidence
        back in its averages. get_recent_mae_mfe() already filters this way and
        this method must agree with it, or the same table reports two different
        populations depending on which accessor you happened to call.

        Rows with NULL trade_id (every pattern recorded before §51) are simply
        absent, which is the honest answer - not zero excursion.
        """
        sql = """
            SELECT p.id            AS pattern_id,
                   p.ticker        AS ticker,
                   p.mode          AS mode,
                   p.outcome_pct   AS outcome_pct,
                   p.hold_hours    AS hold_hours,
                   p.exit_kind     AS exit_kind,
                   p.recorded_at   AS recorded_at,
                   m.mae_pct       AS mae_pct,
                   m.mfe_pct       AS mfe_pct
              FROM pattern_database p
              JOIN mae_mfe_data m
                -- §C1: migrations/012 made mae_mfe_data.trade_id a real
                -- INTEGER FK to positions(id). pattern_database.trade_id is
                -- still TEXT, so the CAST stays - one side is now typed and
                -- constrained, the other is not yet. The cast is correct
                -- either way; what changed is that the mae side can no longer
                -- hold a value naming no position at all, which was the
                -- failure this join was written to survive.
                ON CAST(m.trade_id AS TEXT) = CAST(p.trade_id AS TEXT)
               AND UPPER(m.ticker) = UPPER(p.ticker)
             WHERE p.is_closed = 1
               AND p.trade_id IS NOT NULL
               AND COALESCE(m.data_quality, 'ok') = 'ok'
               AND COALESCE(p.data_quality, 'ok') = 'ok'
        """
        params = []
        if mode:
            sql += " AND p.mode = ?"
            params.append(mode)
        if since:
            sql += " AND p.recorded_at >= ?"
            params.append(since)
        sql += " ORDER BY p.recorded_at DESC"

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

        seen, out = set(), []
        for r in rows:
            if r["pattern_id"] in seen:
                logger.warning(
                    f"get_pattern_excursions: pattern #{r['pattern_id']} "
                    f"({r['ticker']}) matched more than one mae_mfe_data row. "
                    f"Keeping the first and dropping the rest - the unique "
                    f"index from migrations/010 is missing or was lost in a "
                    f"restore. Re-apply it before trusting excursion stats.")
                continue
            seen.add(r["pattern_id"])
            out.append(r)
        return out

    def get_pattern_by_id(self, pattern_id: int):
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM pattern_database WHERE id = ?", (pattern_id,)).fetchone()
            if not row:
                return None
            row = dict(row)
            row["features"] = json.loads(row["features"])
            return row

    def close_pattern(self, pattern_id: int, outcome_pct: float, hold_hours: float,
                       exit_reason: str, exit_kind: str = None):
        """§50 (Phase 2.5): writes the countable `exit_kind` alongside the
        human-readable `exit_reason`.

        `exit_kind` defaults to None and is then derived by
        rules/common.classify_exit(), which returns None for anything it cannot
        determine from a structured token. Callers that hold the structured
        value directly should pass it rather than relying on the derivation -
        the derivation exists so that the namespaced reasons already generated
        from fixed vocabularies (price_watch:, rotation:, time_based_close,
        manual_fill_confirmed) do not each need a second argument threaded
        through their call chain to say what their own string already says.

        An unrecognised exit_kind is rejected rather than stored. The column is
        only worth having if its domain is closed; one typo'd value that never
        appears again reintroduces exactly the ungroupable-column problem this
        was written to fix.
        """
        from rules.common import EXIT_KINDS, classify_exit
        kind = exit_kind if exit_kind is not None else classify_exit(exit_reason)
        if kind is not None and kind not in EXIT_KINDS:
            logger.error(
                f"close_pattern({pattern_id}): exit_kind {kind!r} is not in "
                f"EXIT_KINDS - storing NULL. The exit_reason is unaffected and "
                f"is still {exit_reason!r}; only the countable column is "
                f"dropped, because a value outside the closed set makes the "
                f"column uncountable again.")
            kind = None
        with self._conn() as conn:
            conn.execute(
                """UPDATE pattern_database
                      SET outcome_pct = ?, hold_hours = ?, exit_reason = ?,
                          exit_kind = ?, is_closed = 1
                WHERE id = ?""",
                (outcome_pct, hold_hours, exit_reason, kind, pattern_id),
            )

    def get_patterns(self, mode: str = None, ticker: str = None, closed_only: bool = True,
                      since: str = None, include_quarantined: bool = False) -> list:
        """`since`: ISO timestamp; only patterns with recorded_at >= it.

        The `since` DEFAULT IS STILL UNCHANGED (§17, Phase 1): engine/ev_engine.py
        reaches this method through PatternDatabase.find_similar_trades on the
        live decision path, and Phase 1 shipped decision_function_changed:
        false, so learning callers opt into the cutoff via get_closed_patterns().

        §15 (Phase 2) makes the data_quality filter universal instead, and
        that IS a decision-function change - declared as such in the release
        note, not slipped in. The distinction from §17's cutoff is the reason:
        a `pre_stop_fix` row is a REAL trade taken under a system that no
        longer exists, so excluding it from live EV is a judgement call worth
        deferring. A row marked `synthetic` is not a trade at all - a 12ms
        hold, or a non-zero outcome with arithmetically impossible zero
        excursion - and there is no reading of "evidence" under which it
        should inform a live decision. The sweep marks both, so both are
        filtered; the honest consequence is that ev_engine will report
        insufficient history until real post-fix trades accumulate, which is
        the true state of the evidence rather than a regression.

        `include_quarantined` is for forensics and for the UI's own audit
        views. The rows were marked rather than deleted so that the evidence
        of how the contamination happened survives; a reader that cannot see
        them defeats the point of keeping them.
        """
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM pattern_database WHERE 1=1"
            params = []
            if not include_quarantined:
                query += " AND COALESCE(data_quality, 'ok') = 'ok'"
            if mode:
                query += " AND mode = ?"
                params.append(mode)
            if ticker:
                query += " AND ticker = ?"
                params.append(ticker)
            if closed_only:
                query += " AND is_closed = 1"
            if since:
                query += " AND recorded_at >= ?"
                params.append(since)
            cur = conn.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["features"] = json.loads(r["features"])
            return rows

    def get_closed_patterns(self, cfg: dict = None, mode: str = None,
                             since: str = None) -> list:
        """Closed patterns that are ALLOWED TO TRAIN something (§17).

        Applies learning.min_pattern_recorded_at, so every learning caller
        gets the same clean sample without each one remembering to pass the
        cutoff. Everything recorded before it was produced under the stop bug
        removed 2026-07-20; a large sample of contaminated trades is worse
        than a small one, because it looks trustworthy.

        Pass `cfg` (preferred) or an explicit `since`. With neither, this is
        just get_patterns(closed_only=True) - and that is a bug in the caller,
        so it logs a warning rather than silently widening the sample.

        §15 (Phase 2) added the data_quality='ok' filter - in get_patterns()
        rather than here, so that it applies to every reader including the
        live path, not only to callers that remembered to come through this
        door. See get_patterns() for why that filter is universal while the
        §17 cutoff is not.
        """
        if since is None and cfg is not None:
            since = ((cfg.get("learning", {}) or {}).get("min_pattern_recorded_at"))
        if since is None:
            logger.warning("get_closed_patterns called with no cutoff - the §17 "
                            "contamination filter is NOT being applied")
        return self.get_patterns(mode=mode, closed_only=True, since=since)

    # ---------- Bayesian weight updates ----------
    def log_bayesian_update(self, rule_name: str, bucket: str, old_weight: float, new_weight: float,
                             occurrences: int, win_rate_when_fired: float, overall_win_rate: float,
                             applied: bool = True, block_reason: str = None):
        change_pct = ((new_weight - old_weight) / old_weight * 100) if old_weight else 0.0
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO bayesian_weight_history
                (timestamp, rule_name, bucket, old_weight, new_weight, change_pct, occurrences,
                 win_rate_when_fired, overall_win_rate, applied, block_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), rule_name, bucket, old_weight, new_weight, change_pct,
                 occurrences, win_rate_when_fired, overall_win_rate, int(applied), block_reason),
            )

    def get_weekly_bayesian_change(self, week_start: str) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT total_weight_change_pct FROM bayesian_weekly_tracker WHERE week_start = ?", (week_start,)
            ).fetchone()
            return row[0] if row else 0.0

    def add_weekly_bayesian_change(self, week_start: str, pct: float):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO bayesian_weekly_tracker (week_start, total_weight_change_pct) VALUES (?, ?)
                   ON CONFLICT(week_start) DO UPDATE SET
                     total_weight_change_pct = bayesian_weekly_tracker.total_weight_change_pct + excluded.total_weight_change_pct""",
                (week_start, pct),
            )

    def get_monthly_bayesian_change(self, month_start: str) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT total_weight_change_pct FROM bayesian_monthly_tracker WHERE month_start = ?", (month_start,)
            ).fetchone()
            return row[0] if row else 0.0

    def add_monthly_bayesian_change(self, month_start: str, pct: float):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO bayesian_monthly_tracker (month_start, total_weight_change_pct) VALUES (?, ?)
                   ON CONFLICT(month_start) DO UPDATE SET
                     total_weight_change_pct = bayesian_monthly_tracker.total_weight_change_pct + excluded.total_weight_change_pct""",
                (month_start, pct),
            )

    def get_bayesian_history(self, rule_name: str = None, limit: int = 100) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if rule_name:
                cur = conn.execute(
                    "SELECT * FROM bayesian_weight_history WHERE rule_name = ? ORDER BY id DESC LIMIT ?",
                    (rule_name, limit),
                )
            else:
                cur = conn.execute("SELECT * FROM bayesian_weight_history ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- champion/challenger ----------
    def create_challenge(self, challenge_id: str, config_json: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO champion_challenger
                (id, challenger_start, challenger_config, status, updated_at)
                VALUES (?,?,?, 'running', ?)""",
                (challenge_id, datetime.utcnow().isoformat(), config_json, datetime.utcnow().isoformat()),
            )

    def record_challenge_trade(self, challenge_id: str, is_challenger: bool, won: bool, pnl_pct: float):
        field_trades = "challenger_trades" if is_challenger else "champion_trades"
        field_wins = "challenger_wins" if is_challenger else "champion_wins"
        field_pnl = "challenger_pnl_pct" if is_challenger else "champion_pnl_pct"
        with self._conn() as conn:
            conn.execute(
                f"""UPDATE champion_challenger SET
                    {field_trades} = {field_trades} + 1,
                    {field_wins} = {field_wins} + ?,
                    {field_pnl} = {field_pnl} + ?,
                    updated_at = ?
                WHERE id = ?""",
                (int(won), pnl_pct, datetime.utcnow().isoformat(), challenge_id),
            )

    def get_challenge(self, challenge_id: str):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM champion_challenger WHERE id = ?", (challenge_id,)).fetchone()
            return dict(row) if row else None

    def update_challenge_status(self, challenge_id: str, status: str, significance: float = None):
        with self._conn() as conn:
            conn.execute(
                "UPDATE champion_challenger SET status = ?, statistical_significance = ?, updated_at = ? WHERE id = ?",
                (status, significance, datetime.utcnow().isoformat(), challenge_id),
            )

    def get_active_challenges(self) -> list:
        """Challenges still in 'running' status (create_challenge()'s default) -
        used by the automated learning loop to know which challenges to
        re-evaluate each trigger, without the caller needing to track IDs
        themselves."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM champion_challenger WHERE status = 'running'")
            return [dict(r) for r in cur.fetchall()]

    def get_all_challenges(self, limit: int = 50) -> list:
        """Every champion/challenger row regardless of status (running,
        promoted, discarded) - used by the Strategy tab's evolution history
        to show past promotions/discards, not just what's currently active."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM champion_challenger ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- learning-loop automation ----------
    def log_learning_run(self, trigger_reason: str, mode: str, n_patterns: int,
                          proposals: dict, challenges_evaluated: list):
        import json
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO learning_runs (run_at, trigger_reason, mode, n_patterns, proposals, challenges_evaluated)
                VALUES (?,?,?,?,?,?)""",
                (datetime.utcnow().isoformat(), trigger_reason, mode, n_patterns,
                 json.dumps(proposals, default=str), json.dumps(challenges_evaluated, default=str)),
            )

    def get_last_learning_run(self, mode: str = None):
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if mode:
                row = conn.execute(
                    "SELECT * FROM learning_runs WHERE mode = ? ORDER BY id DESC LIMIT 1", (mode,)
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM learning_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            d = dict(row)
            d["proposals"] = json.loads(d["proposals"]) if d.get("proposals") else {}
            d["challenges_evaluated"] = json.loads(d["challenges_evaluated"]) if d.get("challenges_evaluated") else []
            return d

    def get_recent_learning_runs(self, limit: int = 20) -> list:
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM learning_runs ORDER BY id DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for d in rows:
                d["proposals"] = json.loads(d["proposals"]) if d.get("proposals") else {}
                d["challenges_evaluated"] = json.loads(d["challenges_evaluated"]) if d.get("challenges_evaluated") else []
            return rows

    # ---------- historical replay / backtest runs ----------
    def log_backtest_run_start(self, tickers: list, start_date: str, end_date: str,
                                triggered_by: str = "manual") -> int:
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """INSERT INTO backtest_runs (started_at, status, triggered_by, tickers, start_date, end_date)
                VALUES (?,?,?,?,?,?) RETURNING id""",
                (datetime.utcnow().isoformat(), "running", triggered_by,
                 json.dumps(tickers), start_date, end_date),
            )
            row = cur.fetchone()
            return row["id"] if row else None

    def log_backtest_run_complete(self, run_id: int, n_scored: int, veto_counts: dict,
                                   summary: dict, trades: list, config: dict, output_dir: str = None):
        import json
        with self._conn() as conn:
            conn.execute(
                """UPDATE backtest_runs SET completed_at=?, status='completed', n_scored=?,
                veto_counts=?, summary=?, trades=?, config=?, output_dir=? WHERE id=?""",
                (datetime.utcnow().isoformat(), n_scored, json.dumps(veto_counts, default=str),
                 json.dumps(summary, default=str), json.dumps(trades, default=str),
                 json.dumps(config, default=str), output_dir, run_id),
            )

    def log_backtest_run_failed(self, run_id: int, error: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE backtest_runs SET completed_at=?, status='failed', error=? WHERE id=?",
                (datetime.utcnow().isoformat(), error, run_id),
            )

    def _hydrate_backtest_row(self, d: dict) -> dict:
        import json
        for key in ("tickers", "veto_counts", "summary", "trades", "config"):
            d[key] = json.loads(d[key]) if d.get(key) else ([] if key in ("tickers", "trades") else {})
        return d

    def get_last_backtest_run(self, status: str = None):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if status:
                row = conn.execute(
                    "SELECT * FROM backtest_runs WHERE status = ? ORDER BY id DESC LIMIT 1", (status,)
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 1").fetchone()
            return self._hydrate_backtest_row(dict(row)) if row else None

    def get_running_backtest_run(self):
        return self.get_last_backtest_run(status="running")

    def get_recent_backtest_runs(self, limit: int = 20) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,))
            return [self._hydrate_backtest_row(dict(r)) for r in cur.fetchall()]

    # ---------- override analytics ----------
    def record_override(self, override_id: str, signal_id, override_type: str,
                         system_recommendation: str, user_action: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO override_analytics
                (id, signal_id, override_type, system_recommendation, user_action, recorded_at)
                VALUES (?,?,?,?,?,?)""",
                (override_id, signal_id, override_type, system_recommendation, user_action,
                 datetime.utcnow().isoformat()),
            )

    def close_override_outcome(self, override_id: str, outcome_pct: float, system_would_have_pct: float):
        improved = outcome_pct > system_would_have_pct
        with self._conn() as conn:
            conn.execute(
                """UPDATE override_analytics SET outcome_pct = ?, system_would_have_pct = ?, override_improved = ?
                WHERE id = ?""",
                (outcome_pct, system_would_have_pct, int(improved), override_id),
            )

    def get_overrides(self, limit: int = 100) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM override_analytics ORDER BY recorded_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- rejected signals / opportunity cost ----------
    def log_rejected_signal(self, ticker: str, reject_stage: str, reject_reason: str,
                             score_at_rejection: float, price_at_rejection: float,
                             would_have_size: float = None) -> int:
        """One row per declined candidate (§18).

        portfolio_risk_log held 244 evaluations and this table held 0, so
        there was a complete record of every trade taken and none at all of
        any trade declined - which makes false negatives unmeasurable. You
        can audit the trades you took but not the ones you skipped.

        `would_have_size` is what makes a row a genuine counterfactual rather
        than a note: without the dollar amount the trade would have taken, a
        later "what did skipping this cost?" question has no denominator.
        """
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO rejected_signals
                (timestamp, ticker, reject_stage, reject_reason, score_at_rejection,
                 price_at_rejection, would_have_size)
                VALUES (?,?,?,?,?,?,?)
                RETURNING id""",
                (datetime.utcnow().isoformat(), ticker, reject_stage, reject_reason,
                 score_at_rejection, price_at_rejection, would_have_size),
            )
            return cur.lastrowid

    def record_simulated_outcome(self, rejected_id: int, simulated_outcome_pct: float):
        with self._conn() as conn:
            conn.execute(
                "UPDATE rejected_signals SET simulated_outcome_pct = ?, simulated_at = ? WHERE id = ?",
                (simulated_outcome_pct, datetime.utcnow().isoformat(), rejected_id),
            )

    def get_rejected_signals(self, unsimulated_only: bool = False, limit: int = 200) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM rejected_signals"
            if unsimulated_only:
                query += " WHERE simulated_at IS NULL"
            query += " ORDER BY id DESC LIMIT ?"
            cur = conn.execute(query, (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- missed opportunity report ----------
    def get_hold_signals(self, limit: int = 500) -> list:
        """Every HOLD signal that was actually SCORED (bucket_scores +
        threshold_breakdown present, i.e. it cleared hard-vetoes and reached
        rules/swing_buy_rules.py's score() but didn't cross the buy
        threshold) - the raw material for analytics/missed_opportunity.py.
        Hard-vetoed tickers never reach score() so they have no bucket data
        and are correctly excluded here (a "missed opportunity" report about
        buckets/threshold doesn't apply to something that never got scored)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """SELECT * FROM signals WHERE signal = 'HOLD' AND bucket_scores IS NOT NULL
                   AND threshold_breakdown IS NOT NULL ORDER BY id DESC LIMIT ?""",
                (limit,),
            )
            return [self._parse_signal_json(dict(r)) for r in cur.fetchall()]

    def save_missed_opportunity_outcome(self, signal_id: int, ticker: str, hold_days: int,
                                         entry_price: float, would_have_returned_pct: float = None,
                                         peak_return_pct: float = None, peak_at_days: int = None,
                                         trough_return_pct: float = None, trough_at_days: int = None,
                                         still_pending: bool = False):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO missed_opportunity_outcomes
                (signal_id, ticker, evaluated_at, hold_days, entry_price, would_have_returned_pct,
                 peak_return_pct, peak_at_days, trough_return_pct, trough_at_days, still_pending)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    evaluated_at=excluded.evaluated_at, hold_days=excluded.hold_days,
                    would_have_returned_pct=excluded.would_have_returned_pct,
                    peak_return_pct=excluded.peak_return_pct, peak_at_days=excluded.peak_at_days,
                    trough_return_pct=excluded.trough_return_pct, trough_at_days=excluded.trough_at_days,
                    still_pending=excluded.still_pending""",
                (signal_id, ticker, datetime.utcnow().isoformat(), hold_days, entry_price,
                 would_have_returned_pct, peak_return_pct, peak_at_days, trough_return_pct, trough_at_days,
                 int(bool(still_pending))),
            )

    def get_missed_opportunity_outcome(self, signal_id: int):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM missed_opportunity_outcomes WHERE signal_id = ?", (signal_id,)
            ).fetchone()
            return dict(row) if row else None

    # ---------- regret analysis ----------
    def save_regret_analysis(self, pattern_id: int, ticker: str, entry_price: float, exit_price: float,
                              exit_reason: str, forward_window_days: int, highest_afterwards: float,
                              lowest_afterwards: float, regret_pts: float, regret_pct: float,
                              downside_avoided_pts: float, downside_avoided_pct: float,
                              classification: str, still_maturing: bool = False):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO regret_analysis
                (pattern_id, ticker, computed_at, entry_price, exit_price, exit_reason, forward_window_days,
                 highest_afterwards, lowest_afterwards, regret_pts, regret_pct, downside_avoided_pts,
                 downside_avoided_pct, classification, still_maturing)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pattern_id) DO UPDATE SET
                    computed_at=excluded.computed_at, highest_afterwards=excluded.highest_afterwards,
                    lowest_afterwards=excluded.lowest_afterwards, regret_pts=excluded.regret_pts,
                    regret_pct=excluded.regret_pct, downside_avoided_pts=excluded.downside_avoided_pts,
                    downside_avoided_pct=excluded.downside_avoided_pct, classification=excluded.classification,
                    still_maturing=excluded.still_maturing""",
                (pattern_id, ticker, datetime.utcnow().isoformat(), entry_price, exit_price, exit_reason,
                 forward_window_days, highest_afterwards, lowest_afterwards, regret_pts, regret_pct,
                 downside_avoided_pts, downside_avoided_pct, classification, int(bool(still_maturing))),
            )

    def get_regret_analysis(self, pattern_id: int):
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM regret_analysis WHERE pattern_id = ?", (pattern_id,)).fetchone()
            return dict(row) if row else None

    def get_regret_analyses(self, limit: int = 200) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM regret_analysis ORDER BY pattern_id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------- threshold regret analysis (2026-07-23) ----------
    def log_threshold_regret_run(self, trigger_reason: str, report: dict) -> int:
        """Persists one evaluate_threshold_regret() snapshot. Mirrors
        log_learning_run()'s exact shape (run_at + trigger_reason + a JSON
        payload column) - same "history of runs over time", not just the
        latest one."""
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """INSERT INTO threshold_regret_runs
                (run_at, trigger_reason, n_signals, n_evaluated, n_still_pending, report)
                VALUES (?,?,?,?,?,?) RETURNING id""",
                (datetime.utcnow().isoformat(), trigger_reason, report.get("n_signals"),
                 report.get("n_evaluated"), report.get("n_still_pending"), json.dumps(report, default=str)),
            ).fetchone()
            return row["id"] if row else None

    def get_last_threshold_regret_run(self):
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM threshold_regret_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["report"] = json.loads(d["report"]) if d.get("report") else {}
            return d

    def get_recent_threshold_regret_runs(self, limit: int = 20) -> list:
        import json
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM threshold_regret_runs ORDER BY id DESC LIMIT ?", (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for d in rows:
                d["report"] = json.loads(d["report"]) if d.get("report") else {}
            return rows

    # ---------- monitoring alerts ----------
    def log_alert(self, alert_id: str, alert_type: str, severity: str, message: str):
        """ON CONFLICT DO NOTHING - alert_id is caller-generated and often
        deterministic per (ticker, day) (see scheduler.py's stale-data
        health alert), so a second call for the same id on the same day is
        expected (the SAME condition is still true next cycle) and should be
        a silent no-op, not an IntegrityError."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO monitoring_alerts (id, alert_type, severity, message, triggered_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING""",
                (alert_id, alert_type, severity, message, datetime.utcnow().isoformat()),
            )

    def resolve_alert(self, alert_id: str, resolution: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE monitoring_alerts SET resolved_at = ?, resolution = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), resolution, alert_id),
            )

    def get_open_alerts(self) -> list:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM monitoring_alerts WHERE resolved_at IS NULL ORDER BY triggered_at DESC")
            return [dict(r) for r in cur.fetchall()]
