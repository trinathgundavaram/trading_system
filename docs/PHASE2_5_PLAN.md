# Phase 2.5 — measurement integrity before recalibration (§48–§55)

Status: 2026-07-25. Sits between Phase 2 (`v1.3.1`, shipped) and Phase 3
(§19–§21, scoring recalibration, ships `v2.0.0`).

| Item | | Status |
|---|---|---|
| §48 | Rebase the paper purse | **code done**, operation pending — see the item |
| §49 | Extend the damage tooling to `mae_mfe_data` | **implemented** |
| §50 | A structured exit vocabulary | **implemented** (migration 009 unapplied) |
| §51 | Make the pattern↔excursion join safe | **implemented** (migration 010 unapplied) |
| §52 | Set the drawdown caps from the cleaned curve | **tooling implemented**, numbers pending |
| §53 | Fix the high-volatility unit mismatch | **implemented** (migration 011 unapplied) |
| §54 | Retire the dead risk surface | **implemented** |
| §55 | Calibrate the stale-indicator veto | **instrumentation implemented**, needs a week of data |

Implemented items are code-complete with tests and are **not yet released**.

Three things remain that code cannot do:

1. **§48's actual reset** needs the live database and is destructive. The code
   is ready (`reset_paper_account()` now clears the equity curve too, and
   `assess_test_damage.py` reports on `mae_mfe_data`); running it is yours.
2. **§52's numbers.** `scripts/calibrate_risk_caps.py` derives them from the
   curve, but the curve has to be clean first, so this waits on §48.
3. **§55's threshold.** The instrumentation writes a `rejected_signals` row per
   stale-data veto; a week of those is the input.

The three migrations are written but deliberately **not applied**: 010 will
fail until §49's purge has run, and that failure is the gate, not a bug.

`scripts/classify_change.py` reports **MAJOR** on this diff. Most of that is
the conservative heuristics — `engine/rules_catalog.py` sits in
`DECISION_PATHS` and its change here is description strings plus one added
`enforced_in` key, and migrations 009/010/011 add two nullable columns and
three indexes. `config.yaml` also changed, but only by removing two keys that
nothing read, and `config_fingerprint` is **unchanged at `cc9a149613427f56`**,
which is the check that matters for pooling.

**One real decision-function change is in here, and it should not be lost in
that paragraph:** §53 changes which quantity `engine/portfolio_risk.py` counts
as a high-volatility position. Portfolio risk sizes and can block entries, so
the same candidate can now receive a different size than it would have under
`v1.3.1`. The change makes the count *stricter* (the old proxy read low), so
expect marginally more size reduction around volatile names, not less.

Everything else is inert: nothing reads `exit_kind` or
`get_pattern_excursions()` on the decision path yet, and removing a config key
with no reader cannot change a decision.

**Decision function: CHANGED, by §53 alone.** Pattern rows remain poolable
individually; anything reasoning about position *sizing* across the boundary
has to account for it.

## Why this phase exists at all

Phase 3 is a recalibration: §19 re-derives the scoring function, §20 re-derives
every threshold from the new scale, §21 revives the position-sizing tiers. All
three take the recorded history as their input.

That input is not currently trustworthy, and not in the way the remediation
plan anticipated. §15 purged and reconciled `pattern_database`, `paper_trades`
and `positions`. It did not touch `mae_mfe_data`, and that table is where the
excursion statistics live. Recalibrating thresholds against a contaminated
measurement base produces numbers that look derived and are not, and — worse —
the contamination is of a kind that a reasonable join will silently amplify
rather than reject (§51).

So this phase has one job: make the measurement base honest, then set the caps
from it. It is a **gate on Phase 3**, not a parallel track. Nothing here changes
the decision function; §52 and §53 change *configuration values*, which is a
separate and declarable thing.

---

## Part 1 — adjudication of the 2026-07-25 external review

The review was written against an earlier snapshot. Roughly half of what it
recommends shipped in `v1.3.0`/`v1.3.1`. Recording the verdicts here so the
same recommendations do not get re-actioned next time the document resurfaces.

