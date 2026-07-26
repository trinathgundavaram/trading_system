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

## [3.1] — v3.1.0 — 2026-07-26

### Decision function: UNCHANGED — presentation, monitoring and one data-layer fix

`scripts/classify_change.py` reports MINOR and this ships as **minor**. Worth
recording why, since the scope invites a major bump: the diff touches
`main.py`, `server.py` and `storage/database.py` (all BEHAVIOUR_PATHS), adds
four columns and two endpoints, and re-themes the entire UI — but no file in
`DECISION_PATHS`, no `migrations/`, and no decision-relevant `config.yaml` key.
Scoring, sizing, exits and vetoes are byte-identical.

That distinction is the whole point of this project's versioning rule. A major
bump asserts that trade history either side of it was produced by different
strategies and must not be pooled. Claiming that here — on a release whose
largest change is a colour palette — would corrupt every future comparison
against v3.0.0 data for no reason. Size of diff is not the criterion; movement
of the decision function is.

Nothing in this entry alters how market data maps to a buy, a size, or an exit.
`rules/`, `engine/` scoring and the exit path are untouched, so trade history
from v3.0.0 pools with this. What changed is what the UI *says* about that
mapping, plus one storage-layer bug that had silently disabled a monitoring
panel.

Prompted by a full click-through audit of every tab in both books
(2026-07-26). Grouped by severity rather than by file, because the severity is
the point.

#### Fixed — the UI stated things that were not true

- **`storage/database.py`: `get_source_health()` always returned `[]` on
  Postgres.** `_PGCursorWrapper` implemented `fetchone`/`fetchall`/`rowcount`/
  `lastrowid` but not `description`, and this was the tree's only
  `cur.description` caller. The resulting `AttributeError` was swallowed by a
  bare `except Exception: return []` annotated "table not created yet", so
  after the v3.0 Postgres migration the Monitor tab reported **NO DATA YET for
  all 14 data sources, permanently**, with nothing in the logs. A monitoring
  panel that cannot report a problem is read as "no problem". The property is
  implemented, the reader no longer depends on it, and the `except` is narrowed
  to the condition it claimed to handle.
- **`server.py`: `/api/logs` destroyed every traceback.** Lines were sorted by
  their whole text (`merged.sort(key=lambda e: e["raw"])`) on the assumption
  that asctime sorts lexically — true only for lines that *begin* with a
  timestamp. Traceback frames carry none, so they sorted by their own text:
  identical frames from unrelated failures stacked together and
  `Traceback (most recent call last):` appeared with no frames beneath it. Now
  carries the last-seen timestamp forward per source and sorts on
  `(ts, source, position)`; level filtering keeps a kept record's continuation
  lines instead of orphaning them.
- **`${RH_ACCOUNT_NUMBER}` was rendered into the Control tab as if it were an
  account number.** `/api/state` ships raw YAML by design (the server also
  writes that file, and an expanded tree would bake a secret into a versioned
  one). New `_redact_config_for_ui()` converts a `${VAR}` reference into a
  descriptor — variable name, whether it resolves, and a masked tail — and the
  UI renders a read-only "managed by .env" field. `POST /api/config` now
  refuses to overwrite a `${VAR}` reference with an empty string, so a stale
  tab cannot silently unlink the account.
- **Signals showed `HOLD` for tickers that were never scored.** During the
  audit all 42 rows of a scan read `HOLD`; every one had been vetoed before
  scoring ran (weekend-stale quotes). Expanding a row said so; the column the
  table is scanned by did not. New `VETOED` and `HOLDING` pills, driven by a
  single `isVetoed()` predicate shared with the Dashboard.
- **A sentinel leaked as fact:** `hours_to_next_macro: 999.0` rendered as
  "999.0h to next macro event" — a fabricated 41-day countdown. Now "none
  scheduled".
