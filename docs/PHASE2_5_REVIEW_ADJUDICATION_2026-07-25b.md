# Adjudication of the second external review (2026-07-25)

**Verdict: the review is reading a stale commit.** It states it is reviewing
`origin/main` at `5865050 ... v1.2.0`. Actual `main` is:

```
4a166dd tests: fix two defects in the Phase 2.5 tests, found by running them
94030ed CHANGELOG: Phase 2.5 under Unreleased
b08bced Phase 2.5 (§48-§55): make the measurement base honest before Phase 3
41c8e80 docs: Phase 2.5 plan, and adjudicate the 2026-07-25 review
115c172 reconcile: say RECORD, not FAIL, while recording a baseline
5865050 v1.2.0                                   <- what the review read
```

Every one of the seven code-level findings was implemented in `b08bced`, and
in most cases the implementation goes further than the review asked. The
review's own "reality check" is therefore describing the repo three commits
ago, and its sequencing advice — which is sound — is advice we already took.

What follows is (A) claim-by-claim verification, (B) the work that genuinely
remains, and (C) three findings in the review that Phase 2.5 did **not**
address and which are still open.

---

## A. Claim-by-claim verification

| Review claim | Status | Evidence |
|---|---|---|
| No `009_exit_kind`, `010_mae_mfe_integrity`, `011_entry_atr_pct` | **Stale** | All three exist in `migrations/` |
| `grep exit_kind` returns nothing | **Stale** | 30+ hits: `storage/database.py:389,3829,3881-3913`, `rules/common.py:19-70`, `scheduler.py:1604-1611`, `learning/pattern_database.py:161`, `tests/test_exit_vocabulary.py` |
| No `calibrate_risk_caps.py` | **Stale** | `scripts/calibrate_risk_caps.py` exists (§52) |
| `portfolio_risk.py` still uses `_position_risk_band_pct` as high-vol proxy | **Stale** | Line 417 calls `_position_atr_pct()`. `_position_risk_band_pct` survives at line 151 as an **explicit, debug-logged fallback** for pre-`011` rows only |
| Damage tooling doesn't mention `mae_mfe_data` | **Stale** | `assess_test_damage.py:92,146-226`; `repair_test_damage.py:83-93,132,185-264` |
| `reset_paper_account()` doesn't clear `paper_equity_history` | **Stale** | `storage/database.py:2987` — `DELETE FROM paper_equity_history` |
| `daily_loss_limit_triggered` / `daily_profit_lock_triggered` are writerless live vetoes | **Stale** | Removed from `config.yaml` (tombstone comment at :151), `rules/hard_vetoes.py:114`, `engine/position_management.py:273`, `engine/rules_catalog.py:264-265`. Tests at `tests/test_risk_calibration.py:214-227` assert they cannot be reintroduced |
| Stale-indicator veto not instrumented into `rejected_signals` | **Stale** | `scheduler.py:772-773` writes `reject_stage='data_quality'` when the veto fires |
| Test for catalogue-vs-config drift | **Implemented** | `tests/test_exit_vocabulary.py:288-307` pins `ACCOUNT_RISK_CATALOG` to `config.yaml` |

Where the implementation went further than the review asked:

- **§51 `get_pattern_excursions()`** carries three defences, not one: direct
  indexed hop on `pattern_database.trade_id`, mandatory ticker agreement, and a
  redundant in-Python dedupe so a database restored *without* `010`'s unique
  index degrades to a logged warning rather than to wrong averages. It also
  honours §15's quarantine on **both** sides, so it cannot disagree with
  `get_recent_mae_mfe()` about which population it is reporting.
- **§50 `classify_exit()`** refuses to classify `sell_rules:` prose rather than
  prefix-matching "Dynamic stop hit", and `close_pattern()` **rejects** a value
  outside `EXIT_KINDS` rather than storing it.
- **§53** persists `entry_atr_pct` from both entry paths (`scheduler.py:1067`
  and `confirm_fill.py:154`), not just one.
- **§54** additionally removed six dead `check_*` helpers and
  `LegacyRiskEngine` from `rules/risk_rules.py` — a second implementation of
  the same limits that had already silently diverged from the live one (`>=`
  vs `>` on daily trade count; raw `max_daily_loss_usd` vs §8's equity-scaled
  `daily_loss_limit()`).

Test suite: **201 passed, 93 skipped, 1 failed.** The single failure
(`tests/test_live_trader.py::test_confirm_phrase_required_to_enable`) is
`ModuleNotFoundError: fastapi` in a bare sandbox, not a defect. The 93 skips
are the Postgres-gated tests — see item B7.