| # | Review recommendation | Verdict | Evidence |
|---|---|---|---|
| 1 | "Add an explicit `risk.max_intraday_drawdown_pct` in config plus a `check_max_intraday_drawdown` rule that reads `daily_stats.max_drawdown`" | **Already shipped** | `config.yaml risk.max_intraday_drawdown_pct: 2.0`; `rules/risk_rules.py::_intraday_drawdown_breach`, reached from `RiskEngine.check()`. §11, v1.3.0. |
| 2 | "trip either a kill-switch or 'no new entries for the day' when breached" | **Already shipped, and the review's either/or was resolved deliberately** | Two caps with two half-lives: intraday blocks entries and expires with the day; `max_running_drawdown_pct: 15.0` escalates to the kill switch. See `trip_kill_switch_if_needed`'s docstring. |
| 3 | "Separate per-position ATR bands from account-level intraday drawdown caps" | **Already shipped** | `position_sizing.volatility_atr_pct_bands` and `risk.max_intraday_drawdown_pct` are independent keys read by independent code paths. They were never the same control. |
| 4 | "verify `Database.realized_pnl_today()` exists" | **Already shipped** | `storage/database.py:3561`, book-aware via `simulated=`. The review is quoting a finding that §9 fixed. |
| 5 | "verify scheduler or live-trader paths call `trip_kill_switch_if_needed` … a kill switch that's defined but not wired breeds false confidence" | **Already shipped — three call sites** | `scheduler.py:1430` (per cycle), `engine/paper_trader.py:373` (after **every** paper close), `engine/live_trader.py:596` (after every real close). The per-close placement is the stronger version of what the review asks for: a stop cascade inside one 12-minute cycle no longer runs to completion unchecked. |
| 6 | "Theme exposure depends entirely on the static `theme_map`; consider a helper script to audit tickers without a theme" | **Superseded by a better fix** | §18 made `_themes_for()` fall back to `SECTOR:`/`INDUSTRY:` synthetic themes and bucket the remainder as `UNCLASSIFIED` under its own `max_unclassified_exposure_pct: 25` cap. Nothing is unmeasured any more, so the audit script has no gap to report. |
| 7 | "keep tests around `None` correlation so future changes don't treat missing history as low correlation" | **Already correct, test coverage worth confirming** | `get_pairwise_correlation` returns `None`; `portfolio_risk.py:333` `continue`s on it. Confirm `tests/test_portfolio_risk_binding.py` pins this — it is a one-line regression away from becoming `or 0.0`. |
| 8 | "Re-run the drawdown backfill … derive the cap from 95th/99th percentiles rather than a hand-picked 2%" | **Valid** → §52 | `scripts/backfill_drawdown.py` already prints the distribution for exactly this purpose. The cap is still the hand-picked number. |
| 9 | "Document the scale dependence — caps become materially binding as equity grows" | **Valid** → §52 | And there is a sharper version of this the review missed: see §52's note on the two 2.0% figures. |
| 10 | Paper purse: clean slate over reconcile | **Valid, and already half-decided** → §48 | `docs/reconcile_baseline.json` (2026-07-25) records the discrepancy as accepted and *not reconstructable* — 145 orphaned sells, cash 1000.0 against a ledger implying 83.65. The decision has effectively been taken; §48 executes it. |
| 11 | "Extend real trades to stamp structured `exit_reason` (stop/target/manual/time)" | **Valid, and more urgent than stated** → §50 | Current vocabulary is free text with prices interpolated into it: `paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $82.56 <= stop $83.15`. Every such string is unique, so the column cannot be grouped, counted or filtered at all. |
| 12 | "Introduce a trade-id linkage between `mae_mfe_data` rows and their originating patterns" | **Valid, but the framing is wrong and the risk is inverted** → §51 | A linkage already exists transitively (`pattern_database.id ← positions.pattern_id`, `positions.id → mae_mfe_data.trade_id`). The problem is not that it is absent. It is that using it today returns wrong answers — see §51. |
| 13 | "update `ev_engine` to use MAE-derived intraday drawdown instead of horizon proxies" | **Valid, blocked** → Phase 3, after §49/§50/§51 | The `HONESTY NOTE` at the top of `engine/ev_engine.py` already states the limitation accurately. Do not lift it until the excursion table is clean. |
| 14 | "calibrate `high_vol_atr_pct_threshold` … so it matches reality, not intuition" | **Valid, and there is a unit bug underneath it** → §53 | Calibrating the threshold will not help while the two sides of the comparison are in different units. |
| 15 | "calibrate `data_quality.stale_indicator_veto_threshold` based on actual behaviour" | **Valid, needs instrumentation first** → §55 | Nothing currently counts how often the veto fires or how many indicators were defaulted when it did. |
| 16 | "keep `assess_test_damage.py` as a required step before any reset" | **Valid, and the script has a blind spot** → §49 | It covers `paper_account`, `paper_trades`, `positions`, `pattern_database`. It does not cover `mae_mfe_data`, which is where the surviving contamination is. |
| 17 | "never expose reset from the UI without a big explicit confirmation" | **Already true** | No UI route calls `reset_paper_account()`. Worth a test that pins the absence. |

