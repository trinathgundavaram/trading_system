# Changelog

All notable changes to trading_platform. Format follows [Keep a Changelog];
version labels use the project shorthand (`1.01` = tag `v1.0.1`) — see §35 of
the remediation plan, and `scripts/version.py`, which is the only place that
knows how the two forms map onto each other.

Every entry states whether the **DECISION FUNCTION** changed, because that
determines whether trade history from earlier versions may be pooled with this
one. Conventional semver asks "does this break a caller's code?" — the wrong
question here, since this system has no external callers. The question that
matters is whether the mapping from market data to a buy, a size, or an exit
moved. If it did, every trade recorded before the release was produced by a
different strategy, and averaging the two sets together is a measurement error.

`scripts/classify_change.py` applies that rule to the diff mechanically, and
`scripts/release.sh` refuses to proceed quietly when the two disagree.

[Keep a Changelog]: https://keepachangelog.com/en/1.1.0/

## [Unreleased]

Nothing.

## [2.2] — v2.2.0 — 2026-07-26

### The Phase 2.5 cutover, actually run

Six defects, every one found by RUNNING the sequence rather than reading it.
The migrations, the purge and the backup step had all been reasoned about
carefully and never executed once.

**Decision function: UNCHANGED.** Nothing here touches scoring, sizing, entry
or exit logic. No config value a rule reads has moved; `config_fingerprint` is
unchanged at `cc9a149613427f56`.

### Fixed — the cutover could not have completed

- **`migrations/012` died before reading a row, on every database `tp install`
  creates.** Its guard ran `trade_id !~ '^[0-9]+$'` unconditionally, but
  `storage/database.py`'s `SCHEMA` had since been updated to declare
  `trade_id INTEGER` — so on any database built by `init_db()`, Postgres
  answered `operator does not exist: integer !~ unknown` and step B5 of
  `phase2_5_cutover.sh` aborted. The migration was written against the live
  box's TEXT column and had only ever been reasoned about, never executed.

  Two shapes exist in the world and only one had been considered: the LEGACY
  live database (`trade_id TEXT`, `id TEXT` holding uuid4, no FK) and the
  FRESH one that `Database.init_db()` now produces, born in the post-012 shape.
  `tp install` makes a new database per version — that is the whole point of
  §38 — so FRESH is the common case going forward and LEGACY exists on exactly
  one machine, once. 012 now detects which it is looking at, applies only what
  is missing, and is idempotent, which also restores `--from B5` as a usable
  resume point after a partial failure.

- **`tp` allocated a port the registry thought was free and the OS did not.**
  `registry_add()` picked the lowest port not claimed by another *managed*
  version, which says nothing about `./run.sh --ui` from the working tree or
  anything else on the machine. §38's promise that each version gets its own
  port held between managed versions while colliding with the unmanaged UI,
  so the first `tp run` of a new version died on `[errno 48] address already
  in use`. It now tests whether the port can actually be bound, and says which
  process to look for when none in the range can.

### Added — the two pieces Phase 4 was waiting on

- **`scripts/compare_versions.py` (§40).** Deferred since v1.3.0 and named in
  two places as the thing that makes a claim measurable: Phase 3's exit
  criterion ("same backtest in two tags, identical numbers") and Phase 4's
  justification ("a measured before-and-after"). Until now both were
  assertions.

  It treats the comparison as two questions, not one, and decides which
  applies from the config fingerprint. **Same fingerprint** means the two runs
  were meant to be the same computation, so any divergence is a
  reproducibility defect — an unpinned numeric library, a different pandas,
  one side on `ta_fallback.py` (§13). **Different fingerprint** means the
  decision function moved, so difference is the *result*; what would be a
  fault there is *no* difference, since a recalibration that changes nothing
  measurable has not been shown to do anything. Conflating those two is how a
  reproducibility bug gets filed as "expected, we changed the scoring" and how
  an inert recalibration gets declared validated.

  Trades are compared as a set keyed on (ticker, entry date), not just at
  summary level: two runs can agree on trade count, win rate and profit factor
  while disagreeing about which trades those were.

- **`scripts/phase4_recalibrate.py` (§19–§21).** The recalibration harness —
  the machinery, deliberately not the numbers.

  `assess` is the gate and the most important part: it refuses a sample that
  is too small (150, reusing `learning.min_trades_before_bayesian` rather than
  inventing a second answer), too short (90 days — a fit to one regime is a
  fit to that regime), missing the §48 epoch, or thin on linked excursion rows.
  It also names every feature that never varies in the sample, which is the
  placeholder problem stated in numbers instead of prose.

  `propose` derives §19 weights from measured rank correlation with outcome,
  §20 thresholds from the realised outcome distribution, and §21 sizing input
  from the MAE distribution — and writes a proposal file. There are no default
  weights, no fallback thresholds and no hardcoded tiers anywhere in it: if the
  sample cannot support a number it says so and exits non-zero rather than
  emitting a plausible one.

  **It edits no config.** A recalibration that silently rewrote `config.yaml`
  would move the decision function with no release, no declared fingerprint
  change and no §35 boundary — and a test asserts the script contains no yaml
  writer, because that property is worth more than the intention behind it.

  `receipt` writes the §32 validation receipt. `engine/live_trader.py` has
  looked for that file since Phase 1 and never found one, because nothing had
  ever written it — so "validation receipt gate blocks arming" had been
  passing for the least interesting possible reason. A passing receipt needs a
  backtest that ran, a comparison that exists, and a `--signed-off-by`, since
  a receipt records that a person read the numbers and an unattributed one
  records nothing. A failing receipt is written rather than skipped: the code
  distinguishes "last validation FAILED" from "no receipt", and the first is
  the more useful thing to find.