---

## B. What actually remains — the operational tail

Phase 2.5 is complete as far as code can take it. Everything below needs the
live database or elapsed time, and **the ordering matters** — it is the
review's own sequencing, which is correct.

### B1. Run `scripts/assess_test_damage.py`, then back up
**Blocking everything else.** Establishes the current contamination counts
against live Postgres rather than the pre-cutover snapshot. Take
`scripts/tp backup` immediately after and before any destructive step.

### B2. Run §49's purge (`scripts/repair_test_damage.py`)
`mae_mfe_data` is cleaned **by evidence, not by time window** — the table has
no provenance columns, so the window filter that works for every neighbouring
table cannot work here. Three predicates: `TEST_TICKERS` membership,
`mae_pct = 0 AND mfe_pct = 0`, and `trade_id` that does not resolve to a
`positions` row of the same ticker. On the pre-Postgres snapshot this was 22 of
25 rows.

### B3. Apply `migrations/009`, `010`, `011`
Use `./scripts/apply_migration.sh`, **not** `psql "$POSTGRES_DB" -f` —
`POSTGRES_DB` is unset in this project's `.env`, so that expands to an empty
database name and psql silently falls back to `$USER`.

**`010` will fail while duplicate `trade_id`s remain, and that failure is the
gate on B2, not a bug.** If it errors on the unique index, go finish the purge;
do not drop the constraint.

### B4. Run §48's reset and re-baseline
`reset_paper_account()` → `init_paper_account(starting_cash)` →
`scripts/backfill_drawdown.py`, against Postgres. Destructive; B1's backup is
the prerequisite. This is what makes the equity curve a single epoch, which
every drawdown figure depends on.

### B5. Run `scripts/calibrate_risk_caps.py` and set the caps
Writes nothing — it recommends. `max_intraday_drawdown_pct` is still **2.0**,
uncalibrated. The script refuses to recommend below `--min-days` (a percentile
of four observations is arithmetic, not evidence) and flags any day showing
≥10% intraday drawdown as far more likely to be a purse re-seed than a trading
loss. **If such a day appears, the curve is not clean and B4 did not take.**

### B6. Recalibrate `high_vol_atr_pct_threshold`
Still **5.0**, unchanged. Now worth doing — before §53 it would have been
tuning against the wrong axis, because the two sides of the comparison were in
different units. Derive it from the actual ATR distribution of tickers you
trade, with at least one book turnover so `entry_atr_pct` is populated and the
proxy fallback is no longer being hit. Check the logs for
`"has no entry_atr_pct"` warnings to know when that is true.

### B7. Run the full suite against Postgres
93 tests skip without it, including the whole of `test_portfolio_risk_binding`
and `test_learning_data_quality`. These have never been executed against the
Phase 2.5 changes. Do this before B8.

### B8. Release — and re-validate before arming live
**The decision function CHANGED.** §53 alters which quantity
`engine/portfolio_risk.py` counts as an open high-volatility position, and
portfolio risk both sizes and blocks entries. The count becomes *stricter*
(the old proxy read low, in one direction, twice over), so expect marginally
more size reduction around volatile names.

`config_fingerprint` is unchanged at `cc9a149613427f56`, so pattern rows remain
individually poolable — but anything reasoning about **position sizing** across
this boundary has to account for it. `scripts/classify_change.py` reports MAJOR;
most of that is conservative heuristics, but do not let §53 get lost in that
paragraph.

### B9. §55 — one week of data, *then* decide
`data_quality.stale_indicator_veto_threshold` is still **3**. The
instrumentation is live but the sample does not exist yet. After a week:

```sql
SELECT substr(timestamp, 12, 2) AS utc_hour, COUNT(*)
  FROM rejected_signals
 WHERE reject_stage = 'data_quality'
 GROUP BY 1 ORDER BY 1;
```

Expect a concentration in the first minutes after the open — VWAP needs
intraday bars that have not accumulated yet. If the distribution is *only* that
spike, the threshold is fine and the right fix is a warm-up window, not a
looser threshold.

---

## C. Genuinely open — raised by the review, not addressed by Phase 2.5

These three are real and should be tracked. None is urgent; all are cheap.

### C1. `mae_mfe_data.id` is still `TEXT PRIMARY KEY`, and there is no FK
`storage/database.py:476-487`. The review is right that for a purely internal
analytics table this is brittle. `migrations/010` added the unique index on
`trade_id` — which is the constraint that was actually load-bearing — but did
not touch the primary key and did not add a foreign key to `positions`.