**Summary.** Of 17 recommendations: 7 already shipped, 1 superseded by a
stronger fix, 9 valid and carried into §48–§55 below. The review's overall
judgement — "mis-calibrated caps and occasional partial implementation rather
than fundamental design flaws" — holds up. Its specific worked examples are
mostly stale.

---

## Part 2 — findings the review did not reach

These came out of reading the code and querying the July-21 SQLite snapshot
(`output/trading.db`). **Every data figure below must be re-run against the
live Postgres database before acting** — the snapshot predates the Postgres
cutover and the 2026-07-25 incident cleanup. The *code* findings are
independent of which database is in front of you.

### F1. `mae_mfe_data` still contains test-suite residue

Of 25 rows, 22 carry the test-fixture tickers `AAA`, `FIX`, `MU`, `NVDA`,
`ORCL` — the same list `scripts/assess_test_damage.py:47` defines as
`TEST_TICKERS` — with `mae_pct = mfe_pct = 0.0`. Three rows look real
(`USB`/23, `SHEL`/31, `BMY`/10).

`repair_test_damage.py`'s `PURGE` list covers `paper_trades`, `positions` and
`pattern_database`. `mae_mfe_data` is not in it, so the 2026-07-25 cleanup
walked past it. This is the table Phase 3 wants to recalibrate against.

### F2. `mae_mfe_data.trade_id` collides, so the obvious join fans out

`trade_id` is `TEXT`, holds a stringified `positions.id`, and has no unique
constraint, no foreign key and no book scope. In the snapshot, `trade_id = '1'`
appears 15 times across 5 different tickers.

Joining `pattern_database → positions → mae_mfe_data` on it returns **37 rows
for 23 closed patterns**, and the surplus is not duplication of the same trade —
it is `NVDA`'s excursion row attaching itself to `ADPT`'s pattern. A naive
`AVG(mae_pct)` over that join returns a number that is wrong in a way nothing
about the query looks wrong.

This is the exact hazard the review named in a different context: a partial
implementation is worse than none, because it invites use.

### F3. `exit_reason` is unqueryable free text

Closed-pattern vocabulary in the snapshot:

```
paper_price_watch:stop_loss                                            14
paper_sell_rules:Earnings in 0 days                                     2
paper_price_watch:trailing_stop                                         2
paper_sell_rules:Earnings in 1 days                                     1
paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $82.56 <= …     1
paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $22.13 <= …     1
paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $19.00 <= …     1
paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $18.38 <= …     1
```

Four stop-loss exits recorded under four distinct strings because the price is
interpolated into the reason. `analytics/regret_analysis.py:42`'s
`_classify_regret` already keyword-matches on this column and its module
docstring says so plainly ("under-fed by today's `exit_reason` vocabulary").
`ev_engine`'s `p_stop_loss` cannot filter on it at all.

### F4. The high-volatility count compares two different quantities

`engine/portfolio_risk.py`, section 5:

- candidate side: `candidate_atr_pct`, computed at `scheduler.py:894` as
  `atr / price * 100` — a true ATR percentage.
- existing-positions side: `_position_risk_band_pct(p)` — stop distance from
  entry as a percentage of entry.