- **`scripts/diagnose_drawdown.py`** — read-only, and written the night the
  kill switch tripped on a 16.48% paper running drawdown. §11's control fired
  correctly; what it could not tell anyone is whether the *number* was real.
  `storage/database.py` already documents the trap — a curve at ~984 that
  jumps to 1491 on a re-seed makes every later day read a ~34% drawdown — and
  `_paper_epoch_start()` guards the RESET case but not a re-seed *within* the
  current epoch, which is the case on this machine because §48 has not run.

  The detector is an accounting identity rather than a threshold. Since
  `total_value = cash + market_value`: market movement leaves cash flat, a buy
  or sell moves the two legs in opposite directions and cancels, and only a
  balance change leaves cash moving unmatched. The first version instead
  flagged any large move not matched by realized P&L, and promptly called an
  ordinary 7.8% market decline "unexplained" — unrealized losses do not touch
  `realized_pnl`. Tests now pin all four events.

  It then rebases the series and reports what the drawdown would be if the
  balance changes had not happened. Rebasing rather than "measuring from the
  last jump", because the latter handles a permanent re-seed and misses the
  worse case completely: a transient spike, where one bad balance sample
  becomes the all-time peak and every subsequent day is measured against a
  number the account held for one sample. On a seeded reproduction that case
  reads 35.21% unrebased and 1.83% rebased.

### Added

- **`scripts/rehearse_cutover.py`** — runs the whole migration sequence against
  a throwaway database, in both shapes, and asserts what the cutover assumes:
  that 010 and 012 REFUSE while contamination is present, that both shapes
  converge on `trade_id INTEGER` with exactly one FK, that 012 is re-runnable,
  and that deleting a position NULLs the excursion row rather than cascading
  it — the property `reset_paper_account()`'s docstring promises and nothing
  tested. It refuses to open the live database or any `tp_v*` version database,
  since it drops tables. This is what found the 012 defect above.

- **`TP_PG_POOL_MIN` / `TP_PG_POOL_MAX`.** The pool was hardcoded 2-20, so a
  server that cannot give out two connections — a small `max_connections`, a
  pgbouncer, or the single-client Postgres the rehearsal runs against — failed
  inside `Database.__init__` as "server closed the connection unexpectedly",
  which reads like the server died rather than like a pool asking for more
  than it can have. Defaults are unchanged at 2-20.

## [2.1] — v2.1.0 — 2026-07-25

### Phase 3 (§41–§47) — portability and reproducibility

**Decision function: UNCHANGED.** Nothing here touches scoring, sizing, entry
or exit logic. `config.yaml` gains three keys
(`trading.max_clock_skew_seconds`, `notifications.transports`,
`notifications.webhook_url`); none is read by a rule. `classify_change.py` will
report MAJOR because `scheduler.py` and `storage/database.py` sit in
`DECISION_PATHS` — the changes in both are a pre-cycle clock guard and a
`pg_notify` next to an existing INSERT. `config_fingerprint` is **unchanged at
`cc9a149613427f56`**: the three new keys are notification and clock settings,
and the fingerprint deliberately covers only values that alter a decision.

**The point of this phase is a property, not a feature.** §41 inventoried
eleven macOS-locked places, three of them safety-critical, and a fourth,
subtler problem: three operating systems × several Python versions × unpinned
numeric libraries is a matrix in which the same bars can produce different
indicator values, therefore different scores, therefore different trades, with
no error and no log line. Phase 4 is a large change to the decision function
whose entire justification is a measured before-and-after — and that
measurement is only trustworthy if both versions compute indicators
identically. This is what makes Phase 4 provable rather than hopeful.

#### The three that were safety-critical

- hang protection now works off macOS — `engine/cycle_supervisor.py` — §43.2

  `os.killpg`, `os.getpgid` and `signal.SIGKILL` do not exist on Windows, so
  the module raised `AttributeError` at import and the platform had **no hang
  protection at all** there. POSIX keeps the process-group path, which is
  atomic; Windows gets a psutil tree walk, which is racier and is the only
  mechanism the OS offers.

  **`EPERM` is an exit condition, not an error — caught before tagging.** The
  POSIX path's alive-check (`killpg(pgid, 0)`) treated `PermissionError` as
  unexpected. A child dies on SIGTERM but stays a zombie until its parent reaps
  it, and `run_supervised()` reaps only *after* the kill call returns — so for
  the whole grace loop the group holds one unreaped member. Darwin and the BSDs
  clear a process's credentials on exit and answer that signal with `EPERM`
  rather than `ESRCH`; Linux answers `0`, which is why only the release machine
  ever saw it, and why it surfaced in `release.sh`'s own test gate rather than
  in CI. Uncaught, the exception escaped `run_supervised()`'s
  `except subprocess.TimeoutExpired` block *before* `mark_cycle_killed()` ran:
  a cycle killed at the 15-minute ceiling would have been recorded as a clean
  finish, and `/api/cycle/cancel` would have returned 500 to the UI while
  having in fact killed the cycle. `killpg()` reports `EPERM` only when it
  could signal no member of the group, so there is by definition nothing left
  to escalate to; it now returns exactly as `ESRCH` does, at all three call
  sites. Regression tests simulate `EPERM` rather than staging a real zombie,
  because which errno a kernel returns here is precisely the non-portable part.

- secrets no longer fall back to plaintext off macOS — `storage/secrets.py` — §44

  The `security` binary exists only on macOS. Everywhere else the keychain tier
  silently returned `''` and the system fell back to the environment, which in
  practice means a file — the exact thing §3 and §39 exist to eliminate. Now
  `keyring`: Keychain, Credential Locker, Secret Service, or an encrypted file
  on headless Linux. Environment stays FIRST, deliberately, because that is
  what makes containers and CI possible.

  **The upgrade silently orphaned every stored secret — caught before tagging.**
  §44 claimed that keeping the `tp_` prefix meant "the Keychain items are the
  same items, the library reading them is what changed". It did not. The old
  code stored service=`tp_UI_AUTH_TOKEN`, account=`$USER`; `keyring` stores
  service=`trading_platform`, account=`tp_UI_AUTH_TOKEN`. The prefix moved from
  one Keychain field to another, which makes two different items — so on any
  machine with `keyring` installed, every credential written by v2.0.0 became
  invisible. Not as an error: `get()` falls through to the environment, so the
  caller receives `''`, and an empty `UI_AUTH_TOKEN` 503s every write endpoint
  on a dashboard that otherwise looks healthy. `_keychain()` now reads the
  current location, then the legacy one, then the `security` binary (which an
  installed `keyring` used to short-circuit — on exactly the machine that has a
  legacy item to find), and copies anything found forward so the migration
  happens once, on first read. The old item is deliberately left in place: a
  rollback to v2.0.0 has to keep working, and an upgrade that destroys the only
  copy of a credential is worse than reading two locations forever.