**Why it was deferred, and why that is defensible:** the FK is the harder half.
`trade_id` is TEXT holding a stringified `positions.id`, so a real FK needs a
type change on a column that three call sites write, and `positions` rows are
deleted by `reset_paper_account()` while excursion rows are deliberately kept —
so the FK needs an `ON DELETE` policy that encodes a decision nobody has made
yet. `get_pattern_excursions()`'s mandatory ticker-agreement check is the
runtime substitute and it is doing the job.

**What to do:** treat as Phase 3. When taken, the sequence is: decide the
`ON DELETE` semantics first (my read: `SET NULL`, because the excursion is a
fact about a trade that happened even after the position row is gone), then
migrate the type, then add the FK. Do not add the FK first.

### C2. `robinhood_sync.py:141` calls `reset_paper_account()` with no guard
The review flags the exposure and is right. Current state:

```python
db = Database()
old = db.get_paper_account()
if old:
    print(f"Wiping existing paper account (cash ${old['cash']:.2f}, ...")
db.reset_paper_account()
with db._lock, db._conn() as conn:
    conn.execute("DELETE FROM paper_equity_history")   # <- now dead code
```

Three problems:

1. **No confirmation and no backup.** It prints what it is about to destroy and
   then destroys it in the next statement. Every other destructive path in this
   repo goes through `scripts/tp backup` first. This one is reachable from a
   CLI subcommand with no typed-confirmation step — compare `live_trader`'s
   confirm-phrase requirement, which exists for exactly this class of mistake.
2. **The `DELETE FROM paper_equity_history` on the following lines is now
   redundant**, since §48 moved it inside `reset_paper_account()`. Harmless
   today, but it is the kind of leftover that makes a future reader think the
   reset does *not* clear the curve and add it back somewhere else too.
3. **It reaches into `db._lock` and `db._conn()`** — private API, from a
   top-level script.

**Fix:** delete the redundant `DELETE` and the `_lock`/`_conn` block entirely;
add a typed-confirmation prompt and a `scripts/tp backup` invocation before the
`reset_paper_account()` call; assert the path is not reachable from `server.py`
or any UI route.

### C3. `packet_builder.py:428` does not say *by what*
```python
f"High-vol positions open: {pr.high_vol_position_count}",
```
Post-§53 this number means "positions whose **ATR at entry** was ≥ threshold."
Before §53 it meant "positions whose **stop distance** was ≥ threshold." Same
label, different quantity, and an operator comparing today's packet to last
week's has no way to know.

`engine/rules_catalog.py:420` already says ATR correctly. Make the packet agree:
`f"High-vol positions open (by entry ATR%): {pr.high_vol_position_count}"`.

**Related and slightly worse:** while `entry_atr_pct` is NULL on legacy rows,
this count is a *mixture* of ATR-measured and proxy-measured positions. That
mixture is visible in the logs (`_position_atr_pct` warns per row) but not in
the packet. Consider surfacing the fallback count alongside it until the book
has turned over — see B6.

---

## D. One coverage gap worth naming explicitly

Not in the review, but it is the thing most likely to disappoint whoever next
queries `exit_kind`.

`classify_exit()` deliberately returns `None` for any `sell_rules:` reason,
because those are genuinely free text assembled per-trade by
`rules/sell_rules.py` and `engine/stop_state_machine.py`. **That is the most
common exit path.** So `exit_kind` will populate for price-watch stops,
rotations, EOD flattens, time stops and manual confirms — and stay NULL for a
large share of real exits.

This is the right call (a bucket half-filled by guesswork is worse than an
empty one), and the docstring says so. But it means:

- Do not read early `exit_kind` distributions as representative of exits
  overall. They are representative of *mechanically-closed* exits.
- The actual fix is Phase 3: give `sell_rules` a structured exit code **at the
  point of decision**, where the reason is known, rather than growing a
  string-matching table in `common.py` that silently drifts from its producers.
- Anything built on `exit_kind` before that lands should report its own
  coverage (`COUNT(exit_kind IS NOT NULL) / COUNT(*)`) next to its result.

---

## Summary

Nothing in the review's seven findings requires new code — all were fixed in
`b08bced`. Nine operational items (B1–B9) remain, in that order, and three
genuine gaps (C1–C3) plus one coverage caveat (D) are open and cheap.

The critical path is short: **assess → back up → purge → apply migrations →
reset → recalibrate → full test run → release with the §53 decision-change
note.** Do not reorder it; each step's output is the next step's input, and
`migrations/010` is deliberately built to fail loudly if you skip B2.