Both are compared against the same `high_vol_atr_pct_threshold: 5.0`. For a
5%-ATR name the initial stop typically sits well inside 5% of entry, so
existing positions are systematically under-counted as high-vol and
`max_simultaneous_high_vol_positions: 4` is looser in practice than it reads.

The proxy's docstring defends itself honestly ("a genuine, if indirect, proxy —
not a fabricated placeholder") and that defence is fine for *ranking*. It does
not hold for comparing against a threshold expressed in ATR units.

### F5. Two vetoes exist that nothing can trigger

`risk.daily_loss_limit_triggered` and `risk.daily_profit_lock_triggered` are
read in three places — `rules/hard_vetoes.py:113,117` (as vetoes `DAILY_LOSS`
and `PROFIT_LOCK`) and `engine/position_management.py:273` (as a
**priority-1 exit-everything** trigger) — and written by nothing. No code path
sets either flag. `engine/rules_catalog.py:264` advertises `DAILY_LOSS` to the
operator as a live veto.

This is §9's original finding recurring one layer over: a control that is
documented, catalogued and readable, and that cannot fire. It is not dangerous
in the way an un-wired kill switch was — nothing depends on it holding — but it
is false confidence, and `position_management.py`'s priority-1 handler means a
hand-edit of the flag would liquidate the book, which is a lot of consequence
for a key with no writer.

### F6. Six risk helpers and a second risk engine have no callers

`check_kill_switch`, `check_max_trades_per_day`, `check_max_daily_loss`,
`check_buying_power`, `check_position_limits`, `check_position_size_limit` and
class `LegacyRiskEngine` in `rules/risk_rules.py`: zero call sites outside the
file, including in tests.

The limits they express *are* enforced — `max_position_size_usd` at
`engine/position_sizing.py:132` and `engine/live_trader.py:407`,
`max_positions` at `engine/paper_trader.py:163` and
`engine/live_trader.py:378` — but by separate code. So the file now holds two
implementations of the same limits, one live and one dead, and
`ACCOUNT_RISK_CATALOG` attributes all of them to `rules/risk_rules.py`, which
is where the dead one is. The next person to change a limit has a 50% chance of
changing the wrong one.

`LegacyRiskEngine` additionally uses `>=` where `RiskEngine` uses `>`, and a
raw `max_daily_loss_usd` where the live path uses §8's tighter
`daily_loss_limit()`. Two answers to the same question, already diverged.

### F7. Catalogue/config drift on the drawdown default

`engine/rules_catalog.py:378` documents `max_intraday_drawdown_pct` as
"default 3.0%". `config.yaml` says `2.0`. The catalogue is the operator-facing
description of the system's own rules; it should not be able to drift.

---

## Part 3 — the work items

Each item states the files, the acceptance test, and whether it moves the
decision function (which determines whether trade history pools across the
change — see `CHANGELOG.md`).

---

### §48 — Rebase the paper purse from a known-good baseline · CRITICAL · 1 hour — CODE DONE, OPERATION PENDING

The code change described under "New work" below is done:
`reset_paper_account()` now clears `paper_equity_history` in the same
transaction, so a reset genuinely starts from nothing. `_paper_epoch_start()`
and `update_drawdown()`'s comments were updated — they claimed the curve
survives a reset, which was true when written and is now only true of a
*re-seed*. Both keep their guards, which remain load-bearing for that case.

**The reset itself is yours to run**, on the live database, after
`scripts/assess_test_damage.py` and `scripts/tp backup`. It is destructive and
irreversible; nothing in this session touched production data.


**Problem.** `paper_account.cash` disagrees with the `paper_trades` ledger; 145
paper sells have no matching closed position. `docs/reconcile_baseline.json`
records this as accepted and not reconstructable.

**Decision: clean slate**, per the review and for the reason it gives — the
learning path is frozen (`bayesian_enabled: false`,
`min_pattern_recorded_at: 2026-07-25`), so the paper history being discarded is
not training anything, and `reset_paper_account()` leaves `pattern_database`
intact. Carrying a known-inconsistent purse into the next 20 sessions poisons
every accounting and performance figure derived from it.

**Steps.**

1. `python3 scripts/assess_test_damage.py` — required, and read the output.
   Extend it first per §49 so the report covers `mae_mfe_data`.