- background services exist on every OS — `scripts/services.py`, `service.sh` — §45

  `service.sh` was 100% launchd. Elsewhere the scheduler ran in a foreground
  terminal and died with the window — the failure `service.sh` was written to
  fix, reintroduced everywhere else. `service.sh` is now a shim; the original
  is kept as `service.launchd.sh.bak`.

#### The container (§42)

- `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/healthcheck.py`,
  `scripts/build_version.sh`

  The image pins the OS, the Python build, every wheel and the timezone
  database, and **asserts `pandas_ta` at build time** — failing the build is
  infinitely better than discovering at runtime that this image fell back to
  `ta_fallback.py` and computed scores comparable with nothing (§13). The UI
  publishes on `127.0.0.1` only; `TP_FORCE_PAPER` defaults to 1; container logs
  are capped so E-10 cannot recur.

- a skewed clock refuses to trade — `scheduler.py` — §42.4

  Docker Desktop's VM clock can lag the host badly after a laptop sleeps, and
  every market-hours and stop decision is a function of the local clock. Above
  120s of skew the cycle aborts and is RECORDED. Compared against an HTTP
  `Date` header — no NTP client in the image, cached for five minutes. "Cannot
  tell" (no network) proceeds: turning a network blip into a trading outage
  would be the worse trade.

#### The split (§47)

- the engine states events, the host delivers them — `scheduler.py`,
  `storage/database.py`, `scripts/tp_agent.py` — §47.3, §47.4

  `log_ui_event()` was already a cross-process outbox with two consumers. The
  host agent is a third. A transactional `pg_notify` alongside the INSERT means
  it listens rather than polls, removing a one-second latency floor on the
  kill-switch alert — and NOTIFY fires only if the INSERT commits, so the agent
  can never be told about an event that was rolled back. The agent is ~150
  lines and is now the **entire OS-specific surface** of the platform. It
  contains no trading logic: if it dies, the engine keeps running and you stop
  getting popups.

- clipboard and file-open moved into the browser — `server.py`, `ui/index.html` — §47.5

  `/api/prompt/copy` shelled out to `pbcopy` on the SERVER, which assumed the
  server and the human were the same machine. That was already false over an
  SSH tunnel. Replaced by `navigator.clipboard` and `/api/prompt/raw` — two
  features improved, two shell-outs removed, and both now work from a phone.

  Retiring that POST took the write-route count from 15 to 14, which tripped
  `test_ui_auth.py`'s vacuity floor (`>= 15`). The floor is stale, not the app:
  the assertion that matters — no write route lacks the `require_token`
  dependency — passed throughout. Lowered to 14 and now backed by an explicit
  list of the routes that move money or state, since a bare count can be
  quietly decremented to turn a red test green and a named route cannot.

- `deploy/com.tradingplatform.stack.plist` — §47.6

  launchd's job shrinks to "bring the stack up, keep the agent alive". The
  wait-for-Docker loop is not padding: Docker Desktop takes 20–40s after login,
  and a compose command issued before then fails in a way that looks exactly
  like the 22 July incident.

#### Everything else

- one place for OS-specific calls — `storage/platform_support.py`, `main.py` — §43.1
- notifications are a transport chain, and `log` is last and always succeeds,
  so a notification is never silently dropped — `engine/notifications.py` — §43.3
- `scripts/tp` rewritten in Python; `rm -P`, `shasum` and `sed -i ''` were all
  BSD-only — `scripts/tp` (bash kept as `scripts/tp.sh.bak`) — §46.1
- `scripts/bootstrap.py` reports what is missing with the install command for
  *this* OS — §46.2
- `psutil`, `keyring`, `keyrings.cryptfile`, `tzdata` pinned — `requirements.txt`
- `rich` pinned 13.7.1 → **14.2.0**. 13.7.1 was a number nobody had run:
  `main.py` imports `rich` at module scope and the UI process has been up on
  14.2.0 throughout, so the pin described an environment that did not exist —
  the exact failure §13 is for. Installing it also broke an unrelated package
  in the shared conda base (`fastmcp-slim` needs `rich>=13.9.4`). ROUTINE tier:
  presentation only, no score moves, no re-validation.
- **§13's drift guard stopped crying wolf** — `scripts/check_deps.py`,
  `scripts/pin_requirements.py`

  Both read `pip freeze` and kept only lines containing `==`. A conda-built or
  locally-installed distribution is rendered `pandas @ file:///croot/...`,
  because that is the form that would reinstall it — so on the release machine,
  an Anaconda base env, sixteen packages that were present and working were
  reported NOT INSTALLED, two flagged SCORE-AFFECTING, `pytest` among them
  while it was running the suite that had just passed. A guard that is wrong
  about sixteen packages is one nobody reads by the third release, which
  defeats the point of having it. Both now read installed metadata via
  `importlib.metadata` — the same `.dist-info` the import system reads, so the
  reported version is the one actually loaded however it was installed. As a
  consequence `requirements.lock.txt` is synthesised as `name==version` rather
  than raw freeze output, which could otherwise write a `file:///` path from
  one machine into the file whose purpose (§42) is rebuilding elsewhere.
- `tests/test_phase3_portability.py`, including a lint that fails the build if
  a macOS-only binary appears in the engine

#### Not done, and deliberately

The Phase 3 **exit criterion** is not a code change and cannot be claimed by
this entry: build images from two tags, run the same backtest window in both,
and confirm the shared code paths produce identical numbers. Until that has
been run, the reproducibility claim above is a design intention rather than a
measured fact. `scripts/build_version.sh` exists to make it a short exercise.

## [2.0] — v2.0.0 — 2026-07-25

Phase 2.5 (§48–§55): make the measurement base honest before Phase 3.

