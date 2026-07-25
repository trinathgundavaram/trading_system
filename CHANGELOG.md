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