2. `scripts/tp backup` before anything destructive.
3. `db.reset_paper_account()` then `db.init_paper_account(starting_cash)`.
4. Re-run `python3 scripts/backfill_drawdown.py` — the equity curve is now
   epoch-bounded by a single `created_at`, which is the condition v1.3.1's fix
   assumes.
5. Append to `docs/reconcile_baseline.json`: the reset timestamp, the new
   `starting_cash`, and the row counts destroyed.

**New work.** `storage/database.py::reset_paper_account` should also clear
`paper_equity_history`. It currently does not, and v1.3.1's epoch logic exists
specifically to cope with curve points that predate the current account. Once
the reset drops them, the epoch guard becomes belt-and-braces rather than
load-bearing — keep the guard, it is correct for a re-seed that is not a reset.

**Acceptance.** `scripts/reconcile.py` reports zero discrepancies immediately
after. `tests/test_paper_trading.py` gains a case asserting
`reset_paper_account()` leaves zero rows in `paper_equity_history` and a
non-zero count in `pattern_database`.

**Decision function: unchanged.**

---

### §49 — Extend the damage tooling to `mae_mfe_data` · CRITICAL · 2 hours

**Problem.** F1 — the excursion table was never cleaned, and the tooling that
exists to find such things does not look at it.

**Changes.**

- `scripts/assess_test_damage.py`: add `("mae_mfe_data", "recorded_at")` to the
  timestamp-window scan, and add a section reporting rows whose `ticker` is in
  `TEST_TICKERS`, rows with `mae_pct = 0 AND mfe_pct = 0` (a real excursion is
  never exactly zero on both sides), and rows whose `trade_id` does not resolve
  to a `positions` row with a matching `ticker`.
- `scripts/repair_test_damage.py`: add `mae_mfe_data` to `PURGE` with the same
  window semantics, plus the ticker-mismatch predicate — a row whose ticker
  disagrees with the position it claims is not repairable, only deletable.

**Acceptance.** A new `tests/test_test_damage_tooling.py` seeds one synthetic
contaminated row and one clean row, runs the assess pass, and asserts the
contaminated row is reported and the clean one is not. The repair pass in
`--dry-run` reports exactly one deletion.

**Decision function: unchanged.**

---

### §50 — A structured exit vocabulary · HIGH · 3 hours

**Problem.** F3 — `exit_reason` is free text and cannot be grouped.

**Design.** Add a column rather than constrain the existing one. The
human-readable string is genuinely useful in the UI and in `regret_analysis`'s
narrative output; the fix is to stop asking it to be two things at once.

`migrations/009_exit_kind.sql`:

```sql
ALTER TABLE pattern_database ADD COLUMN IF NOT EXISTS exit_kind TEXT;
CREATE INDEX IF NOT EXISTS idx_pattern_exit_kind ON pattern_database (exit_kind);
```

Vocabulary (closed set, in `rules/common.py` so both traders and the analytics
modules import the same constant):

| `exit_kind` | Meaning |
|---|---|
| `stop_loss` | initial-risk or dynamic stop hit |
| `trailing_stop` | trailing stop hit after the position moved in favour |
| `take_profit` | target reached |
| `time_stop` | horizon/`pattern_hold_days` expiry, incl. scheduler's time-based close |
| `eod_flatten` | DAY position closed at the session cutoff |
| `rule_exit` | a `sell_rules` signal that is none of the above (earnings, thesis break) |
| `manual` | `confirm_fill.py` human-confirmed sell |
| `rotation` | closed to make room for a higher-conviction candidate |

**No backfill.** The 23 existing closed rows would have to be reverse-engineered
from strings, and `paper_sell_rules:Earnings in 0 days` maps to `rule_exit`
only by inference. Leave them NULL, which reads as "not recorded", and let
every consumer filter `exit_kind IS NOT NULL`. §49's precedent and §008's
docstring both make this argument already.