**Why this is a major bump and v1.4.0/v1.5.0 were not used.** The plan
([docs/PHASE2_5_PLAN.md](docs/PHASE2_5_PLAN.md)) suggested splitting this work
across two releases — v1.4.0 for the decision-function-neutral half and v1.5.0
for §48/§52/§53. It shipped as one body of work instead, and §53 is in it. Once
a release contains a decision-function change, §35's rule is not a preference:
trade history either side of this tag was produced by different strategies, and
averaging the two sets together is a measurement error. A minor bump would have
buried that boundary where nobody looks for it.

### Decision function: CHANGED by §53 — re-validation required before arming live

Phase 2.5 (§48–§55) complete as far as code can take it. Plan and adjudication
of the 2026-07-25 external review:
[docs/PHASE2_5_PLAN.md](docs/PHASE2_5_PLAN.md).

`scripts/classify_change.py` reports MAJOR, and most of that is the
conservative heuristics: `engine/rules_catalog.py` sits in `DECISION_PATHS` and
changed only in description strings plus one added `enforced_in` key, and the
`migrations/` rule fires on 009/010/011, which add two nullable columns and
three indexes. `config.yaml` changed only by removing two keys nothing read, and
`config_fingerprint` is **unchanged at `cc9a149613427f56`**.

**One change is genuinely decision-moving and should not be lost in that
paragraph.** §53 changes which quantity `engine/portfolio_risk.py` counts as an
open high-volatility position, and portfolio risk sizes and can block entries.
The count becomes *stricter* — the old proxy read low — so expect marginally
more size reduction around volatile names. Pattern rows remain poolable
individually; anything reasoning about position sizing across this boundary has
to account for it.

Nothing else is on the decision path: nothing reads `exit_kind` or
`get_pattern_excursions()` yet (lifting `ev_engine`'s `p_stop_loss` onto them is
Phase 3, and will be its own declared change), and removing a config key with no
reader cannot change a decision.

**The second review's follow-ups (§C1–§C3, §D) add no further decision change.**
`config.yaml` is untouched and `config_fingerprint` is still
`cc9a149613427f56`. `classify_change.py` will report MAJOR again because
`rules/sell_rules.py` and `engine/stop_state_machine.py` sit in
`DECISION_PATHS` and `migrations/012` exists — but §D adds a *field* to
`SellResult` and reads `stop_state`, which was already on the row. Every
existing field (`should_sell`, `triggered_rule`, `reason`, `urgency`) is
byte-identical across all eight trigger branches; §C3 changes a label; §C2
gates a CLI command; §C1 constrains a table nothing on the decision path
reads. The heuristic is being conservative, correctly, and the answer is still
that §53 is the one change that moved a decision.

### Added

- **§50** `pattern_database.exit_kind` (`migrations/009`): the countable
  companion to `exit_reason`. The reason string interpolates the price into
  itself, so four stop-loss exits were recorded as four distinct strings and
  the column could not be grouped at all — which is why `ev_engine`'s
  `p_stop_loss` is a horizon proxy and says so. `rules/common.classify_exit()`
  derives the kind from reasons that are structured tokens and returns None for
  prose, deliberately: a bucket half-filled by guesswork is worse than an empty
  one. Values outside `EXIT_KINDS` are refused rather than stored.
- **§51** `Database.link_pattern_to_trade()` and `get_pattern_excursions()`
  (`migrations/010`). `pattern_database.trade_id` had existed since the table
  was created and was NULL on every row, because its only writer runs at signal
  time when no position exists. Both ids are in scope exactly once — just after
  the position opens — and that is now where the link is written.
- **§49** `scripts/assess_test_damage.py` and `repair_test_damage.py` now cover
  `mae_mfe_data`, by evidence rather than by time window (the table has no
  provenance columns to filter on).
- **§53** `positions.entry_atr_pct` (`migrations/011`), populated by
  `scheduler.py` and `confirm_fill.py` from the ATR already in scope at entry.
- **§52** `scripts/calibrate_risk_caps.py`. Writes nothing; turns the equity
  curve into a recommended `max_intraday_drawdown_pct`, refuses to recommend
  below `--min-days` because a percentile of four observations is arithmetic
  rather than evidence, flags any day showing ≥10% intraday drawdown as far
  more likely to be a purse re-seed than a trading loss, and converts the cap
  into dollars at current equity so the scale dependence the review asked us to
  document is visible rather than inferred.
- **§D** structured exit codes at the point of decision.
  `rules/sell_rules.py` now emits an `exit_kind` on `SellResult`, threaded
  through `scheduler.py` → `paper_trader`/`live_trader` → `close_trade()` →
  `close_pattern(exit_kind=...)`. §50 deliberately refused to classify
  `sell_rules:` strings — they are free text with prices interpolated in, and
  prefix-matching "Dynamic stop hit" would be a table that silently drifts
  from its producer — which left `exit_kind` NULL on the **most common exit
  path**. The fix is emitting the token where the trigger fires, not a
  smarter parser.

  The distinction that matters is inside the stop machine:
  `INITIAL_RISK`/`TRADE_CONFIRMING` are a loss being capped
  (`stop_loss`), while `BREAKEVEN`/`PROFIT_PROTECT`/`TREND_FOLLOWING` are a
  winner giving some back (`trailing_stop`). Identical trigger, identical
  reason-string shape; only `stop_state` tells them apart, and by the time
  `close_pattern()` sees the sentence the state is inside a parenthesis.
  Folding them together is how a future `p_stop_loss` would count winners as
  stop-outs and come out biased high. `StopState.exit_kind` and
  `rules/common.STOP_STATE_EXIT_KINDS` hold that mapping in one place.

  Also covered: rotation victims (`rotation` — closed to make room, not on
  their own merits), Loop B urgent exits (`eod_flatten` for the clock event,
  `rule_exit` for everything else — six labels would become six buckets of a
  handful of rows each), and `server.py`'s Sell button, which passed
  `reason="manual_ui"` that `classify_exit()` never recognised, so the one
  exit whose kind is least ambiguous was landing NULL.
