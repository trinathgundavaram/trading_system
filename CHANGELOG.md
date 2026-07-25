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

## [1.2] — v1.2.0 — 2026-07-25

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