**Threading.** `close_trade(pattern_id, outcome_pct, hold_hours, exit_reason,
exit_kind)` → `close_pattern(...)`. Call sites: `engine/paper_trader.py:323`,
`engine/live_trader.py:583`, `confirm_fill.py:264`, `scheduler.py:1572`. The
traders receive `reason` from `rules/sell_rules.py` and
`engine/stop_state_machine.py`; classify at the point where the *structured*
information still exists, not by re-parsing the string downstream.

**Acceptance.** `tests/test_paper_trading.py` asserts a stop-loss paper close
writes `exit_kind = 'stop_loss'` and an unchanged `exit_reason`. A test asserts
every value written is a member of the constant set.

**Decision function: unchanged** — nothing reads `exit_kind` yet. Lifting
`ev_engine`'s `p_stop_loss` onto it is a Phase 3 change and *will* move the
decision function.

---

### §51 — Make the pattern↔excursion join safe · HIGH · 3 hours

**Problem.** F2 — the join fans out and mixes tickers. `pattern_database.trade_id`
exists and is NULL on every row, so the direct path is dead and the transitive
path is unsafe.

**Changes.**

1. `storage/database.py`: `link_pattern_to_trade(pattern_id, position_id)`,
   called from `engine/paper_trader.py` and `engine/live_trader.py` immediately
   after `try_open_position` / `open_position` returns an id. This is the only
   moment where both ids are in scope — `record_entry` runs at *signal* time,
   before any position exists, which is why the column has always been NULL.
2. `migrations/010_mae_mfe_integrity.sql`:
   - `CREATE UNIQUE INDEX ... ON mae_mfe_data (trade_id)` — one excursion record
     per trade. Guard: this will fail on the current data, which is the point;
     run §49's purge first and let the index failure be the check that it worked.
   - `CREATE INDEX ... ON pattern_database (trade_id)`.
3. `storage/database.py`: `get_pattern_excursions(...)` as the *single*
   sanctioned join, matching on `pattern_database.trade_id` directly and
   asserting ticker agreement. Everything downstream goes through it rather
   than hand-rolling the three-table join.

**Acceptance.** `tests/test_learning_data_quality.py` seeds two patterns on
different tickers whose positions collide on a stringified id and asserts
`get_pattern_excursions` returns each pattern's own excursion and does not
fan out. A test asserts a paper buy leaves `pattern_database.trade_id`
non-NULL.

**Decision function: unchanged.**

---

### §52 — Set the drawdown caps from the cleaned curve · HIGH · 2 hours — TOOLING IMPLEMENTED

`scripts/calibrate_risk_caps.py` writes nothing and answers the question this
section asks: given the distribution, what should the caps be. It reports
percentiles for both caps, refuses to recommend a number below `--min-days`
(a percentile of four observations is arithmetic, not evidence), flags any day
showing ≥10% intraday drawdown as far more likely to be a re-seed than a
trading loss, prints the `max_daily_loss_pct` interaction described below, and
converts the cap into dollars at current equity so the scale dependence is
visible rather than inferred.

**The numbers still need you**, and they need §48 first.


**Problem.** `max_intraday_drawdown_pct: 2.0` is a hand-picked number. The
observed distribution (median ~0.17%, worst ~1.25%) says it has never bound.

**Do this after §48**, not before — the current curve spans a re-seed and two
accounting events, and v1.3.1 exists because of what that did to the arithmetic.

**Method.** `python3 scripts/backfill_drawdown.py` already prints the
distribution for exactly this decision. Set `max_intraday_drawdown_pct` at
roughly the 99th percentile of clean observations, floored at a value that is
material against the account — a cap below the median halts most days, one
above every observation is documentation.

**And resolve the interaction the review missed.** `risk.max_daily_loss_pct` is
also `2.0`. Intraday peak-to-trough drawdown includes unrealised P&L and is
therefore *always ≥* the realised daily loss for the same session. Two controls
set to the same 2.0% means the intraday gate fires first in essentially every
scenario, and the realised daily-loss limit — the one §8 was written to give a
real input, and the one that escalates to the kill switch — becomes close to
unreachable. Pick two numbers that mean two different things, and record the
gap between them as deliberate.

**Also.** `max_running_drawdown_pct: 15.0` has never been evaluated against a
real curve, because until §48 there was no clean one.