- **§C1** `migrations/012_mae_mfe_fk.sql`. `010` added the unique index on
  `mae_mfe_data.trade_id` — the load-bearing half — but left the types alone:
  `id` was a uuid4 TEXT primary key and `trade_id` a stringified
  `positions.id` with no FK, so nothing stopped a row naming a trade that
  does not exist. `trade_id` is now `INTEGER REFERENCES positions(id)`, `id`
  is a `BIGINT` identity column, and `insert_mae_mfe()` no longer mints a
  uuid or accepts a non-numeric `trade_id`.

  **No deploy-order constraint.** `CREATE TABLE IF NOT EXISTS` is a no-op on a
  database that already has the table, so this code shipped ahead of `012`
  would meet the old `id TEXT PRIMARY KEY` — NOT NULL, no default — and every
  excursion write would raise. `insert_mae_mfe()` therefore probes the column
  type once per process and supplies a uuid when the pre-`012` schema is still
  in place, warning on each startup so it cannot be forgotten. Code and
  migration can land in either order, which matters for a system whose
  scheduler restarts on a timer rather than when someone is watching. `012`
  uses `GENERATED BY DEFAULT`, not `ALWAYS`, so a process that probed before
  the migration keeps working after it.

  **ON DELETE SET NULL, not CASCADE.** `reset_paper_account()` deletes every
  simulated position by design and deliberately does *not* delete excursion
  rows; CASCADE would make the reset silently destroy history its own
  docstring promises to keep, and nobody would notice until an MAE average
  came back thin. A trade's maximum adverse excursion stays true after its
  position row is gone — what stops being true is *which* position it was.
  The migration refuses to run on dirty data with a message naming the count,
  rather than failing later on a cast error naming a row.
- **§C2** `robinhood_sync.py`'s `seed-paper` is gated. It printed what it was
  about to destroy and destroyed it on the next statement, with no way to
  stop in between — while every other destructive path in this repo has a
  gate. Now: an itemised list of what goes (including the equity-curve point
  count), a typed confirmation phrase deliberately different from
  `LIVE_EXECUTION_CONFIRM_PHRASE`, and a verified `tp backup` that aborts
  rather than prompting if the dump fails. The redundant
  `DELETE FROM paper_equity_history` (and its reach into `db._lock`/`db._conn`
  from a top-level script) is gone — §48 moved it inside
  `reset_paper_account()`, and leaving it here implied the reset does not
  clear the curve.
- **§C3** `packet_builder.high_vol_line()`. The packet said "High-vol
  positions open: N" both before and after §53, but the quantity changed —
  stop distance then, entry ATR% now — and the old one read systematically
  low. It now names the unit, and while any position predates
  `migrations/011` it also reports the proxy share, because a mixture printed
  as a plain integer looks measured whichever way it was arrived at.
  `PortfolioRiskResult.high_vol_proxy_count` carries that number.
- **§55** the stale-data circuit breaker now writes a `rejected_signals` row
  (`reject_stage = "data_quality"`) naming the defaulted indicators, so
  `data_quality.stale_indicator_veto_threshold` can be set from a week of
  evidence instead of swapped for another guess. Only this veto is
  instrumented — the others are decisions about the name; this one is a
  decision about our own data.
- `tests/test_exit_vocabulary.py`, `tests/test_risk_calibration.py`,
  `tests/test_review_followups.py` (44 covering §C1–§C3 and §D, most of them
  database-free on purpose — a test that needs Postgres to check a string gets
  skipped on the machine where someone is editing the string).
- `scripts/phase2_5_cutover.sh` — the operational tail (§B1–§B9) as one
  resumable, dry-run-by-default sequence with a gate between every step. A
  script rather than a checklist because the ordering is not advisory: B5
  before B3 gets a migration that aborts by design, and B8 before B6
  calibrates against a curve spanning a purse re-seed, which is the arithmetic
  v1.3.1 exists because of.

### Fixed

- **§49** the 2026-07-25 test-against-production cleanup missed `mae_mfe_data`
  entirely: it was absent from `repair_test_damage.py`'s `PURGE` list while
  every neighbouring table was cleaned. On the pre-Postgres snapshot, 22 of 25
  rows are test-fixture residue.

  The consequence is worse than the residue. `mae_mfe_data.trade_id` is TEXT
  with no unique constraint and no book scope, and `trade_id = '1'` is claimed
  by five different tickers — so joining patterns to excursions returned 37 rows
  for 23 patterns, with NVDA's excursion attaching itself to ADPT's pattern. An
  `AVG(mae_pct)` over that join is wrong in a way nothing about the query looks
  wrong, and Phase 3 was going to recalibrate against it.

  `get_pattern_excursions()` is now the single sanctioned join: one indexed hop,
  ticker agreement required, §15's quarantine honoured on both sides, and a
  redundant in-Python dedupe so a database restored without `migrations/010`'s
  unique index degrades to a warning rather than to wrong averages.

- **§54** removed six module-level `check_*` risk helpers and `LegacyRiskEngine`
  from `rules/risk_rules.py`. Zero call sites, including in tests, while the
  limits they described are genuinely enforced in `position_sizing.py`,
  `live_trader.py`, `paper_trader.py` and `RiskEngine.check()`. Two
  implementations of one limit is a coin flip about which one a future edit
  lands in — and they had already diverged: the dead copy compared today's trade
  count with `>=` where the live one uses `>`, and read `max_daily_loss_usd` raw
  where the live path uses §8's equity-scaled `daily_loss_limit()`.

- **§54** `ACCOUNT_RISK_CATALOG` attributed all eight account-risk checks to
  `rules/risk_rules.py`, which is where the dead copy lived; each entry now
  names its real enforcement site. It also documented
  `max_intraday_drawdown_pct` as defaulting to 3.0% while `config.yaml` says
  2.0 — a catalogue that can disagree with the config is worse than none,
  because it is what someone reads instead of the config. A test now pins them
  together.

