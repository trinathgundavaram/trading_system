# Trading Platform — Production Readiness Plan

Compiled 2026-07-21 from a live debugging session (DB stalls, scheduler not
firing, FMP 402s, dead FRED tool). Part one (below, marked **DONE**) was
implemented directly. Part two is higher-risk and needs your review before
anything touches the code.

## Part 1 — Fixed already

| Issue | Root cause | Fix | File |
|---|---|---|---|
| FMP `grades`/`analyst-estimates` spamming warnings, tripping the shared FMP breaker | HTTP 402 "not available under your current subscription" on most symbols — a **permanent** plan-tier limit, not a transient blip. All FMP endpoints shared one circuit breaker, so this permanently-broken pair was also taking down the still-healthy `movers`/`stock-list`/`earnings` calls. | Split into its own `ratings_breaker`. Any HTTP 402 now force-opens that breaker for 24h instead of the normal 15-min retry loop, so it stops re-probing a wall that won't move and stops punishing unrelated endpoints. | `mcp_clients/market_data.py`, `mcp_clients/base.py` |
| FRED macro data — `get_series_observations` retried 8x every single cycle, all failing with "Tool not found" | The local `fred-mcp-server` build's actual tool set doesn't match what this module assumed (server drifted). Same failure class already fixed for `stock_scanner.py` on 2026-07-16. | Applied the same fix: discover the real tool set once via `list_tools()` (cached 24h), skip calls to tools that don't exist, one info log line instead of 8 warnings/cycle. | `mcp_clients/fred_mcp.py` |
| Alpaca 400 "invalid symbol" on class-share tickers (e.g. `PBR-A`), which also tripped the shared Alpaca breaker for every other ticker in that cycle | This app's canonical ticker format uses hyphens (`PBR-A`, `BRK-B`); Alpaca's REST API expects dot notation (`PBR.A`, `BRK.B`). | Added `_to_alpaca_symbol()` translation at the Alpaca call sites only — canonical hyphen form is untouched everywhere else in the app. | `mcp_clients/market_data.py` |

All three changes are localized (new methods / edited except-blocks), don't
change any function signatures callers depend on, and degrade to the exact
same fallback behavior (`None`/defaults) the rest of the app already handles
— nothing downstream needed to change. Syntax-checked; recommend running one
manual cycle to confirm the log noise is gone before your next trading
session.

## Part 2 — Implemented 2026-07-21

The items below that were safe to implement without a deeper architectural
rewrite are now done. Two items remain genuinely out of scope for a single
pass (marked accordingly) — those still need your explicit go-ahead.