**Acceptance.** A `# calibrated <date> from N observations, p99 = X%` comment on
each cap in `config.yaml`, and the README risk section states the scale
dependence in the review's own terms: at current book size these caps rarely
approach binding, and become materially binding as equity grows.

**Decision function: CHANGED** (config values that gate entries). Declare it,
bump accordingly, and note that trade history either side of the change is not
poolable if either cap actually bound.

---

### §53 — Fix the high-volatility unit mismatch · MEDIUM · 3 hours — IMPLEMENTED

Shipped as specified: `migrations/011` adds `positions.entry_atr_pct`,
`scheduler.py` and `confirm_fill.py` populate it from the ATR already in scope,
and `engine/portfolio_risk.py` counts through a new `_position_atr_pct()` with
the stop-distance proxy kept as an explicit, logged fallback.

One thing the original spec understated. The proxy is not merely *indirect*, it
is biased in a specific direction, twice over. `risk_per_share` is
`min(max(1.2*ATR, price*1.5%), price*stop_loss_pct)` — so past a certain
volatility the stop is clamped by `stop_loss_swing_pct` while ATR keeps going,
and the proxy saturates. And the stop *ratchets* as a position moves in favour,
so `|entry - stop|` shrinks: a position's measured volatility fell the better
it did, and a winner quietly stopped counting toward the cap. Both are now
pinned by tests.

**The recalibration of `high_vol_atr_pct_threshold` is still outstanding** and
is now worth doing, which it was not before — a threshold tuned against
stop-distance would have been tuned against the wrong axis.


**Problem.** F4 — ATR% on one side of the comparison, stop-distance% on the
other.

**Preferred fix.** Persist ATR at entry. `positions` already carries
`entry_signal_score`, `entry_p_win`, `entry_ev`, `entry_regime`,
`entry_rs_percentile`, `entry_ad_ratio` — the entry-context pattern is
established and §16 fixed its persistence. Add `entry_atr_pct` alongside them
(`migrations/011`), populate from the same `ticker_dict["atr"] / price * 100`
the candidate side already computes, and have `_position_risk_band_pct` become
`_position_atr_pct` reading the real column with the stop-distance proxy as an
explicit fallback for rows predating the migration.

**Then** recalibrate `high_vol_atr_pct_threshold` against the observed ATR
distribution of names actually traded, per the review's item 14 — which becomes
a meaningful exercise only once both sides are in the same units.

**Acceptance.** `tests/test_portfolio_risk_binding.py` asserts a position with
`entry_atr_pct = 6.0` and a 2%-wide stop counts as high-vol (it does not
today), and that a pre-migration row with NULL `entry_atr_pct` still falls back
to the proxy rather than counting as zero.

**Decision function: CHANGED** (portfolio risk sizes and blocks entries).

---

### §54 — Retire the dead risk surface · MEDIUM · 2 hours — IMPLEMENTED

All three steps. The seven dead symbols are gone from `rules/risk_rules.py`,
with the module docstring recording what they were and the two ways the dead
copy had already diverged. `ACCOUNT_RISK_CATALOG` carries an `enforced_in`
field per check, and the 3.0%/2.0% drawdown drift is corrected — in
`scripts/backfill_drawdown.py` too, which was printing the same wrong literal
in the place an operator is most likely to read it as authoritative.

**On step 2**, the writerless flags: removed rather than promoted. The judgement
is that the capability was never actually lost — `kill_switch_triggered` is the
documented manual halt, it has a writer, a persist step, a notification and a
test, and it reaches the same priority-1 exit branch in
`engine/position_management.py` that `daily_loss_limit_triggered` did. Keeping
a second, undocumented path to a full liquidation, gated on a key nothing
writes, bought nothing.

If you do want a daily profit-lock — there is no equivalent for that one — build
it as a real control rather than reinstating the key.


**Problem.** F5, F6, F7 — dead helpers, unwritable flags, drifted catalogue.

**Changes.**

1. Delete `check_kill_switch`, `check_max_trades_per_day`,
   `check_max_daily_loss`, `check_buying_power`, `check_position_limits`,
   `check_position_size_limit` and `LegacyRiskEngine` from
   `rules/risk_rules.py`. Zero callers; git preserves them if the legacy
   `engine/executor.py` path is ever revived, and reviving it against a
   diverged copy of the limits would be worse than rewriting it.