- **§53** `engine/portfolio_risk.py`'s high-volatility count compared two
  different quantities against one threshold: the candidate arrived as a true
  ATR percentage (`atr / price * 100`), while open positions were measured by
  `_position_risk_band_pct` — stop distance as a percentage of entry — both
  against `high_vol_atr_pct_threshold`.

  The proxy defended itself on the grounds that wider stops track wider ATR,
  and that holds for ranking. It does not hold against a threshold denominated
  in ATR, and it is biased in one direction twice over. `risk_per_share` is
  `min(max(1.2*ATR, price*1.5%), price*stop_loss_pct)`, so past a certain
  volatility the stop is clamped while ATR keeps going and the proxy saturates.
  And the stop ratchets as a position moves in favour, so a position's measured
  volatility *fell the better it did* — a winner quietly stopped counting
  toward the cap. `max_simultaneous_high_vol_positions` was therefore looser in
  practice than it read, and recalibrating the threshold (which the review
  asked for) could not have fixed it.

  `_position_atr_pct()` now reads the persisted entry ATR, with the proxy kept
  as an explicit, debug-logged fallback for rows predating `migrations/011`.
  Absent volatility is not zero volatility.

- **§48** `reset_paper_account()` now clears `paper_equity_history` too. It did
  not, so a "clean slate" account inherited the previous account's equity curve
  — and that curve is the input to every drawdown figure. v1.3.1 exists because
  of what a discontinuity there does: a downward re-seed reads as a 33%
  intraday drawdown and, against a 2.0% cap, blocks entries for the rest of the
  day for an accounting event. The epoch guards stay; they remain load-bearing
  for a re-seed, which does not delete the account.

- **§54** removed `risk.daily_loss_limit_triggered` and
  `risk.daily_profit_lock_triggered` from `config.yaml`, `rules/hard_vetoes.py`,
  `engine/position_management.py` and the catalogue. Nothing ever wrote either
  flag, while three readers treated them as live controls — one of which used
  the loss flag as a **priority-1 exit-everything** trigger, so hand-setting a
  writerless key liquidated the book. No capability is lost:
  `kill_switch_triggered` is the documented manual halt, it has a writer, a
  persist step, a notification and a test, and it reaches the same priority-1
  branch. `config_fingerprint` is unchanged, so this is not a decision change.

- **§54** `scripts/backfill_drawdown.py` printed "the configured 3.0%" while
  `config.yaml` said 2.0 — the same drift as the catalogue, in the place an
  operator is most likely to read it as authoritative. It now reads the value.

### Known / deferred

- `migrations/009`, `010`, `011` and `012` are written but **not applied**.
  010 will fail while duplicate `trade_id`s remain and 012 raises on any
  orphan or non-numeric `trade_id`; those failures are the gate on §49's
  purge, not bugs. `scripts/phase2_5_cutover.sh` sequences all of this.
- **§48's reset has not been run.** It is destructive and needs the live
  database; nothing in this work touched production data. Run
  `scripts/assess_test_damage.py` and `scripts/tp backup` first.
- **§52's cap values are unchanged.** The tooling is in; the numbers need a
  clean curve, so they wait on §48.
- **§53's `high_vol_atr_pct_threshold` is unchanged at 5.0.** Recalibrating it
  is now worth doing — before this change it would have been tuning against the
  wrong axis.
- **§55's threshold is unchanged at 3.** It needs a week of the new
  `rejected_signals` rows.
- 92 tests require Postgres and were not executed in the environment this work
  was done in. Run `pytest` locally before releasing.

## [1.3.1] — v1.3.1 — 2026-07-25

### Decision function: unchanged — v1.3.0 trade data remains poolable

A patch. The control shipped in v1.3.0 and was computing one of its two
numbers incorrectly under one condition.

Full note: [docs/releases/v1.3.1.md](docs/releases/v1.3.1.md).

### Fixed

- **§11** the paper-account epoch now bounds the **intraday** drawdown window,
  not only the running peak. v1.3.0 fixed half of this: scoping the peak alone
  left a mid-day reset inside today's window, so the peak-to-trough scan ran
  across the discontinuity. A re-seed downward — 1491 back to a 1000
  `starting_cash` — reads as a 33% intraday drawdown, which against the 2.0%
  cap blocks entries for the rest of the day, for an accounting event.

  The 2026-07-25 re-seed stepped *up*, and an upward step produces no
  drawdown, so the live data exercised the running half of the bug and was
  silent about the intraday half. It surfaced because a test failed on an
  unrelated assertion.

- `backfill_drawdown` drops pre-reset points on the epoch day only. Earlier
  days keep theirs — they belong to the previous account and are self-contained
  and true for it.

- test fixture: `_equity()` wrote today's points at a fixed 10:00 local, which
  depending on the hour the suite ran landed *before* `init_paper_account`
  stamped `created_at`. They were then correctly excluded and the assertion
  read 0.0% against an expected 1.6% — the code was right and the fixture
  depended on what time you ran it. Points are now written forward from
  `utcnow()`.

## [1.3] — v1.3.0 — 2026-07-25

### Decision function: CHANGED — re-validation required before arming live

Phase 2 complete: all ten steps (2.1–2.10). This is the release where "the
config says a $500 daily loss limit" becomes true at runtime rather than being
a sentence in a file.

`scripts/classify_change.py v1.1.0` reports MAJOR. This ships as a **minor**
bump, and the disagreement is recorded rather than waved through: the MAJOR
comes from the deliberately conservative `migrations/` heuristic, and
migrations 005–008 are additive columns, one partial index and one data
quarantine. No scoring weight, threshold, bucket or stop rule moved, and
`config_fingerprint` is unchanged at `cc9a149613427f56`.

The decision function is nonetheless flagged as changed, for one specific
reason: §15's quarantine filter applies to `get_patterns` for **every** reader
including the live path, so `engine/ev_engine.py` now draws on a different
sample and the same candidate can receive a different EV. Pattern rows remain
poolable **individually** with v1.1.0; anything reasoning about the population
must account for §18's new selection filter as well.

Full note: [docs/releases/v1.3.0.md](docs/releases/v1.3.0.md).

### There is no v1.2.0

`docs/releases/v1.2.0.md` was written and never tagged; its content ships
here. The number is skipped rather than reused — reusing it would mean two
different trees had at different times been called v1.2.0, which is worse than
a gap. The note is kept and marked superseded.