| Item | What shipped | File(s) |
|---|---|---|
| Scheduler silently missing its cron schedule | New `_log_startup_health_check()` logs the live open-file rlimit and whether `caffeinate` is in the process ancestry at every startup — both now verifiable from `scheduler.log` without shelling into the Mac. New `_check_cycle_heartbeat()` runs from the (separate) price-watch thread every ~30s during market hours; if the cron-scheduled cycle is more than 2 intervals (min 6 min) overdue, it logs a rate-limited warning (once per 5 min) instead of failing silently. | `scheduler.py` |
| SQLite lock/contention hardening | Every connection now sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000`, so ordinary multi-thread/multi-process contention retries internally instead of erroring immediately. **Note:** this does not fix the OS-level `open()` stall itself (see 2.2 below) — that happens before a connection exists, WAL/busy_timeout can't touch it. | `storage/database.py` |
| Alpaca breaker cross-contamination | Same fix pattern as the FMP split: quotes, bars, and assets each now have their own `SourceCircuitBreaker`, so a bad run on one doesn't take the others down with it. Updated the router and screener call sites accordingly. | `mcp_clients/market_data.py`, `engine/screener.py` |
| Unrotated `launchd_*.log` growth | `setup_logging()` was attaching a console handler unconditionally, duplicating every log line into launchd's unrotated stdout capture file even though the same content already goes to the properly-rotated `scheduler.log`/`server.log`. Now only attaches the console handler when stdout is a real interactive terminal (`sys.stdout.isatty()`) — interactive `./run.sh` use is unaffected, launchd-managed runs stop double-logging. | `storage/log_setup.py` |

**Still out of scope, needs your call:**

> **All three of these shipped. Corrected 2026-07-26 (documentation audit).**
> This list was accurate on 2026-07-21 and was never revisited, so a document
> titled "production readiness" has spent several weeks listing the platform's
> three biggest infrastructure gaps as open when all three were closed. Struck
> through rather than deleted — the point of this file is the 7/20–7/21 incident
> record, and an item silently vanishing from a readiness list reads worse than
> one marked done.

- ~~**Persistent DB connection pool** (replacing open-per-call) — real
  architecture change to `_conn()`/`_open_with_timeout()`'s threading model,
  wants dedicated testing before touching the live-trading DB path. Not
  attempted.~~ **DONE.** `storage/database.py` `_get_pool()` holds a
  process-wide `psycopg2.pool.ThreadedConnectionPool` shared by every
  `Database()` instance, sized by `PG_POOL_MIN`/`PG_POOL_MAX`.
- ~~**Postgres migration** — a bigger infra decision than a code fix; not
  attempted.~~ **DONE.** `storage/database.py` runs on psycopg2 against
  Postgres; `migrate_to_postgres.py` performs the move. This also retired 2.2
  below — the OS-level `open()` stall that motivated the WAL/busy_timeout work
  cannot occur against a client-server database, which is the real fix for the
  7/20 incident rather than a mitigation of it.
- ~~**External alerting** (SMS/Slack/email on a stale cycle) — the heartbeat
  *detection* now exists and logs clearly, but wiring it to an actual
  notification channel needs credentials/config only you can provide.~~
  **DONE** (§43.3, Phase 3). `engine/notifications.py` tries transports in the
  order set by `notifications.transports` in `config.yaml`: desktop
  (osascript/notify-send/win10toast) → webhook (ntfy.sh, Slack, Discord,
  Pushover — anything accepting a POST) → log, which always succeeds, so a
  notification is never silently dropped the way the old single-`osascript`
  path dropped it. The credentials note still stands in one narrow sense: the
  webhook transport does nothing until you put a URL in the config.

### 2.1 Scheduler not firing automatically (the issue you hit this morning)

**What we saw:** the cron-driven cycle didn't fire once between 7/20 16:55
and past 7/21 09:40 ET, despite the market being open — 8+ scheduled
5-minute marks silently skipped. The only cycle that ran was manually
triggered through the UI. DNS resolution failures at 08:06 CDT ("failed to
resolve data.alpaca.markets" etc.) suggest the Mac had just reconnected to
the network, i.e. it had been asleep or disconnected.

**Why it matters for prod:** a scheduler that silently stops is worse than
one that crashes loudly — nothing alerts you, so a mid-day outage could run
for hours unnoticed (which is exactly what happened with the DB-stall
incident on 7/20 — 90+ minutes hung before you caught it manually).

**Recommended fixes, in order of effort:**
1. **Verify `caffeinate` is actually attached to the live process.** `service.sh`
   wraps the scheduler in `caffeinate -i` to prevent idle sleep, but that only
   takes effect if `./service.sh install` was re-run after that line was
   added. Quick check: `ps aux | grep caffeinate` while the scheduler is
   running — you should see a `caffeinate -i` parent/sibling process. If it's
   not there, `./service.sh install` fixes it immediately.
2. **Add a heartbeat/staleness check.** A tiny script (or a line in the
   existing price-watch loop) that checks "has a cycle run in the last N
   minutes during market hours?" and writes a warning to a place you'll
   actually see it — this is separate from the scheduler process itself, so
   it keeps working even if the scheduler is the thing that died.
3. **Alerting, not just logging.** Right now every failure mode surfaces only
   as a log line you have to go find. Even a simple approach — a scheduled
   task (or cron) that tails the last cycle timestamp from the `cycles` table
   every 15 minutes during market hours and sends you a text/Slack/email if
   it's stale — would have caught both the 90-minute DB hang and this
   morning's missed-schedule issue within 15 minutes instead of you
   discovering them after the fact.

### 2.2 SQLite DB open stalls (the 90-minute hang from 7/20)

> **Resolved by the Postgres migration, 2026-07-26 audit.** Everything below is
> kept as the incident record, but it describes a failure mode that no longer
> has a mechanism: `storage/database.py` no longer opens a SQLite file per call,
> so there is no `open()` to stall. The diagnosis in the paragraph below —
> "the app's own concurrency … starving itself of OS resources" — turned out to
> be right, and moving to a pooled client-server database removed the resource
> being contended rather than raising the limit on it. Do not spend more time on
> the numbered recommendations; they were written for an architecture that has
> since been replaced.

**Status (as of 2026-07-21):** root cause still not fully confirmed. Spotlight, Time Machine,
local snapshots, and antivirus/EDR are all ruled out. ~4,000 stall warnings
over 3 days is too frequent to be an external one-off — it looks more like
the app's own concurrency (up to 6 tickers × 9 concurrent MCP calls + a
30-second price-watch loop opening its own connections, all against one
60MB SQLite file) intermittently starving itself of OS resources.

**Recommended fixes, in order of effort/risk:**
1. **Confirm the file-descriptor limit fix is actually live** (same
   `./service.sh install` gap as above — the 4096/8192 limits in the plist
   only apply if the service was reinstalled after that fix was written).
   Lowest-risk, do this first.
2. **Add a persistent connection instead of open-per-call.** Right now every
   DB read/write opens a fresh `sqlite3.connect()` in a background thread
   with a 15s timeout. A single long-lived connection (or a small connection
   pool) per process, reused across calls, would eliminate the vast majority
   of that open/close churn. This is a real code change to `storage/database.py`'s
   `_conn()`/`_open_with_timeout()` pattern — worth doing carefully with
   tests, not a quick patch, since SQLite's threading model has sharp edges.
3. **Enable WAL mode explicitly if not already** (`PRAGMA journal_mode=WAL`) —
   allows concurrent readers alongside a writer, which reduces contention
   between the price-watch thread, the main cycle, and the UI process all
   touching the same file.
4. **If stalls persist after 1–3**, consider moving off a single SQLite file
   under this level of concurrency entirely — a local Postgres (or even
   SQLite with a proper connection-per-thread pool via a library like
   `sqlite3worker`) is a more natural fit for "multiple processes, high
   write frequency" than a shared file.

### 2.3 Circuit breaker audit across other providers

The FMP fix (2.1 above) revealed a general pattern worth checking elsewhere:
**any provider class where multiple, independently-reliable endpoints share
one `SourceCircuitBreaker` instance** will have the same problem — one
permanently-broken endpoint drags down healthy ones. Worth a quick audit of
`AlpacaProvider` (quotes/bars/assets currently share one breaker) and any
other multi-endpoint provider in `mcp_clients/` to see if the same split
makes sense there.

### 2.4 Observability / log hygiene

- Log files are large (`launchd_scheduler.log` and rotated `scheduler.log.2`
  are both 5MB+) — confirm there's a rotation/retention policy so these
  don't eventually fill the disk (which would itself cause new failures).
- The existing "Data Sources" health panel (mentioned in `base.py` comments)
  tracks per-source success/failure — worth extending it to also surface
  "last successful automatic cycle" front-and-center, since that's the
  metric that actually matters for "is this thing working."

## Suggested next step

Items 2.1.1 and 2.2.1 (both just confirming `./service.sh install` picked up
the live plist) are the cheapest, lowest-risk things to check first — they
may already explain both the missed schedule and a meaningful share of the
DB stalls, and cost nothing but running one command and checking the output.