2. `rules/hard_vetoes.py` and `engine/position_management.py`: remove the
   `daily_loss_limit_triggered` / `daily_profit_lock_triggered` branches, and
   remove the keys from `config.yaml`. The daily-loss control lives in
   `RiskEngine.check()` and `trip_kill_switch_if_needed`; a second, manual,
   hand-edited path to a priority-1 liquidation is a hazard, not a feature.
   *If* a manual "flatten everything" control is wanted, it should be one
   deliberate thing with a writer, a test and a UI confirmation — not a config
   key that reads like a limit.
3. `engine/rules_catalog.py`: drop the `DAILY_LOSS` / `PROFIT_LOCK` veto
   entries; re-point `max_positions`, `max_position_size` and
   `sufficient_buying_power` at the files that actually enforce them; correct
   the `max_intraday_drawdown_pct` default from 3.0% to 2.0%.

**Acceptance.** A test asserts every default named in `ACCOUNT_RISK_CATALOG`
matches the value in `config.yaml` — the catalogue should not be able to drift
again. `scripts/verify_phase2.py` still passes.

**Decision function: unchanged** — no removed code has a caller, and no removed
config key has a writer.

---

### §55 — Calibrate the stale-indicator veto · LOW · 2 hours — INSTRUMENTATION IMPLEMENTED

`scheduler.py` now writes a `rejected_signals` row with
`reject_stage = "data_quality"` whenever `STALE_DATA_CIRCUIT_BREAKER` fires.
The reason string already names the offending indicators and the row carries a
UTC timestamp, so one week answers both halves: which indicators default, and
whether this is overwhelmingly a first-N-minutes effect. Only this veto is
instrumented — the others are decisions about the *name*, this one is a
decision about our own *data*, and it is the only one whose threshold is up for
calibration.

    SELECT substr(timestamp, 12, 2) AS utc_hour, COUNT(*)
      FROM rejected_signals WHERE reject_stage = 'data_quality'
     GROUP BY 1 ORDER BY 1;


**Problem.** `data_quality.stale_indicator_veto_threshold: 3` (of 5 indicators)
was chosen a priori. Nothing counts how often it fires.

**Instrument first.** `rules/hard_vetoes.py`'s
`STALE_DATA_CIRCUIT_BREAKER` path should write a `rejected_signals` row (§18
added the table and `reject_stage`) carrying the defaulted-indicator count and
*which* indicators defaulted. One week of that data answers the question; a
threshold changed without it is a swap of one guess for another.

**Expect a time-of-day signal.** The catalogue already notes this fires most
around the open, when VWAP has no intraday bars yet. If the data confirms it is
almost entirely a first-N-minutes effect, the right fix is a time-aware
threshold or a longer `market_open_buffer_minutes`, not a looser cap.

**Acceptance.** A one-week report; then either a changed threshold with a
calibration comment, or a recorded decision that 3 is right and why.

**Decision function: unchanged** by the instrumentation. Changing the threshold
afterwards does change it.

---

## Part 4 — sequencing

```
§49  extend damage tooling ─┐
                            ├─→ §48  rebase the purse ─→ §52  set the caps ─┐
§50  exit vocabulary  ──────┤                                                │
§51  safe join        ──────┘                                                ├─→ Phase 3
§53  ATR units ──────────────────────────────────────────────────────────────┤   (§19–§21)
§54  retire dead surface (independent, any time) ─────────────────────────────┤
§55  stale-veto instrumentation → 1 week of data → decision ─────────────────┘
```

§49 comes before §48 because the assess step is the gate on the reset, and it
currently cannot see the table with the surviving contamination.

§50, §51 and §54 are independent of the data work and can ship immediately.
They are additive and move no decision.

§52 and §53 change values the entry path reads, so they ship together as one
declared decision-function change with its own release note.

**Suggested releases.** `v1.4.0`: §49, §50, §51, §54 — decision function
unchanged, `v1.3.x` trade data remains poolable. `v1.5.0`: §48, §52, §53 —
decision function changed, new measurement epoch, re-validation required before
arming live (§32).