### Added — the risk controls have real inputs

- **§7** paper trades increment their own daily counters. `daily_stats` was a
  live-book table, so on a paper-only deployment the cap read zero forever:
  31 buys across seven days against a 10/day cap, "0 trades placed" every day.
- **§8** the daily-loss limit resolves against actual equity — the tighter of
  the absolute $ and a percentage. $500 against a $1,000 account is not a
  limit, it is a number.
- **§9** the automatic kill switch is wired, and the three bugs that would
  have stopped it firing are fixed. It had zero call sites, so none had ever
  surfaced.
- **§10** the risk gate moved inside `execute_buy`. A cycle that began at 9
  trades and found 15 candidates placed all 15.
- **§11** drawdown is computed and persisted on every equity point, and both
  caps bind. An intraday breach blocks entries for the day; a running breach
  trips the kill switch, because 15% off the all-time high is not a bad day.

### Added — structural guarantees

- **§14** opening a position is one transaction: a partial unique index, an
  advisory lock for the cap, and a conditional debit for the purse. Six
  workers on one ticker now open one position.
- **§15** `data_quality` quarantine on the learning tables; `close_position`
  is the single definition of P&L and hold time; `scripts/reconcile.py` fails
  loudly on cross-table disagreement.
- **§16** every by-ticker position write is book-scoped and raises without it.
  A $100 paper entry in HCA could previously overwrite the stop on an $8,553
  real holding of the same ticker.
- **§18** portfolio risk derives themes from cached sector/industry, blocks on
  a severe breach, and records every rejection with the size it would have
  taken.

### Added — verification

- `scripts/verify_phase2.py` — 29 checks that the guards are **in force**, not
  merely present. `release.sh` and `run.sh` both consult it.
- `scripts/apply_migration.sh`, `scripts/inspect_duplicate_positions.py`,
  `scripts/backfill_drawdown.py`.

### Changed

- `release.sh`'s pytest gate is now hard. It soft-failed with a y/N prompt
  while §12 was outstanding; §12 is done, and the 2026-07-25 incident was a
  `release.sh` run whose suite executed against the live database.
- `run.sh` runs a guard preflight. On failure the UI still starts — it is
  read-mostly and it is how you diagnose — while the scheduler asks first.

### Fixed

- Running drawdown measured its peak across account re-seeds, so the
  2026-07-25 re-seed (curve stepping ~984 → 1491.54) would have read a ~34%
  drawdown against a 15% cap and tripped the kill switch on the next cycle.
  The peak is now scoped to the current paper-account epoch.
- `RiskEngine.check()` raised `KeyError` on a config with no `risk` section —
  surfacing through `scheduler.py`'s handler as "paper buy failed", a risk
  misconfiguration diagnosed as a buy failure. It now fails closed and names
  the missing key.
- Every migration header said `psql "$POSTGRES_DB" -f ...`, and `POSTGRES_DB`
  is unset in this project's `.env`. That expands to an empty database name
  and psql falls back to `$USER` — so a migration could have applied cleanly
  to the wrong database and reported success.

## [1.2] — v1.2.0 — 2026-07-25 — SUPERSEDED, never tagged; shipped in v1.3.0

### Decision function: unchanged — v1.0.x/v1.1.0 trade data remains poolable

A **minor** bump, not a patch. `scripts/classify_change.py` reports MINOR
because `server.py` is a behaviour path, and it is right: requiring auth on
eight routes and disabling dashboard caching change what you observe, even
though no scoring, sizing or exit logic moved. Shipping this as a patch would
have meant overriding the classifier on its first real disagreement.

Full note: [docs/releases/v1.2.0.md](docs/releases/v1.2.0.md).

### Security

- The last eight write routes now carry the `require_token` dependency —
  **15 of 15 guarded**. §4 made the argument that a dependency cannot be
  forgotten the way an inline check can, then applied it to seven routes and
  left eight that had never had a check to replace. `/api/cycle/run_now` was
  the one with teeth: with the §2 gates open it reaches the order path.
- `authFetch()` in the UI — the client-side mirror of `require_token`. One
  place that attaches the header, and one place that handles a 403 (clear the
  cached token and re-prompt) and a 429 (report the lockout as a lockout).
- `saveConfig(update, needsAuth=false)` → `needsAuth=true`. All twelve call
  sites already pass `true`, so no behaviour changes; a safety parameter
  should not default to off.

### Fixed

Three pre-existing call sites that misreported an auth failure:

- `/api/ticker/validate` let the error body fall through to `!data.valid` and
  told you **the ticker was invalid** — sending you to debug the wrong thing.
- `/api/alerts/{id}/resolve` discarded the response, so a rejected request
  still toasted "Alert dismissed" and removed the row while the alert stayed
  open in the database.
- `/api/prompt/copy` stacked a generic "Copy failed" on top of the real error.

### Changed

- `scripts/verify_phase1.py` treats an unguarded write route as a **FAIL**
  rather than an accepted warning.
- `tests/test_ui_auth.py` asserts "no write route lacks the dependency"
  instead of checking a fixed allow-list — a list has to be remembered, which
  is the failure mode being designed out.

## [1.1] — v1.1.0 — 2026-07-25

### Decision function: unchanged — v1.0.x trade data remains poolable

`scripts/classify_change.py` reports MAJOR because
`engine/stop_state_machine.py` is on its file list. All four consumers of
`stop_state` were enumerated and none can change a trade; the stop *price* is
untouched. The reasoning is set out in full in
[docs/releases/v1.1.0.md](docs/releases/v1.1.0.md) — that argument, not the
file path, is what the field records.

### Fixed

- **S-1** — a stop stage a trade has reached no longer reverts. `calculate()`
  re-derived its stage from the current `profit_r` every cycle with no memory,
  so a pullback below `breakeven_r` flipped a breakeven-protected position back
  to `INITIAL_RISK` while `should_advance()` correctly held the stop price
  where it was. State and price then described different positions. Found on
  AES (entry 14.8050, stop 14.8095, state `INITIAL_RISK`) by
  `scripts/audit_stops.py` on the day it shipped.