- **Copy that contradicted the code.** The sidebar strapline
  ("Decision support - no auto orders"), the Control tab description ("No order
  is ever placed from here"), the Watch/Execute help ("orders are placed there
  (never from this app)") and the Positions panel ("this UI can't place or
  confirm trades itself") all predate `engine/live_trader.py` and were false.
  The strapline is now derived live from the real gates. Also reconciled: the
  Risk tab's heat caption and its tooltip named different denominators, and
  "6-bucket" vs "7-bucket" scoring disagreed across three strings.
- Two `var()` references to CSS tokens that were never defined
  (`--text-muted`, `--bg-subtle`), and unguarded `b.weight` / `b.min_pct`
  arithmetic that could print `NaN pts`.
- **`main.py --ui` ignored `TP_UI_PORT`.** It bound 8080 unconditionally *and*
  passed `port=8080` to `_free_ui_port()`, whose job is to kill whatever holds
  that port — while `scripts/tp` assigns every installed version its own port
  and `scripts/services.py` honours it. `tp run <tag> --ui` would therefore
  kill the PRIMARY version's UI and take 8080 for itself: the exact
  cross-version collision the version manager exists to prevent, arriving
  through the one launcher that ignored the mechanism. Quiet from the
  browser's side too — `localhost:8080` keeps serving *a* UI, just a different
  version's.
- The Data Sources panel printed raw naive-UTC ISO strings
  (`2026-07-26T12:58:29.638442`) because the note was pre-formatted
  server-side. It was the only surface bypassing `fmtCST()`, which exists
  precisely to stop unconverted, unlabelled UTC reaching the screen.
- The Real Portfolio panel printed the **full** Robinhood account number, which
  made the Control tab's new "the full number never leaves the server"
  statement false on the very next tab. Both now go through one
  `_mask_account()` helper.
- `REAL_APPROXIMATE` had no entry in `STATUS_PILL`, so it fell through to a
  bare `pill` class with no background or colour and rendered as unstyled text
  in a column of coloured pills — and the legend above the table did not
  mention it. One rule in 49 carries it, which is why it went unnoticed. The
  legend is now generated from the same map, so a new status cannot appear
  without one.
- The veto banner said "18 of 20 tickers" directly beneath a banner reporting
  43 tickers scanned: `recent_signals` is capped at 20 server-side. It now
  names the sample as a sample.

#### Fixed — the Real Portfolio tab reported the wrong money

Three separate problems, all found by Trinath reading the numbers and noticing
they did not add up. Each was individually plausible on screen, which is why
none had been caught.

- **"Total portfolio value" was actually portfolio CASH.**
  `_robinhood_account_probe()` resolved it through a first-non-empty chain,
  `("equity", "portfolio_cash", "total_equity", "market_value")` — but
  `load_account_profile()` has no `equity` key at all; equity lives on
  `load_portfolio_profile()`, a different endpoint. The chain therefore always
  fell through to `portfolio_cash`. Verified against the broker: cash
  $1,859.94, holdings $144.21, true total $2,004.15 — and $1,859.94 is exactly
  what the tab displayed. The error equals the value of the holdings, so it
  looks correct whenever the book is nearly flat. Now reads equity from the
  portfolio endpoint, falls back to cash + market value, and returns `cash`
  and `holdings_value` separately so the headline is checkable by eye.

- **The real book merges positions across Robinhood accounts.** The tab showed
  four positions worth $245.77 under an account-value figure for account
  ...3794; that account holds three, worth $144.21. The fourth (CLF) was
  bought in the PRIMARY account. Nothing is corrupt — the schema cannot
  express the distinction: `positions` has no account column, and
  `engine/account_sync.py`'s own docstring records that robin_stocks'
  `build_holdings()` takes no `account_number` and only ever sees the primary
  account. So every aggregate on the tab — market value, unrealized P&L,
  invested cost, portfolio heat — sums across accounts while the header
  reports one account's value.

  New `GET /api/real/reconcile` and a Broker Reconciliation panel compare the
  local book against the configured account and classify each disagreement
  (`not_held_in_this_account`, `share_count_differs`,
  `held_at_broker_but_not_tracked`) with the remedy for each. **It reports
  only — it mutates nothing.** Deciding what a mismatched row means needs a
  human, and deleting position rows to make a total agree would compound the
  original fault rather than fix it. The value panel also warns when the
  broker's holdings figure and the locally-summed market value diverge.

- **All-Time Realized could not be audited.** `-$369.91` on a book whose
  ledger holds six real trades and exactly one completed round trip (a
  four-minute VZ buy/sell) is not a number anyone should act on. It is
  `SUM(daily_stats.realized_pnl)` over every row ever written — an accumulator
  spanning earlier versions, with no drill-down. The card now recomputes the
  total from the closed round trips in the ledger and states plainly whether
  the two agree, so it either earns trust or visibly fails to. It is also
  explicitly labelled *real book only* (paper P&L lives in the separate
  `paper_realized_pnl` column and always did — that separation was correct;
  the auditability was not).

#### Fixed — `close_position()` destroyed its own evidence

Chasing the −$369.91 found the root cause, and it is a data-integrity bug
rather than a display one.

`close_position()` computed `pnl = (exit_price - entry_price) * shares`, added
it to `daily_stats`, and then **discarded every input**. The position row
recorded `status='closed'` and nothing else — no exit price, no exit time, no
stored P&L. The paper book survived this because `paper_trades` logs each
simulated sell; the real book had no equivalent, so a real close left a flag on
one table and a number in a daily accumulator with nothing connecting them.

That is why the figure could not be investigated: not because the evidence was
hidden, but because it was never written. An audit query written against
`positions.exit_price` failed with "column does not exist", which was the
finding.

- `positions` gains `exit_price`, `exit_time`, `realized_pnl` and `closed_by`.
  NULL on pre-existing rows, which is itself the signal — those are exactly the
  closes that cannot be audited.
- **`close_position()` now refuses a non-positive `exit_price`.** It used to
  accept `0`/`None` and compute `(0 - entry) * shares == -cost`, booking an
  entire cost basis as a realized loss. That is the single most plausible way a
  small book records a large negative number, and it is always a bug — a real
  fill has a real price.
- The closing `UPDATE` was scoped by `ticker + status`, while the `SELECT`
  above it deliberately took only the newest row (`ORDER BY id DESC LIMIT 1`).
  The two disagreed, so a duplicate open row in the same ticker would be
  silently closed with no P&L recorded for it. Now scoped by `id`.

`scripts/audit_realized_pnl.sql` traces any realized-P&L figure back to the
positions that produced it. `scripts/cleanup_test_pnl.sql` clears the
2026-07-24 test-session accumulator — backing the original values up to
`daily_stats_pnl_backup` first, zeroing only the two accumulator columns for
that one date, and deleting no trade, position or history row.

#### Fixed — behaviour

- **Reload keeps your place.** There was no route: every tab lived at `/` and
  `boot()` ended in a hardcoded `showTab("dashboard")`, so reload, restore-session
  and the back button all landed on the Dashboard. Now a hash route
  (`#tab`, or `#tab/book` for the three book-aware tabs), with a topbar
  **Refresh** that re-pulls state and re-renders in place.
- WebSocket used a hardcoded `ws://` — blocked as mixed content on any https
  deployment, silently killing live updates. Now scheme-aware.
- The 30-second ping `setInterval` was registered inside `connect()`, which is
  also the reconnect path, so every drop leaked another timer.
- Learning tab called `summary.profit_factor.toFixed(2)` where the Performance
  tab uses `fmtRatio()` — `profit_factor` is legitimately null until the book
  has a loss, so that panel threw as soon as there were wins and no losses.
- `toast()` was called with four kinds (`success`/`error`/`alert`/`info`) and
  only two were styled; warnings and errors were visually identical grey boxes.
  All four now styled, aliases normalised, errors persist longer.

#### Added — values the server computed and the UI never showed

A field-by-field diff of every API payload against the UI found several
computed values with zero references in `index.html`. New **Execution Posture**
panel on Control surfaces them as one AND-ed verdict:
`live_trading_armed`, `execution_path` (who actually submits the order),
**`orders_breaker_open`** (when open, real orders are silently skipped —
previously invisible), `read_ok`, and the backtest `validation` receipt.
Monitor now shows `kill_reason` and the running `pid`.

Also new: a Dashboard banner explaining *why* a scan produced no buy signals
when vetoes dominate the batch — "42 of 42 vetoed before scoring ran" is a
different fact from "nothing qualified", and the UI could not previously tell
them apart.

#### Removed — redundancy

- **Chart.js** was fetched from a CDN on every page load and never used: the
  file contained exactly one occurrence of the string "Chart", the script tag
  itself. The one chart is hand-rolled SVG. (~200 KB and a third-party round
  trip on every load of a local-first dashboard.)
- The Learning tab's three-metric strip was a byte-for-byte duplicate of the
  Performance tab's, reading the same endpoint — and was the copy with the
  null-handling bug. Replaced with a pointer.
- Dashboard "Market Pulse" and News "Market Mood" were two renderers of
  overlapping data that had already drifted: the Dashboard, whose job is the
  at-a-glance summary, was the one missing the regime — while the regime pill
  sat directly above it. Now one shared `marketMoodPanel()`.
- Positions' "Manage a fill" panel (about `confirm_fill.py`, real book only)
  rendered in the Paper view too; empty states described both books at once.

#### Changed — theme: "indigo on warm paper"

The first pass ported Robinhood's palette fairly literally. Trinath's read was
that the reference was right but the result was too close to a replica, and
that the *text* tone specifically should differentiate it. Second pass:

- **Indigo owns interaction; green and red own money.** Robinhood uses one
  green for both "selected" and "made money", which works in an app showing one
  number at a time and actively hurts in a dense table where a green nav item,
  a green primary button and a green P&L cell compete for the same meaning.
  Active nav, primary buttons, focus rings and links are now `#4338CA`; gain and
  loss keep green/red and nothing else does.
- **Warm surfaces** (`#FDFDFC` page, `#F5F4F1` fills) instead of white and
  blue-grey — a paper cast that is the most immediately distinguishing choice
  on the page and easier over long sessions.
- **Warm near-black text** (`#1C1B1A`) on a warm slate ramp, rather than the
  cool blue-black most fintech UIs use. This was the specific request: text
  tone carries most of a UI's character because it is everywhere.
- Gain/loss remain semantic and contrast-corrected for the 10px table figures
  this UI is full of. Being novel about those colours costs comprehension.

Every colour is a token; the ~24 hardcoded hexes scattered through the rules
are gone. Strategy's 2,700-character intro paragraph moved behind a disclosure;
long prose capped to a readable measure; mobile breakpoint added.

#### Changed — filter row

Two complaints with one cause: the row was a permanent full-width band of
bordered boxes under every header, so on a table like Signals it drew a second,
competing header of empty form controls above the data. Native `<select>`
elements made it worse — OS-drawn arrows at the OS's own height, which no amount
of surrounding styling can bring into line.

- Controls are borderless on the row's own tint and resolve into a bordered
  field only on hover or focus, so an unused filter reads as quiet placeholder
  text and the active one is unmistakable.
- Native select arrow replaced with an inline SVG chevron; heights pinned so
  text inputs and selects agree.
- A filter that is actually filtering keeps a tinted, accent-bordered state at
  rest — previously the only way to spot one was to scan a dozen identical
  boxes for stray text, so a table that looked empty because of a forgotten
  filter in an off-screen column was indistinguishable from one with no data.
- Active filters now render as removable chips naming their column, with a
  "N of M rows" count and "Clear all".

#### Added — tests

`tests/ui/` boots the real `ui/index.html` in jsdom against captured payloads.
`run.js` renders all 13 tabs × 2 books; `assert.js` carries 31 named
assertions. The file had no test of any kind before this.

## [3.0] — v3.0.0 — 2026-07-26

### Decision function: CHANGED — re-validation required before arming live

`scripts/classify_change.py` reports MAJOR and this ships as **major**. The two
agree, which is the uninteresting case; what is worth recording is that
`classify_change.py`'s own docstring uses this exact change as its worked
example — *"Deleting `* b.qual_mult` from the weighted sum (§19) is a one-line
change that a conventional scheme would call a patch. It re-scores every
candidate in the system... That is a major bump."* That is precisely what
happened here, arrived at from the opposite direction: a zero-trades audit
rather than a release review.

**`config_fingerprint` is UNCHANGED at `cc9a149613427f56`, and that is a gap,
not a reassurance.** The fingerprint hashes `config.yaml` values only. Every
change below moved the decision function in *code*, touching no config key, so
the mechanism §17 built to make strategy changes self-partitioning does not fire
here. Nothing automatically distinguishes a pattern row scored before this
release from one scored after it.

> **Pattern rows recorded before v3.0.0 must NOT be pooled with rows recorded
> after it.** The partition is the release boundary, not the fingerprint. Any
> analysis spanning 2026-07-26 has to filter on `app_version` by hand.

Trade history is unaffected in the sense that no *closed trade* changes
retroactively — but every score, every threshold and every exit in the backtest
corpus was produced by a different function than the one now running.

### The scoring ceiling: 29,882 candidate-days, 0 trades, and why that was not the strategy

A 3-year Stage 1 replay over 60 tickers scored 29,882 candidate-days and
produced **zero** trades, with the score distribution showing a hard right edge
at 52.94% that did not move between runs. A distribution whose maximum is
identical across two runs is a ceiling, not a signal. Three compounding
compressions, in `rules/swing_buy_rules.py`:

- **The 1.25× redistribution clamp could not do the job it was added for, and
  cost 17pp of ceiling doing it.** The 2026-07-21 review asked that no bucket be
  able to "dominate the entire score" when several go dark simultaneously. The
  implementation clamped `scale` — but `scale` multiplies every available bucket
  uniformly, so it cancels out of the share ratio entirely:
  `share_b = (w_b − unavail_b)·scale / (w_avail·scale)`. Relative dominance was
  exactly the same at 1.25 as at 1.54. The clamp guarded nothing and lowered the
  reachable ceiling from 89.5% to 72.5%. Replaced with a real per-bucket share
  guard (`MAX_BUCKET_SHARE`), which — because share is scale-invariant, so an
  over-concentrated composite *cannot* be repaired by rescaling — surfaces as a
  confidence dock and telemetry rather than as a silently lower ceiling.

- **`_qualification_multiplier` was applied twice.** Bucket contribution was
  `(points/max_points) × weight × qual_mult`, where `qual_mult` is itself a
  function of `points/max_points` — so the effective contribution was ≈ pct². A
  bucket at 80% completion contributed 0.704, at 60% contributed 0.420. Real
  candidates sit at 70–85% and essentially never at 100%, making this the
  dominant real-world compressor (mean score 21.7% against a 50% bar). It was
  also not the documented design: that function's docstring says it exists to
  *replace* a binary qualification cliff, not to compose on top of an
  already-proportional term. Now applied once, via the anchor curve, recomputed
  from `_effective_bucket_pct` so EXTERNAL's partial-outage handling still feeds
  it.

- **The threshold never learned that the score scale had shrunk.** 25% of an
  unavailable bucket's weight is deliberately left dead, but `dynamic_thresholds.py`
  returns a bar on a fixed 0–100 scale, so the dead weight was charged twice —
  once by compressing the score, once by leaving the bar where full coverage
  would need it. A nominal 50% bar was really demanding 55.9% of measurable
  evidence, and got stricter every time a data source went down. Every outage was
  quietly becoming a regime nobody chose. The threshold is now rescaled by the
  achievable ceiling; `final_score_pct` keeps its existing meaning so stored
  `final_score` rows stay comparable, and the rescale is a no-op at full
  coverage.

This is the same defect class as the VOLATILITY_EXPANSION drag fixed on
2026-07-15 (its 7% weight capped every non-squeeze stock at 93%), at four times
the magnitude and reached by a different mechanism. `tests/test_scoring_sanity.py`
gains 5 tests pinning the ceiling under every availability combination — the
missing test both times.

### The backtest was not replaying the exit policy the system actually runs

With trades finally flowing, the first result was 302 trades at profit factor
1.10 — a thin-to-nonexistent edge. It was mostly an artifact.
`simulate_forward_exit` held **one** stop price for a whole 20-day hold, while
production runs `engine/stop_state_machine.py`'s 6-state ratcheting stop on
every cycle. Of 302 trades, 213 reached `breakeven_r` (0.5R) and **123 of those
still recorded a full stop-out**, because nothing ever moved the stop up.
Production would have exited them near flat. The 208 losers were not going
straight down — 53.8% reached +3% first and 17.8% reached +10% first, then
round-tripped. Losers that rally first and winners that never dip is the
signature of a missing trailing stop, not a bad entry.

`engine/backtest_engine.py` now replays `stop_state_machine.calculate()`
bar-by-bar with production's `should_advance()` ratchet. Win rate **29.8% →
53.5%**, PF **1.10 → 1.23**, on an unchanged ticker set.

The subtle part is point-in-time discipline, and it has its own test. The stop
in force during bar *i* is priced off bar *i−1*'s close. Re-pricing from bar
*i*'s own close and then testing that bar's low against it would be look-ahead
that flatters trailing stops **specifically** — it would manufacture the exact
improvement the change is meant to measure.
`tests/test_backtest_exit_replay.py` (9 tests).

Also: `summarize()` now reports **expectancy in R**. The replay equal-weights
percentage returns, so a stop-width comparison read on `avg_outcome_pct` rewards
wider stops for taking more risk per trade. Exit vocabulary moved to
`rules/common.py`'s canonical `EXIT_KINDS` — a stop hit in `TREND_FOLLOWING` is
a `trailing_stop` protecting profit, not a `stop_loss`, and collapsing the two
made every trailing exit read as a failure.

### What the measurements say, and what they do not

Swept as full re-runs (entries are path-dependent — `i = exit_idx + 1` — so
nothing can be re-filtered from a saved trade list), read on expectancy_R:

| Config | n | win | PF | exp_R |
|---|---|---|---|---|
| stop=8 (current) | 396 | 53.5% | 1.23 | 0.119 |
| **stop=16** | 330 | 58.8% | 1.48 | **0.202** |
| stop=16, trail=0.75 | 370 | 56.8% | 1.28 | 0.157 |
| stop=16, r=4.0 | 324 | 58.3% | 1.44 | 0.202 |
| stop=16, threshold=62 | 257 | 59.5% | 1.44 | 0.178 |

Only the stop cap matters: it was binding on 94 of 181 stop-outs, clamping
tighter than the ATR justified. Trail and `r_multiple` are already at optimum,
and **raising the threshold makes things worse** — the opposite of what the
broken exit model implied, where ≥65 looked like PF 1.60. Acting on that earlier
reading would have degraded the system.

**No `config.yaml` value is changed in this release.** The sweep ran on
`engine/ta_fallback.py`, not the `pandas_ta` backend every threshold in
`config.yaml` was derived on, and `engine/ticker_analyzer.py` fails closed on
that for exactly this reason. Tuning against fallback numbers is how a threshold
derived on one backend gets replaced by one derived on another.

On a mega-cap holdout (MSFT, GOOGL, BAC, PFE, CMCSA, F) the stop change is
byte-identical — median risk 2.66%, so the 8% cap never binds — which is the
right property for a targeted fix. The same run shows **PF 1.01, expectancy
0.004R**: no edge at all on liquid large-caps. The entire measured edge sits in
high-volatility names over a 2023–2026 window containing a large crypto/momentum
run. That is one volatility bucket in one favourable period, not a validated
strategy, and `learning/walk_forward.py` exists and has never been pointed at it.

Full analysis: [docs/backtest_eval_2026-07-26.md](docs/backtest_eval_2026-07-26.md).
Sweep harness: `scripts/sweep_exit_params.py`.

## [2.4] — v2.4.0 — 2026-07-26

### Documentation audit: what the docs claimed was unbuilt, checked against the code

**Decision function: UNCHANGED.** `config_fingerprint` `cc9a149613427f56` —
deliberately unchanged; see the feature-schema note below for why the pattern
encoding change is handled per-row rather than by invalidating the fingerprint.
`classify_change.py` says MINOR.

Every "not implemented" / "placeholder" / "not wired" / "deferred" claim across
`README.md`, `CHANGELOG.md`, `BUILD_FROM_SCRATCH_GUIDE.md`,
`pre_selection_criteria_and_trading_modes.md`, `prod_readiness_plan.md` and
`docs/` was re-read against the code it describes. `BUILD_FROM_SCRATCH_GUIDE.md`
and `docs/trayd.md` came back clean. The rest did not.

- **README.md opened with three false statements about live execution, and had
  since `engine/live_trader.py` landed.** It said `auto_trade` was "not
  implemented at all", that "nothing in this codebase places live orders", and
  that this code "never places, modifies, or cancels an order." `live_trader.py`
  submits fractional market buys and sells against the real Robinhood account
  through `robin_stocks`. The word `live_trader` did not appear in README.md at
  all. `docs/trayd.md` and this CHANGELOG had it right the whole time — README
  was the outlier, and it is the file a new reader opens first. Replaced with
  the actual gate table (`TP_FORCE_PAPER`, `live_execution_enabled`, validation
  receipt, `watch_execute: EXECUTE`, `auto_trade`) and an explicit note that the
  old claim was wrong, rather than a silent edit.

- **`engine/pattern_features.py` was still writing constants for seven features
  whose real sources had gone live sessions earlier.** ADX, CMF, sector RS (1d
  and 1m), the TTM squeeze, unusual-options flow and the opex calendar were all
  wired into `engine/ticker_data_adapter.py` as each became real; this module
  was never updated and kept recording `0.0` / `False` / `"normal"`. README
  described this as "the source data is a placeholder", which stopped being true
  some time ago — the sources were real and the writer simply was not asking.
  Now read from the caller's already-built `ticker_dict`/`market_dict`, so the
  fix costs zero extra fetches. `tests/test_pattern_feature_wiring.py`.

- **Fixing that writer would have corrupted every existing row, so the reader
  was fixed too.** While every row held `adx = 0.0` the column's standard
  deviation was zero, `_encode_patterns` clamped it to 1.0, and every row
  encoded to 0.0 — useless but symmetric. Once real ADX readings (15–40) start
  landing, the column acquires a mean and every historical `0.0` z-scores to a
  large negative number: the old rows stop being uninformative and start
  asserting *"extremely low ADX"*, a measurement nobody took. Rows are now
  stamped `feature_schema`, and unstamped rows have those seven features
  treated as **missing** — numeric ones mean-imputed (z = 0, "this row tells us
  nothing"), categorical ones routed to a distinct `__unrecorded__` bucket so a
  stale `False` cannot pass for a measured one.
  `tests/test_feature_schema_migration.py` includes a test that disables the
  guard and asserts the misreading reappears, so the guard cannot be deleted as
  dead code once every row carries the stamp.

- **`config_fingerprint` deliberately not touched.** It answers "was this row
  produced by a different strategy", and the strategy did not move — only the
  fidelity of what was recorded about it. Folding the schema in would have
  discarded the entire pattern history for what is, handled correctly, a
  recoverable gap.

- **`scripts/classify_change.py` could not see this class of change.**
  `learning/pattern_database.py` and `engine/pattern_features.py` were in
  neither `DECISION_PATHS` nor `BEHAVIOUR_PATHS`, so a change to what the
  learning backend records classified as PATCH. Added to `BEHAVIOUR_PATHS` —
  not `DECISION_PATHS`, because MAJOR would wrongly declare the whole pattern
  history unpoolable every time a feature is added, and that question now has a
  better home in `FEATURE_SCHEMA_VERSION`.

- **`prod_readiness_plan.md` listed the platform's three biggest infrastructure
  gaps as open; all three had shipped.** The Postgres migration, the persistent
  connection pool (`_get_pool()`, `ThreadedConnectionPool`) and external
  alerting (`engine/notifications.py`'s desktop → webhook → log chain) were all
  marked "not attempted, needs your call" from 2026-07-21 onward. Struck through
  and annotated rather than deleted. Its §2.2 — the SQLite `open()` stall behind
  the 90-minute hang on 7/20 — is marked resolved-by-architecture: there is no
  per-call file `open()` left to stall.

- **Smaller corrections.** `rules/hard_vetoes.py` has 13 active vetoes, not 15
  (slots run 1–16, #6 was never filled, §54 removed `DAILY_LOSS` and
  `PROFIT_LOCK`). The UI has 13 tabs; README said 9 in one place and 10 in
  another. The "NVIDIA NIM fallback cascade" entry pointed at
  `engine/claude_brain.py` as though it existed and merely needed wiring — that
  file appears nowhere in the git history; the true part of the claim is that
  the pipeline calls no LLM automatically.

- **Portfolio heat: claim true, stated reason false.** `db.get_portfolio_heat()`
  is genuinely still un-normalized against account equity. The reason given in
  README, the docstring and the UI tooltip — "no Robinhood balance API is called
  from Python by design" — is not: `engine/account_sync.py`, `robinhood_sync.py`
  and `live_trader._buying_power()` all read it. Corrected in all three places.
  The behaviour is left alone on purpose: heat feeds sizing, so re-basing its
  denominator changes position sizes on a system that is already trading, and
  that is a decision to take deliberately rather than as a side effect of a
  documentation pass.

**Verified as still accurate, no change:** `momentum_screen` and
`insider_buying` remain honest stubs (no market-wide screening tool exists on
any connected MCP); `options_expected_move_pct` and
`historical_earnings_move_avg_pct` remain placeholders, so `EARNINGS_RISK` is in
practice a pure days-to-earnings check; the 11-metric strategy health score and
the named 10-step orchestration layer were never built.

**Still genuinely unbuilt after this pass**, and now an exhaustive list for the
pattern database: `vix_percentile_1y`, `vix_percentile_3m` (need VIX history;
`market_context.py` fetches spot only), and `gap_pct`, `premarket_gap`,
`premarket_rvol` (need a premarket session the scheduler does not run in).

### Phase 2.5 / Phase 3 review follow-ups

Folded into this release because v2.4.0 was prepared but never tagged — the
CHANGELOG header used the wrong version shorthand (`[2.4.0]` where
`version.py --shorthand` says `[2.4]`), so `release.sh` exited at step 5 and
never reached `git tag`.

- **`exit_kind` coverage is now carried with every consumer, not left to be
  remembered.** `db.get_exit_kind_coverage()` returns
  `{structured, total, missing, pct, unclassified_reasons, label}`, and
  `rules/common.format_exit_kind_coverage()` owns the wording so it cannot
  drift between a dashboard and a report. Wired into
  `/api/analytics/performance` and `scripts/phase4_recalibrate.py assess`.
  **The premise needed correcting**: nothing in `analytics/` or the UI reads
  `exit_kind` yet, so no consumer was misreporting — the denominator ships
  first precisely so the panel that eventually renders a breakdown cannot be
  written without one. **And the gap is historical, not a pending producer**:
  `rules/sell_rules.py` has carried `exit_kind` on its `SellResult` since §D and
  every triggered hard check supplies one, Loop B goes through
  `exit_kind_for_loop_b_label`, and the price watch, rotation, time stops and
  manual confirms all pass fixed literals. Coverage below 100% is rows closed
  before §D ageing out, not an unwired producer to go and find.

- **Migrations 009–012 are now a hard release gate.** `verify_phase2.py`'s
  database section stopped at 008, so the four migrations establishing the
  measurement base that §19–§21 re-derive scoring, thresholds and sizing tiers
  from were enforced nowhere but the runbook. Now checked, as hard FAILs that
  block `release.sh`: `idx_mae_mfe_trade_id` exists (010), `trade_id` is
  INTEGER (012), a FK to `positions` exists whose delete rule is SET NULL and
  not CASCADE (012), and no orphaned excursion rows survive (§49). Each reads
  the *shape* the migration leaves rather than a bookkeeping row, because a
  restore from a pre-cutover backup loses the shape and keeps the bookkeeping.

- **The FK delete policy is now exercised, not just read.** The existing tests
  assert the migration *file* contains "ON DELETE SET NULL". That catches a
  hand-edit and nothing else — it passes identically on a database where 012
  was never applied, or where the FK exists under a generated name with a
  different rule. `tests/test_mae_mfe_fk_lifecycle.py` performs the destructive
  operations the policy exists to survive: a bare `DELETE FROM positions`, and
  `reset_paper_account()`, asserting the excursion row survives with
  `trade_id IS NULL` and its measurement intact, that a real position's link is
  untouched by a paper reset, that an orphan is excluded from
  `get_pattern_excursions()` rather than counted as zero, and that the FK
  refuses an orphan being written in the first place.

- **`high_vol_atr_pct_threshold: 5.0` is documented as provisional.** Unlike the
  drawdown caps, it was never measured — a round number that sounded like "high
  volatility". It also cannot be fitted while `high_vol_proxy_count` is
  non-zero, because the open-position side is then part real ATR and part
  stop-distance proxy (§C3) and a percentile over a mixed population describes
  neither. Recorded in `config.yaml` with the recalibration trigger and what to
  write when measured, and in `engine/rules_catalog.py` so the Strategy tab
  stops presenting 5.0% as a derived figure. `max_simultaneous_high_vol_positions`
  flagged the same way.

- **Phase 4 proposals now carry a model identity.** `phase4_proposal.json` had a
  timestamp and nothing else identifying, and the default output path is the
  same file every run — so two proposals from either side of a §19 weight
  change were structurally identical documents describing different models.
  Now stamped with `config_fingerprint`, `app_version`, the writer and sample
  `feature_schema` values, a `mixed_feature_schema` flag, and the exit_kind
  coverage the §20 distribution was computed over. This is the "later notebooks
  mix pre- and post-Phase-3 EV curves" risk: pooling two EV curves produces a
  curve, and a plausible one.

**Verified as accurate, no change needed:** `engine/ev_engine.py` retains its
HONESTY NOTE on horizon proxies for intraday drawdown, in four places, and it
correctly says not to lift the warning until MAE/MFE is clean and linked.
`get_pattern_excursions()` has all three documented safeguards — the indexed
one-hop join on `pattern_database.trade_id`, mandatory ticker agreement, and a
deduping fallback that warns when the unique index is missing. The drawdown caps
already record their own provenance and their "~20 sessions" revisit trigger.

**Cannot be done from code, and is still outstanding:** the Phase 2.5
operational tail (B1–B7) against your Postgres, and the two threshold
recalibrations, which need measured distributions that do not exist yet.

Tests: 429 passing, up from 403. The 8 new FK-lifecycle tests require Postgres
and skip without it — they are statically verified (signatures, `?`→`%s`
translation, `@contextmanager` auto-commit) but have not been executed.

## [2.3.2] — v2.3.2 — 2026-07-26

### v2.3.1's port fix was in the wrong layer

**Decision function: UNCHANGED.** `config_fingerprint` `cc9a149613427f56`;
`classify_change.py` says PATCH.

- **The reclaim only ran from `services.py`'s verbs, so launchd's relaunches
  bypassed it and the loop v2.3.1 was written to stop was still running when
  v2.3.1 was tagged.** `KeepAlive` re-executes `main.py --ui` directly — as do
  systemd's `Restart=` and Task Scheduler — re-entering none of `services.py`.
  One orphan held 8080, launchd spawned a replacement every few seconds, and
  each printed the entire startup banner including `Serving http://...` before
  dying on `[Errno 48]`. The log reads like a healthy server restarting, which
  is why 342 iterations went unnoticed.

  `_free_ui_port()` now guards the bind itself, in `main.py:run_ui()` between
  the banner and `uvicorn.run()` — the one point every launcher passes through.
  Wrapped so any failure (no `scripts/`, no `lsof`) prints a note and proceeds:
  refusing to start because the cleanup could not run would be worse than the
  bug.

  Two new tests assert *placement* — that `_free_ui_port` precedes
  `uvicorn.run(` and sits inside a `try`/`except`. v2.3.1's eight behavioural
  tests all passed and missed this, because the function was correct and simply
  unreachable from the path that mattered. Tests proving a helper works say
  nothing about whether anything calls it.

- Known behaviour change: running a launchd UI job **and** `run.sh --ui`
  together now means they fight for the port, last one winning. Same as
  `run.sh`'s behaviour since 2026-07-14, and better than a permanent loop.
  Don't run both.

## [2.3.1] — v2.3.1 — 2026-07-26

### v2.3.0 was correct and never ran

**Decision function: UNCHANGED.** `config_fingerprint` verified at
`cc9a149613427f56`; `classify_change.py` says PATCH. Trade history pools with
v2.2.0 and v2.3.0.

### Fixed — `./service.sh restart` did not replace the running process

- **`scripts/services.py` had no port-conflict handling, so the old UI kept
  port 8080 and the new code never served.** launchd relaunched the new process
  342 times against `[Errno 48] address already in use`, the browser went on
  talking to the pre-v2.3.0 build, and the Robinhood fix looked broken because
  the code under test was never the code answering. Every service verb reported
  success; the only evidence was in `launchd_ui.log`.

  `run.sh --ui` has reclaimed the port since the 2026-07-14 incident, whose
  comment describes this exactly — *"looked exactly like a frontend bug ...
  when it was really 'you have two of these running.'"* §45 moved service
  management into `services.py` and left the cleanup behind.

  `_free_ui_port()` now runs on `install`/`start`/`restart`, ordered
  stop → reclaim → start. Stricter than `run.sh`: it checks the holder's
  command line and **refuses** to kill a process that is not ours, since
  killing an unrelated service costs far more than a failed bind with a
  readable message. SIGTERM escalates to SIGKILL — a UI that ignores SIGTERM
  still holds the socket.

  Eight regression tests in `TestStaleUiPortIsReclaimed`, one of which asserts
  `main()` calls the cleanup in the right order: the unit can be perfect and
  the bug still ship if nothing invokes it, which is what §45 did.

- **`/api/analytics/performance` returned HTTP 500 for any book with no losing
  trade.** `profit_factor()` returned `float("inf")`; Starlette renders with
  `json.dumps(allow_nan=False)`, so it raised at render time — after the
  handler returned successfully — and the route 500'd with a traceback naming
  json.dumps rather than the source. It now returns `None`, matching
  `backtest_engine.py:569` for the same case. `gross_loss` sums `o <= 0`, so a
  single break-even trade among winners triggers it too, and a fresh install
  whose first closed trade wins hits it immediately.

- **The Performance tab could not recover from that 500.** It was the only
  async renderer with no `try`/`catch`, and used raw
  `fetch().then(r=>r.json())` rather than `fetchJSON` — no status check, no
  timeout. It sat on "Loading…" forever, the same symptom as the Journal tab's
  2026-07-23 hang. Now matches the other twelve renderers, and `fmtRatio()`
  renders a null ratio as "n/a" instead of throwing on `null.toFixed()`.

### Added

- **`SafeJSONResponse`** (`server.py`, now `default_response_class`) — a
  non-finite float becomes `null` rather than a 500. For the sources nobody has
  hit yet: `engine/ta_fallback.py` divides by `.replace(0, np.nan)` in seven
  places. Sanitising is logged, not silent — it must not become the reason a
  stray NaN goes unnoticed.
- **`snapshot()` reports `basis_drift`**, shown as a banner on the Portfolio
  tab. `ensure_seeded()` reads `paper_trading.starting_cash` only when creating
  a purse, so editing it on a live account does nothing — correct, but until now
  invisible: config said $10,000, the purse said $1,000, and the only symptom
  was "the paper cash was not updated". Reported with the exact command, never
  applied automatically.

### Removed

- The four `TRAYD_*.md` files (1,041 lines) → **`docs/trayd.md`**. All four
  walked through the same three setup steps in four voices, written while the
  integration was being built and now describing completed work. The
  consolidation states what none of them did: Trayd is integrated but on no live
  path, deliberately, because it can place orders and the only sanctioned order
  path is `live_trader.py` behind `is_live_mode()`.
- `requirements.txt.pre-phase0.bak`, `postgres_cutover_runbook.md` (superseded
  by `scripts/phase2_5_cutover.sh`).
- `ticker_selection_research.md` and `position_tier_evaluation.md` **moved** to
  `docs/archive/`, not deleted — both were flagged stale on zero references, and
  both hold findings never implemented. Zero references is a weak proxy for
  irrelevance in a repo whose analysis lives in Markdown.

Retained despite looking stale: `repair_test_damage.py` and
`assess_test_damage.py` are named in `migrations/012`'s own `RAISE EXCEPTION`
text; `phase2_5_cutover.sh` is read by `tests/test_review_followups.py`;
`scripts/tp.sh.bak` and `service.launchd.sh.bak` are documented keeps.

### Known issue, not fixable by a release

`UI_AUTH_TOKEN` is unset on this machine, so every write endpoint answers 500.
§4 correctly made this fail closed rather than default to empty; the value was
never provisioned. Run `./scripts/tp secrets set UI_AUTH_TOKEN`.

## [2.3] — v2.3.0 — 2026-07-26

### A connectivity failure that reported the wrong cause

**Decision function: UNCHANGED.** Nothing here touches scoring, sizing, entry
or exit logic. `config_fingerprint` is unchanged at `cc9a149613427f56`,
verified against `git show v2.2.0:config.yaml` rather than assumed. Trade
history from earlier versions may be pooled with this one's.

### Fixed — "Robinhood not connected" while the log said the login succeeded

- **`account.robinhood_account_number` reached the engine as the literal
  string `${RH_ACCOUNT_NUMBER}`.** Phase 0 step 0.2 moved that value out of
  the versioned `config.yaml` and left a `${VAR}` reference behind for
  `config_loader` to expand. `server.py:_load_config()` does not use
  `config_loader` — it reads the YAML raw, deliberately, because it also
  writes the file back and an expanded tree round-tripped through
  `yaml.dump` would write the account number into a versioned file. So every
  server-side caller passed the placeholder straight into
  `rh.profiles.load_account_profile(account_number=...)`.

  `config_loader.expand_env_refs()` is the new public single-value expander,
  and `live_trader._account_number()` calls it at the point of USE — which
  fixes the probe, the order path and `account_sync` together, and cannot be
  reintroduced by a future caller that loads config some third way. An
  unresolvable reference now returns `None` (primary account, the documented
  empty-value behaviour) with a warning, instead of being sent to Robinhood
  as an account number.

  Same defect had been copy-pasted into `engine/account_sync.py`. It now
  delegates rather than duplicating: sync must reconcile against the account
  the order path trades.

- **`_robinhood_account_probe()` discarded the exception that would have
  named the cause.** It was `except Exception: read_ok = False`. Every call
  threw, nothing was logged, and the only artifact was the Real Portfolio
  tab's fallback message. It now logs and returns `read_error`, exposed by
  both `/api/real/summary` and `/api/robinhood/status`.

- **The UI told you to set credentials that were already set.** That message
  was unconditional on `connected == false`, so a resolvable account-number
  fault presented as missing credentials and pointed debugging away from the
  actual problem. It is now shown only when the credentials are genuinely
  unset; otherwise the tab prints the real error. `escHtml()` added and used
  on that path — exception text can contain markup.

### Changed — the paper book is $10,000, so the rules bind instead of the purse

At $1,000, with a $500 position cap, two positions fully deployed the
account and most BUY signals were declined for insufficient cash rather than
on their merits. That makes the learning data a sample of whatever arrived
first, not of whatever scored best.

- `paper_trading.starting_cash` 1000 → 10000
- `risk.max_trades_per_day` 10 → 30
- `risk.max_position_size_usd` 500 → 1000

`risk.max_daily_loss_pct` (2.0%) resolves against actual equity, so the daily
loss limit moves from roughly $20 to roughly $200 on its own. It remains the
tighter of the two daily-loss controls and is still what binds.

### Added

- **`Database.credit_paper_capital()`** — moves `starting_cash` and `cash`
  together in a single statement. Capital in is a deposit, not a gain:
  adjusting cash alone breaks `reconcile.py`'s
  `cash == starting_cash - net_buys` invariant, and makes a $9,000
  contribution read as roughly +900% in `snapshot()`'s `total_return_pct`.
- **`scripts/topup_paper_account.py`** — applies the contribution in place.
  `ensure_seeded()` returns early when a purse exists, so editing
  `starting_cash` alone does nothing to a live account; the only other option
  was `reset_paper_account()`, which deletes the ledger, the equity curve and
  every simulated position — the wrong tool when the point of the change is to
  accumulate more data. Dry-run by default, and refuses to run against a purse
  that does not already reconcile with its own ledger.

### Known limitation logged, not fixed

`config_fingerprint` does not cover `risk.max_position_size_usd`, so this
release doubles the maximum position size without moving the fingerprint.
Widening the fingerprint now would invalidate comparability with every pattern
ever recorded, so it is a Phase 4 decision. See `docs/releases/v2.3.0.md`.

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
