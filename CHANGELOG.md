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
- **§55** the stale-data circuit breaker now writes a `rejected_signals` row
  (`reject_stage = "data_quality"`) naming the defaulted indicators, so
  `data_quality.stale_indicator_veto_threshold` can be set from a week of
  evidence instead of swapped for another guess. Only this veto is
  instrumented — the others are decisions about the name; this one is a
  decision about our own data.
- `tests/test_exit_vocabulary.py`, `tests/test_risk_calibration.py`.

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

- `migrations/009`, `010` and `011` are written but **not applied**. 010 will
  fail while duplicate `trade_id`s remain; that failure is the gate on §49's
  purge, not a bug.
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