### Added

- `_calculate_raw()` / `_apply_stage_ratchet()` split in
  `engine/stop_state_machine.py`, so the ratchet is testable independently of
  the stage arithmetic it floors.
- `tests/test_stop_state_ratchet.py` — 13 tests, including a control that
  asserts the raw calculation *still* regresses. If that ever passes, the stage
  maths moved and the rest of the file stops proving anything.

## [1.01] — v1.0.1 — 2026-07-24

### Decision function: unchanged — v1.0.0 trade data remains poolable

Phase 1: contain the risk that can cost real money before Phase 2's controls
exist. Nothing here touches scoring, sizing, thresholds or exits. Full note:
[docs/releases/v1.0.1.md](docs/releases/v1.0.1.md).

### Security

- UI binds `127.0.0.1` by default; `TP_UI_HOST` overrides it with a loud
  warning. Use an SSH tunnel or Tailscale for remote access. (§4, E-3)
- Nine inline token checks replaced by one `require_token` dependency, using
  `hmac.compare_digest` and a per-client 5-failures/5-minutes lockout. A
  dependency cannot be forgotten on a new route the way an inline `if` can.
- The token resolves through `storage/secrets.py`, never `config.yaml`. This
  closes a hole opened in Phase 0: `server.py` reads the YAML unexpanded, so
  the expected token had become the literal `${UI_AUTH_TOKEN}`. **Generate a
  new token** — see the release note.

### Fixed

- SYNC/SEED positions (~$42,000 of imported real holdings) are quarantined from
  every automated exit path, at the query, decision and execution layers. Their
  existing stop machinery is preserved and disarmed by `migrations/002`.
  Manual `/api/real/sell` still works. (§5, R-5)
- Live execution now requires a passing validation receipt no older than 30
  days, in addition to the three original gates. No receipt exists yet — §23
  writes the first one in Phase 4 — so live execution is blocked by code rather
  than by intention. (§2, R-4)
- The Bayesian learning loop is frozen (`learning.bayesian_enabled: false`) and
  the minimum sample raised 10 → 150. All 23 closed patterns were produced
  under the stop bug removed 2026-07-20; a large sample of contaminated trades
  is worse than a small one, because it looks trustworthy. (§17, T-9)

### Added

- `storage/banner.py` — the resolved execution posture, derived from
  `live_trader`'s own gate functions and printed at startup by every entry
  point. It replaces five prose claims that had been false since 16 July and
  cannot drift the way they did. (§6)
- `pattern_database` rows carry `engine_version` and `config_fingerprint`, so
  the Phase 4 recalibration partitions its own data without anyone having to
  remember the date. (§17, `migrations/003`)
- `scripts/audit_stops.py` — zero-distance, missing and prematurely advanced
  stops on managed positions.
- Four test files, 47 new tests, each with explicit control cases: a guard test
  that passes because the harness is broken is worse than no test.

### Phase 0 — Foundation (in progress)

Nothing in this phase changes a trading decision. That is deliberate: it is the
instrument every later phase is measured with.

- Version control initialised; ignore rules committed before any code was
  staged, so no secret ever entered the history. (§34)
- All credentials moved out of the tree. `config.yaml` no longer contains the
  Robinhood account number or the UI auth token; both are `${VAR}` references
  resolved from `.env` (gitignored) by `config_loader`, which raises rather
  than defaulting a missing value to `''`. (§3, §34.3)
- gitleaks + a literal-secret check on `config.yaml` run as pre-commit hooks;
  a pre-push hook refuses to push a version tag with no release note. (§34.4, §36.3)
- `scripts/version.py`, `scripts/classify_change.py`, `scripts/release.sh`:
  cutting a release is one command, with the safety checks built in. (§35, §37)
- Every dependency pinned to `==`; the platform now refuses to start on the
  hand-rolled TA fallback unless the divergence is accepted deliberately. (§13)
- `storage/paths.py` moves runtime data out of the checkout via
  `TP_OUTPUT_DIR`, so several versions can run side by side. (§38.2)
- `scripts/tp`: install, run, promote and patch any tagged version, each with
  its own worktree, venv, database, port and data directory. Non-primary
  versions are forced into paper mode by an environment veto that config
  cannot override. (§38, §38.4)
- `tp secrets` / `tp backup` / `tp doctor` — including the first Postgres
  backup this system has ever had. (§39)

## [1.0] — v1.0.0 — 2026-07-24

### Baseline

First tracked commit: the tree as evaluated on 2026-07-24, before any
remediation work. It is the exact tree the evaluation report measured, so it is
committed as-is on purpose rather than tidied up first — every later version is
measured against it, which requires it to be reproducible, not presentable.

- 29,704 lines of Python across 98 modules
- 3-year backtest: 29,909 candidate-days scored, **0 trades generated**
- 29 closed paper round-trips: 20.7% win rate, −0.75% expectancy
- Live execution was **ARMED at TURBO** when this snapshot was taken

### KNOWN UNSAFE — do not run this tag with live execution enabled

`scripts/tp` makes old tags runnable, which is exactly why these are recorded
here rather than left to memory. The `TP_FORCE_PAPER` veto in §38.4 exists
because this tag's own `config.yaml` has all three live-execution gates open.

| ID | Defect |
|----|--------|
| R-1 | Paper trades never increment the daily counters, so `max_trades_per_day` and `max_daily_loss_usd` do not bind |
| R-2 | The daily-loss limit is fed a value that is always zero |
| R-3 | The automatic kill switch is never called, and crashes on a missing `Database.realized_pnl_today()` when it is |
| R-4 | Live execution armed with no validated edge |
| R-5 | Real SYNC holdings are reachable by every automated exit path |
| E-2 | 33 of 58 tests cannot run |
| E-3 | UI auth surface: weak token, no lockout |
| E-9 | `update_position_by_ticker()` is unscoped by book — a paper entry can write its stop onto a real holding of the same ticker |
| T-9 | The learning loop trains on this data |

Fixed progressively in later releases — see the entries above.
