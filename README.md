# Trading Platform — Current State

This project went through several architecture rewrites in one build session, then
a 4-phase incremental build on top of the working base (regime + 6-bucket rules,
web UI, position management, confirm_fill.py wiring). This README describes what's
**actually built and verified**, not the full spec that was discussed at any point.
Read this before assuming any feature exists.

`auto_trade` is not implemented at all — nothing in this codebase places live orders.
The active pipeline produces a `trade_prompt.md` you paste into Claude Desktop
(which has the Robinhood MCP connected) to execute manually.

## What's built and verified

**Data layer** (`mcp_clients/`) — direct calls to the MCP Python SDK (`pip install mcp`),
sync-wrapped so the rest of the code doesn't need to be async. Talks to 7 MCP servers:
fear-greed, yfinance, stock-scanner, maverick, finviz, fred, and (2026-07-15, optional)
robinhood — READ-ONLY only. Robinhood trade EXECUTION is still intentionally excluded —
it stays Claude-Desktop-only, and this code never places, modifies, or cancels an order.

**Robinhood (read-only)** (`mcp_clients/robinhood_mcp.py` + `robinhood_sync.py`) —
wraps the [`robinhood-mcp`](https://github.com/verygoodplugins/robinhood-mcp) PyPI
server (spawned via `uvx robinhood-mcp`, stdio, same pattern as every other client).
That server exposes zero trading tools, so this integration physically cannot break
the no-local-execution rule — it only reads real account state: positions with cost
basis, portfolio value, buying power, dividends. Activated by `ROBINHOOD_USERNAME`/
`ROBINHOOD_PASSWORD` in `.env` (plus `ROBINHOOD_TOTP_SECRET` only for authenticator-app
logins); with no credentials everything degrades silently and the platform runs as
before. Main consumer is `robinhood_sync.py`:

```
python3 robinhood_sync.py status              # portfolio value / buying power
python3 robinhood_sync.py positions           # real holdings from the account
python3 robinhood_sync.py reconcile           # diff account vs local positions table
python3 robinhood_sync.py reconcile --apply   # auto-import forgotten confirm_fill buys
```

`reconcile` catches the failure mode where a fill happened in Claude Desktop but
`confirm_fill.py` was never run (so sell_rules/ALREADY_OPEN/portfolio-risk were
reasoning about a portfolio that doesn't exist). `--apply` imports missing buys
through `confirm_fill.cmd_buy()` itself (same pattern-linking/stop-seeding/snapshot
path); it never auto-closes local positions — closing needs your real sell fill
price, and guessing one would poison P&L learning, so it prints the exact
`confirm_fill.py sell` command instead. Operational notes: robin_stocks caches its
session in `~/.tokens/robinhood.pickle`, so only the first-ever call does a full
login — which can exceed the 30s MCP timeout; warm it once by running
`uvx robinhood-mcp` manually (Ctrl-C after it starts). Unofficial API — a
`SourceCircuitBreaker` ("robinhood", 3 fails, 30-min cooldown) keeps failed-login
retries from getting the account flagged, and a concurrency semaphore of 1 keeps
account calls polite.

**Analysis layer** (`engine/market_context.py`, `engine/ticker_analyzer.py`) — pulls
market-wide context (fear/greed, VIX, macro) and per-ticker data (price, technicals,
fundamentals, news, options, insider activity), computing indicators locally.
`pandas_ta`'s pip release is broken on Python <3.12 (verified — `SyntaxError` in
`hma.py` from PEP 701 syntax), so `engine/ta_fallback.py` is a hand-rolled drop-in
replacement used automatically when the real package fails to import.

**Regime engine** (`engine/regime_engine.py`) — canonical BULL/BEAR/CHOPPY/CRISIS
classification from SPY price vs. SMA50/200, VIX, Fear & Greed, and A/D ratio, plus
a transition-probability score. One shared module-level instance per process,
recalculated once per `scheduler.py` cycle; everything else (dynamic thresholds,
the 6-bucket engine, pattern features) reads `current_state()` rather than
recomputing it. A/D ratio now comes from `engine/market_breadth.py` — see the
"Market breadth" section below for what it actually measures.

**6-bucket buy scoring** (`rules/swing_buy_rules.py`) — replaced the old 15-rule
`rules/buy_rules.py` in the live pipeline (that file still exists, just isn't called
by `scheduler.py` anymore). Six weighted buckets — TREND, MOMENTUM, VOLUME_PA,
EXTERNAL, SENTIMENT_MACRO, MARKET_BREADTH — each with a minimum qualification
threshold used ONLY for the displayed qualified/unqualified flag; every bucket
contributes continuously via `_qualification_multiplier()`'s soft curve (0% of
its own max → 0.0, 30% → 0.35, 50% → 0.60, 100% → 1.00) — there is no score
cliff at min_qualify_pct (corrected 2026-07-21 - this line was stale from
before the continuous-curve fix; see `rules/swing_buy_rules.py`'s module
docstring for the authoritative current behavior).
Gated by `rules/dynamic_thresholds.py` (regime + VIX stress, capped at +20%, plus
calendar/OpEx adjustments and an EV bonus) and `rules/hard_vetoes.py` (15 pre-scoring
vetoes: earnings risk, spread, volume, price range, breadth collapse, stale quote,
kill switch, cooldown, day-trade time windows, data completeness, already-open).
`rules/market_filters.py` was rewritten as an additional 0-100 scored market gate
(crisis mode and breadth-collapse are hard vetoes within it) - it runs alongside the
older `evaluate_market_gate()` in `engine/market_context.py`, which still governs the
coarse kill-switch/F&G/VIX/blackout check. `rules/sell_rules.py` and
`rules/risk_rules.py` are unchanged. (This became a 7th bucket shortly after - see
"Volatility Expansion bucket" below.)

**Honesty note on inputs**: `engine/ticker_data_adapter.py` is the bridge between the
real `TickerData`/`MarketContextData` dataclasses and the dict-based interface these
new rule modules use. It's explicit about which fields are real (price, RSI, MACD,
SMAs, sector, insider direction, news sentiment, market breadth, TTM squeeze/NR7/NR4/
inside-day — see below) vs. placeholder (ADX, CMF, Donchian channels, anchored VWAP,
industry relative strength, unusual options flow).

**Volatility Expansion bucket** (`rules/swing_buy_rules.py`'s 7th bucket,
`engine/ticker_analyzer.py`'s `_calc_volatility_compression`) — the original 6 buckets
cover trend, momentum, volume/price action, external signals, sentiment/macro, and
market breadth, but nothing captured volatility *contracting before it expands* -
genuinely different information from trend/momentum, which measure the direction and
strength of a move already underway, not whether the market is coiled to make one.
Added three real, zero-extra-MCP-call indicators computed from the same daily OHLCV
bars already fetched for every other indicator: **TTM Squeeze firing** (Bollinger
Bands (20, 2std) were compressed inside Keltner Channels (SMA20 ± 1.5x ATR14) within
the last 5 bars and have now released — the classic John Carter breakout signal, using
ATR14 rather than the original's ATR20 since this codebase already computes ATR14 for
everything else and the ~1-bar-lag difference doesn't matter at swing-trade horizons),
**NR7/NR4 compression** (today's high-low range is the narrowest of the last 7, or
failing that 4, trading days), and **inside day** (today's range sits entirely inside
yesterday's). Weighted 7% with a max of 14 points (TTM squeeze 6, NR7 6 / NR4 3
mutually exclusive, inside day 2) and a deliberate **0% min-qualify threshold** — this
bucket is designed to *confirm* a setup with bonus points, not *gate* one; most good
trend/momentum setups won't be in a squeeze at all, and that's expected, not a
failure. Funding this bucket meant reweighting the other six, using specific per-bucket cuts
the project owner specified: TREND 23%→21%, MOMENTUM 22%→19% (took the largest cut
since it also lost the most rules), VOLUME_PA 15%→14%, SENTIMENT_MACRO 15%→14%,
EXTERNAL and MARKET_BREADTH left unchanged at 15%/10% — still sums to 100%. MOMENTUM's `max_points` was also corrected from 62 to
35 (its true achievable sum after squeeze/NR7/NR4/inside-day moved out) as part of
this change — TREND and EXTERNAL still have the pre-existing max_points-vs-rule-sum
inconsistency flagged earlier in this document; that wasn't touched here since it
wasn't part of this request. `learning/bayesian_updater.py`'s `BUCKET_WEIGHT_BOUNDS`
and the Strategy tab's catalog (`engine/rules_catalog.py`) were updated to match.
Not yet wired: the new bucket's per-bucket score isn't captured in
`learning/pattern_database.py`'s similarity-search schema (`NUMERIC_FEATURES` only
has `bucket1_score`..`bucket6_score`) — extending that is a real schema migration
(new DB column + backfill) that wasn't in scope here; flagged in
`engine/pattern_features.py` so it isn't mistaken for an oversight.

**Market breadth** (`engine/market_breadth.py`) — real, not a placeholder, but a
*proxy*: it's calculated from the 11 SPDR sector ETFs (XLK, XLF, XLE, XLV, XLY, XLP,
XLI, XLB, XLU, XLRE, XLC) via the yfinance MCP client already wired up elsewhere,
not from true NYSE-wide advance/decline data (~3,000+ issues). It fetches ~3 months
of daily closes per ETF (cached 15 min) and computes: `ad_ratio` (today's advancing
vs. declining sector ETFs, 0-1), `pct_above_20ema`/`pct_above_50ema` (% of the 11
ETFs above their own EMA), `nh_nl_ratio` (proximity to trailing-3mo high vs. low, as
a stand-in for true 52-week new-highs/new-lows), `mcclellan` (EMA19-EMA39 of daily
net advance/decline count, rescaled to roughly the real oscillator's range),
`ad_slope_5d_positive` (breadth participation strictly rising over the last 5 days),
and `spy_ad_aligned` (does SPY's own SMA50 trend agree with the sector-breadth
direction — computed against the same SPY fetch `scheduler.py` already does for the
regime engine). `opex_status` (normal/opex_week/post_opex) is a pure calendar
calculation (3rd-Friday-of-month rule) — no data source needed, so it was wired in
too even though it isn't strictly "breadth." If all 11 ETF fetches fail (e.g. no
network), it falls back to the old neutral placeholder values rather than crashing
the cycle. Coarser and noisier than a true market-internals feed, but it moves in
response to real price action instead of sitting frozen at 0.5/50/1.0 forever — a
meaningful upgrade for the MARKET_BREADTH bucket, which previously could never
qualify since every one of its inputs was static.

**Learning/Analytics backend** (`learning/`, `analytics/`) — the files themselves are
unchanged this pass (explicitly left alone per instruction), still fully working:
`pattern_database.py` (cosine similarity + recency decay), `ev_engine.py` (EV with
Wilson CI), `bayesian_updater.py` (gated weight-change proposals, never
auto-applied), `confidence_calibration.py`, `walk_forward.py` (rule attribution +
stability labeling), `champion_challenger.py` (two-proportion z-test promotion
gate), `analytics/` (confidence intervals, performance metrics, opportunity cost,
override tracking).

**Automated learning loop** (`engine/learning_loop.py`) — previously, running
`walk_forward.py` or checking on a champion/challenger test required a manual
Python-shell call; nothing ever triggered them. `scheduler.py` now calls
`maybe_run(db, cfg, mode)` once per cycle (cheap no-op almost every time — it just
checks a trigger condition) and does real work when either
`learning.walk_forward_trigger_trades` new closed patterns have accumulated or
`learning.walk_forward_trigger_days` days have passed since the last run, whichever
comes first. Each run: discovers every distinct rule tag that's actually appeared in
closed patterns' `rules_passed` (rather than a hardcoded list, so it stays correct
as the 6-bucket engine evolves), runs `run_walk_forward()` for attribution +
30/90/180-day stability labeling, re-evaluates any `champion_challenger` row still
in `'running'` status, and persists everything to a new `learning_runs` table
(`storage/database.py`) — visible in the UI's Learning tab (`/api/learning/runs`)
and via `db.get_recent_learning_runs()`. Note some rule tags bake in their
triggering value (e.g. `rsi_oversold_38`, `ad_ratio_0.82`) and will rarely repeat
exactly — `walk_forward.py`'s own `insufficient_data` flag correctly marks those as
not-yet-meaningful rather than this module fabricating false confidence.
**Not automated**: Bayesian weight-change proposals (`bayesian_updater.propose_update()`)
— that function needs a real "current weight" to propose changing, and the 6-bucket
engine's point values are hardcoded literals in `rules/swing_buy_rules.py`, not read
from `config.yaml`, so there's nothing live to target yet. Starting a
champion/challenger test (`ChampionChallenger.start_challenge()`) also stays a
deliberate manual action — this module only re-evaluates challenges someone already
started, it never starts one on its own. Nothing here ever calls `apply_update()`,
`promote()`, or `discard()` automatically.

**Pattern database wiring** (`engine/pattern_features.py` + `scheduler.py`) — every
BUY signal's feature snapshot is recorded (skipping tickers with an already-open
entry), now including real regime and bucket-score fields when the 6-bucket engine
ran. Each cycle auto-closes entries past `learning.pattern_hold_days` (default 5)
using a **simulated, time-based** outcome from that ticker's current price — not a
real fill, since this code never sees Robinhood order confirmations. `confirm_fill.py`
(below) overrides this with your real outcome when you use it.

**Position Management Engine** (`engine/stop_state_machine.py`,
`engine/position_health.py`, `engine/mae_mfe_engine.py`,
`engine/position_management.py`, `rules/exit_scorer.py`) — runs every cycle as
"Loop B" for every open position (opened via `confirm_fill.py`, never automatically):
- `rules/exit_scorer.py` — a continuous 0-100 exit score (stop proximity, momentum
  reversal, structure breakdown, earnings risk, VIX stress). Deliberately separate
  from `sell_rules.py`'s binary OR-logic trigger — the two answer different
  questions (how urgently vs. whether) and can disagree in the packet output.
- `engine/stop_state_machine.py` — 6-state stop machine (INITIAL_RISK →
  TRADE_CONFIRMING → BREAKEVEN → PROFIT_PROTECT → TREND_FOLLOWING, or
  THESIS_BROKEN on an exit-score spike). The stop only ever moves in the trade's
  favor.
- `engine/position_health.py` — an 8-component 0-100 health score (P&L trend, exit
  score, position EV, relative-strength trend, volume, AVWAP relationship, breadth,
  time decay) driving a hold/tighten/reduce/exit recommendation.
- `engine/mae_mfe_engine.py` — tracks max adverse/favorable excursion live, and
  flags when a position's drawdown looks anomalous vs. historical winners of the
  same setup_type/regime (needs ≥10 closed trades in `mae_mfe_data` before it says
  anything — a brand-new table, so expect "insufficient_history" for a while).
- Time stops and a 3-stage partial-exit framework, combined into a 10-priority exit
  recommendation per position.
- Rendered into `trade_prompt.md`'s new "POSITION MANAGEMENT" section by
  `engine/packet_builder.py`'s `build_position_action_packet()`.

**`confirm_fill.py`** — after you execute a buy or sell manually in Claude Desktop:

```bash
python3 confirm_fill.py buy NVDA 145.32 3.5      # ticker, fill price, shares
python3 confirm_fill.py sell NVDA 152.10          # ticker, fill price
python3 confirm_fill.py list                      # show open positions
```

A confirmed `buy` opens a real row in `positions` (linked to the most recent open
pattern_database entry for that ticker, if one exists — this also activates
`sell_rules.py`/Loop B for that ticker, which otherwise never runs), and now also
seeds the Position Management Engine's fields: `entry_signal_score` (from the
`signals` table), `entry_regime`/`setup_type` (from the linked pattern's feature
snapshot), and an initial stop/target at a 1.5%-of-price risk estimate (refined by
`stop_state_machine.py` on the next cycle). A confirmed `sell` closes the position,
computes real P&L, closes the linked pattern with your actual outcome (taking
priority over the time-based simulation if it hasn't already fired), records
MAE/MFE for future anomaly detection, and sets a re-entry cooldown
(`CONSERVATIVE`=48h / `MODERATE`=24h / `AGGRESSIVE`=12h / `TURBO`=6h, by
`risk_level`) that `hard_vetoes.py`'s COOLDOWN check enforces.

**Web UI** (`server.py`, `ui/index.html`) — FastAPI + WebSocket alternative to the
terminal dashboard. `python3 main.py --ui` serves `http://localhost:8080`; it does
**not** run the scan loop itself — run `python3 scheduler.py` as a separate process
(or use `./run.sh --ui`, which starts both). Read-only over SQLite/`config.yaml` plus
three safe write endpoints (`watchlist`/`trading.mode`/`risk_level` edits, and the
kill switch — both gated behind `config.yaml`'s `ui.auth_token`, default
`"change-me"`, **change this before exposing the server beyond localhost**). The
`/ws` endpoint pushes a full-state snapshot on connect, plus live event pushes (see
"Real-time push + notifications" below) so a tab doesn't need to be
reconnected/refreshed to see fresh state anymore. Market
pulse (F&G/VIX) is parsed from the same log-line regex trick the terminal dashboard
already used; A/D ratio and McClellan are called directly from
`engine/market_breadth.py` (independently cached, doesn't need `scheduler.py` to
have run a cycle first) — real sector-ETF-proxy numbers, not placeholders.

UI redesign: sidebar navigation, a card-based Control tab (chip-based watchlist
editor, risk-level cards showing real thresholds from `config.yaml`, segmented
mode/watch-execute toggles, a custom in-page token modal instead of the browser's
native `prompt()`), toast notifications on save, and a market-closed banner (backed
by a new `/api/status` endpoint — `is_market_open()`/`load_config()` imported from
`scheduler.py` without starting its scheduler, plus `db.get_last_cycle()`) so the
dashboard explains *why* it's empty outside market hours instead of just looking
broken. Monitor tab now shows real scheduler status instead of being empty.

**Signal rule/bucket breakdown** (`storage/database.py`'s `signals` table +
`ui/index.html`'s Signals tab) — previously the only place to see WHY a signal
scored what it did was `output/trade_prompt.md` (a file, not the UI), and the
Signals tab just showed the final score. `log_signal()` now also persists
`rules_fired`/`rules_failed`/`bucket_scores` JSON (from
`rules/swing_buy_rules.py`'s `SwingScoreResult`, when the ticker was actually
scored — not vetoed, not already an open position). Each row in the Signals tab
is now expandable: BUY/HOLD rows show all 6 buckets with points/max, qualified
status, and which sub-rules fired in each; vetoed or already-open rows show the
single reason (`rules/hard_vetoes.py`'s veto code, or "ALREADY_OPEN"); SELL rows
show the one rule that triggered (`rules/sell_rules.py` is first-triggered-wins
OR logic, not a weighted score, so a single triggered rule + reason is the
complete, honest explanation there — there's no hidden multi-factor breakdown to
surface). Also fixed the Copy Prompt button, which called an undefined
`copyPrompt()` function and silently did nothing.

> **Superseded by §47.5 (Phase 3).** The button no longer posts to
> `/api/prompt/copy`; that endpoint is gone. It ran `pbcopy` on the *server*,
> which assumed the server process and the person clicking were on the same
> machine — already false over an SSH tunnel, and definitively false once the
> UI runs in a container. The browser now copies with `navigator.clipboard`,
> and an Open button fetches `/api/prompt/raw` into a new tab. Both work on
> every OS, remotely, and from a phone.

**Real-time push + notifications** (`storage/database.py`'s `ui_events`/`latest_regime`
tables, `server.py`, `engine/notifications.py`) — `scheduler.py` and `server.py` run
as separate OS processes with no shared memory, so `server.py`'s in-memory
`broadcast()` couldn't be triggered by `scheduler.py` directly (previously the `/ws`
connection only ever pushed once, on connect). Fixed with a DB-backed outbox instead
of a new dependency: `scheduler.py` writes a row to `ui_events` after a
high-conviction BUY signal (`notifications.high_conviction_buy_pct`, default 80%),
an urgent Loop B exit (`priority_action.urgent`, i.e. exit score >= 90 — "THESIS
BROKEN"), and every cycle's completion; `server.py` runs a background task
(`_event_poll_loop`, polling every `EVENT_POLL_SECONDS` = 3s) that broadcasts new
rows to every connected `/ws` client. The UI shows a toast for buy/exit events and
calls the new `/api/state` endpoint to refresh in place after a `cycle_complete`
event, instead of requiring a manual reconnect. Same fix applied to the regime pill,
which was silently broken before this pass: `engine/regime_engine.py`'s
`current_state()` is a same-process-only singleton, so `server.py` (which never
calls `calculate()` itself) always saw `None` even while `scheduler.py` had a real
regime running for hours — `scheduler.py` now also persists the latest regime to a
`latest_regime` table each cycle, and `server.py` reads from there instead.
Desktop notifications (`engine/notifications.py`) originally used macOS's
`osascript` and silently no-opped everywhere else.

> **Superseded by §43.3 and §47.3 (Phase 3).** Notifications are now an ordered
> chain of transports — `desktop` (osascript / notify-send / win10toast), then
> `webhook` (ntfy, Slack, Discord, Pushover — anything accepting a POST), then
> `log`, which is last and *always* succeeds, so a notification is never
> silently dropped. Configure with `notifications.transports` and
> `notifications.webhook_url`. Under the §47 architecture the containerised
> engine does not notify at all: it writes a `notify` row to `ui_events` and
> the native host agent (`scripts/tp_agent.py`) delivers it, because a
> container has no route to a notification centre.

Complemented by the
browser's own Notification API on the UI side (fires only when the tab is open but
not focused, to avoid double-alerting on top of the toast). Both are gated by
`config.yaml`'s new `notifications:` section (`enabled`, `desktop_enabled`,
`high_conviction_buy_pct`). Latency is bounded by the poll interval (~3s), not true
push — acceptable for a 30-minute scan cadence, and avoids adding Redis/a message
queue for a single-user local tool.

**Transaction indicator/rule snapshots** (`storage/database.py`'s `trade_snapshots` +
`trades.snapshot_id`, `confirm_fill.py`) — `trade_snapshots` existed from early in the
build but nothing ever called `save_trade_snapshot()` until now. `confirm_fill.py`'s
`cmd_buy`/`cmd_sell` now capture a snapshot at confirmation time: for a buy, the
linked pattern's full feature snapshot (RSI/MACD/SMAs/regime/bucket scores at
signal time) plus the most recent `signals` row's rule breakdown; for a sell,
entry vs. exit P&L plus the most recent `signals` row (since `confirm_fill.py`
makes no MCP calls by design, there's no live indicator fetch at sell time — the
last scan cycle's data is the most recent real data available) and the original
entry pattern's features for reference. The UI's Journal tab (`ui/index.html`) is
now expandable per transaction to show this, same pattern as the Signals tab.

**Ticker validation + company names** (`mcp_clients/yfinance_mcp.py`'s
`get_ticker_info()`, `storage/database.py`'s `ticker_info_cache`, `server.py`'s
`/api/ticker/validate` and `/api/ticker/names`) — adding a ticker to the watchlist
now calls a live `yfinance_get_ticker_info` lookup first (the one deliberate
exception to `server.py` otherwise never touching an MCP - see its module
docstring) and rejects typos/delisted symbols rather than silently adding them.
Company names are cached both there and opportunistically every scan cycle
(`engine/ticker_analyzer.py` already fetches yfinance info for every watchlist
ticker - extracting `longName`/`shortName` costs zero extra calls), powering
hover tooltips wherever a ticker is shown in the UI.

**Strategy tab** (`engine/rules_catalog.py`, `server.py`'s `/api/strategy`,
`ui/index.html`) — a new tab documenting the actual current rule set: all 6 buy
buckets with their weights/points/sub-rules (tagged REAL/PROXY/PLACEHOLDER, same
convention as `engine/ticker_data_adapter.py`), the dynamic-threshold formula, all
hard vetoes, and sell rules read live from `config.yaml`. `rules_catalog.py` is a
maintained catalog, not runtime introspection - it must be updated by hand if the
rule modules change (same trust model as `scheduler.py`'s
`NYSE_HOLIDAYS_2026` comment or `bayesian_updater.py`'s `BUCKET_WEIGHT_BOUNDS`,
which also separately hardcodes the bucket names/weights). Building it surfaced a
pre-existing quirk worth knowing about: in the TREND, MOMENTUM, and EXTERNAL
buckets, the sum of every independent rule's points is actually higher than that
bucket's declared `max_points` ceiling in `rules/swing_buy_rules.py` (e.g. TREND's
8 rules sum to 71pts against a declared 55pt max) - meaning a bucket's score can
mathematically exceed 100% of its own ceiling if enough rules fire at once. This
wasn't introduced or fixed here (fixing it would change live scoring behavior,
which wasn't asked for) - just flagged. The tab also shows how the strategy has
evolved: recent walk-forward learning runs, Bayesian weight-change history
(empty today - see `engine/learning_loop.py`'s docstring on why), and any
champion/challenger test history.

**Ad-hoc / manual cycle run** (`scheduler.py`'s `run_cycle(force=True)`,
`server.py`'s `POST /api/cycle/run_now`, `ui/index.html`'s "Run cycle now" button
on the market-closed banner and the Monitor tab) — the schedule (Mon-Fri 9:30-16:00
ET) is still how cycles normally run, but you can now also trigger one on demand,
market open or not - useful for testing, or for populating the dashboard without
waiting for the next window. `force=True` skips ONLY the `is_market_open()` gate;
kill switch and risk limits are still enforced, and if the market is genuinely
closed you'll get real but stale (after-hours/last-close) quotes, not an error - the
UI's warning makes that explicit. Guarded against a double-click (or overlapping
the real scheduler.py process's own scheduled run) by an in-process
`threading.Lock` in `server.py` - a second request while one's already running
gets a clean 409 rather than a duplicate run; this doesn't lock across the two
separate `scheduler.py`/`server.py` processes, which was judged low-risk (worst
case: a duplicate `signals` row, not a bad trade) rather than worth building
cross-process locking for. A new `cycles.triggered_by` column
(`storage/database.py`) records "scheduler" vs "manual" so the Monitor tab's Last
Cycle panel shows which one produced any given cycle. Completion is reported
through the same `cycle_complete` `ui_events`/WebSocket push every other cycle
already uses - no new plumbing needed there.

**ET timezone display fix** — the market-closed banner and Monitor tab were
computing the correct next-open time in Eastern time on the backend, but
formatting it in the browser with `toLocaleString()` and no `timeZone` option,
which silently renders in the *browser's local* timezone while the surrounding
text still says "ET". Anyone not actually in Eastern time (e.g. Central) would
see a real 10:00am ET value displayed as "9:00 AM" - still labeled ET. Fixed by
passing `timeZone: "America/New_York"` explicitly everywhere `next_open_et`/
`server_time_et` are formatted.

**Hover tooltips / glossary** (`ui/index.html`'s `TIPS` object + `withTip()` helper) —
every nav tab and most stat/field labels across the UI now have a native hover
tooltip explaining what the field means and, where one meaningfully exists, the
expected value range (e.g. RSI "below 30 = oversold", VIX "above 27 = high
stress"). `TIPS` is a single glossary object rather than inline strings scattered
across every render function, so it's one place to update if wording drifts from
the code. Same mechanism `tickerLabel()` already used for company-name-on-hover -
a native `title` attribute, with a `.hint` CSS class (dotted underline) as the
visual cue that a field has more info on hover. Covers: all 10 nav tabs, the
top-bar Regime/Health/Mode/Watch badges (the Regime badge's format - `DOMINANT ·
bull%/bear%/choppy%` - was previously unexplained anywhere in the UI), Dashboard
stats, the Positions table headers, Risk tab, Monitor tab, Control tab risk-level
cards, the shared bucket-score breakdown (Signals/Journal/Strategy tabs), and the
Journal detail view's key-indicators grid. Also fixed a stale claim while in
there: the Dashboard's Market Pulse panel said "A/D ratio & McClellan are
placeholders" - no longer true since `engine/market_breadth.py` was wired up
earlier this session.

**Original terminal dashboard** (`main.py`, no flag) — unchanged, still works.

## Explicitly NOT built (deferred, not started)

- **10-step decision framework / hard-veto framework beyond the 15 implemented** —
  only the 15 vetoes in `rules/hard_vetoes.py` exist; the fuller "4-layer/10-step"
  framework from earlier spec messages was never built as a distinct orchestration
  layer (its pieces — vetoes, scoring, thresholds — exist, just not as one named
  10-step function).
- **Confidence decay, re-entry engine (beyond the cooldown gate), immutable trade
  snapshots, news classification engine, strategy health score (11-metric version)**
  — none of these were part of the 4-phase incremental spec and weren't built.
  `db.get_latest_health_score()` exists but is a much smaller thing: the average of
  `position_health.py`'s per-position score, not the original spec's 11-metric
  strategy-level score.
- **Portfolio heat** — `db.get_portfolio_heat()` is a rough approximation (open
  positions' dollar risk vs. dollars deployed), not normalized against real account
  equity, since no Robinhood balance API is called from Python by design.
- **9-screen React UI** — `ui/index.html` is a single vanilla-JS/HTML file with 9
  tabs, not a React build. Matches the "no build step" requirement, not the original
  React spec.
- **NVIDIA NIM model fallback cascade** (`engine/claude_brain.py`) — still not wired
  into the active `scheduler.py` flow. The current pipeline doesn't call any LLM
  automatically at all.
- **PatternDatabase regime/bucket features are placeholders where their source data
  is** — see "Honesty note on inputs" above; ADX, CMF, market breadth, anchored
  VWAP, industry RS, unusual options, VIX percentile, and sector RS are still 0.0/
  neutral defaults throughout the codebase, not just in the pattern database.

## 2026-07-15: Zero-trades-in-a-bull-market recalibration

A full audit of 810 historic signals (100% HOLD, best score ever 58.6% vs
thresholds of 65-85%) found the bar was mathematically unreachable. Root
causes and fixes, all verified against the historic DB and synthetic tests:

- **EV cold-start penalty removed** (`rules/dynamic_thresholds.py`): with an
  empty pattern DB, ev_pct=0 hit the "+5% harder" branch on every one of the
  239 scored signals — no evidence was treated as bad evidence. Now the EV
  adjustment is strictly 0 unless an EV was actually *measured* (new
  `ev_measured` flag), and penalizes only measured-negative EV.
- **Regime bull credit** (`engine/regime_engine.py`): `regime_threshold_adj`
  could only raise the bar (choppy% alone added +3 even in a confirmed BULL).
  Bull dominance now earns a credit (floor -5); `dynamic_thresholds.py`'s
  `max(regime, vix)` was also fixed so a calm VIX (adj 0) no longer swallows
  the negative credit. Threshold floor lowered 55→50. Calendar Mon+3/Wed-3
  removed (no empirical basis); Fri+5 and OpEx kept.
- **Dual-path momentum scoring** (`rules/swing_buy_rules.py`): RSI/stoch/
  Bollinger rules paid only for *oversold* readings — 25 raw points that
  bull-market leaders (RSI 55-70, riding the upper band) structurally could
  never earn. Each now has a pullback path (full points) OR a
  momentum-zone path (partial points); overbought still earns 0.
- **Placeholder points removed from denominators**: EXTERNAL counted 16
  unreachable pts (unusual options, estimate revisions — qualified in 2% of
  historic signals), VOLUME_PA 4 (POC). max_points now reflect achievable
  sums (TREND got this fix earlier; these didn't).
- **VOLATILITY_EXPANSION is now a true bonus**: its 7% composite weight
  capped every non-squeeze stock at 93% max. Weight redistributed across the
  6 decision buckets (sums to 1.0); the bucket now adds up to +4 pts on top.
- **New ACCUMULATION signals** (all REAL, same daily OHLCV bars, zero extra
  MCP calls): OBV 20d-high/divergence (quiet accumulation), 20d-vs-50d
  dollar-volume expansion, consecutive accumulation days, and per-ticker
  relative strength vs SPY (TREND bucket, 6 pts) — the "emerging leader"
  signals the audit's pre-rally analysis called for.
- **Data-ops fixes**: 337/810 signals died on total yfinance OHLCV failure —
  `mcp_clients/base.py` now retries once with jittered backoff; daily
  history fetch widened 3mo→1y so SMA200 (15 pts) is actually computable.
- F&G "optimal" range widened 35-65 → 35-75 (healthy bulls sit 65-75).
- AVWAP swing-low bounce band widened 0.5%→1.5% (never fired historically).

Post-fix calibration: strong bull leader scores ~77% vs a ~51-55% bull
threshold (BUY); weak/choppy setups score ~20% vs ~61-74% (HOLD). Thresholds
by regime: clean bull 51%, current 55%, choppy 61%, bear+VIX 68%.

## 2026-07-15 (round 2): data-source resilience + mode/risk recalibration

After the scoring recalibration, 149 fresh signals STILL all read HOLD (best
51.4% vs 62.1%). Root cause this time was **data, not scoring**: finviz,
maverick, stock-scanner and yfinance-news all went dark on 2026-07-14 — the
day the screener began pushing 38-70 candidates per cycle — so EXTERNAL
scored 5/45 (only the always-true default) and news sentiment was pinned at
its neutral default on 149/149 signals. Every dead call also burned its full
45s timeout per ticker, which is what ballooned cycle time to 220s+.

- **Circuit breakers + per-source caches** (`mcp_clients/`): finviz/scanner
  cached 6h per ticker (ratings/insider data churn daily at most), maverick
  10 min; after 3 consecutive failures a source is skipped for 5-15 min
  instead of timing out per ticker. Finviz gets a 2-slot semaphore (it's a
  scraper — the screener's call volume was ban-bait).
- **Maverick availability re-checked every 5 min** — it was checked ONCE at
  process start, so a Maverick started after the scheduler was treated as
  down forever (matches "it's running but never fires").
- **News shape hardening** (`ticker_analyzer._parse_yfinance`): headlines
  are now extracted tolerantly (title/Title/headline/content.title, list or
  dict wrappers) — they were silently lost on any non-canonical shape, which
  is also why the News tab's `news_items` table stayed empty.
- **Time-normalized RVOL** (`ticker_data_adapter`): volume_ratio compares
  volume-so-far to the FULL-DAY average, so every midday scan read "weak
  volume" structurally (VOLUME_PA qualified in 6.7% of signals). Now divided
  by the elapsed session fraction.
- **Mode rules**: DAY mode adds +3% to the buy threshold (same-day round
  trip pays the spread twice); HYBRID scores through the swing engine,
  scans at the day interval, and takes the swing bar. Day-mode screener
  quality gate now enforces the same 2M avg-volume floor as the hard veto.
- **Risk levels**: base thresholds now CONSERVATIVE 68 / MODERATE 60 /
  AGGRESSIVE 55 / TURBO 50 (TURBO was identical to AGGRESSIVE before), and
  the screener's candidate cap scales by risk level (x0.6/x0.8/x1.0/x1.25)
  so pre-selection behaves like the risk profile it feeds.
- **Performance**: option-chain fetch skipped for non-watchlist candidates
  (display-only data), `cycle_max_parallel_tickers` 4→6, and the dead
  modules (`engine/executor.py`, `engine/rules_engine.py`,
  `engine/claude_brain.py`, `rules/buy_rules.py`, `data/`) are deleted.

Verified: replaying yesterday's best real signal (WFC 51.4% HOLD) with data
sources restored under the new rules scores 76.2% vs the same 62.1%
threshold -> BUY.

## 2026-07-15 (round 3): external-review adoption (rule engine 8.4.0)

An external model review was assessed point-by-point. Adopted (with tests in
`tests/test_scoring_sanity.py`):

- **Docs contradiction fixed**: module docstring + Strategy catalog claimed
  "below min_pct contributes ZERO" while code used the continuous soft
  multiplier. The code was right; the text now describes the anchor-table
  curve definitively. (The reviewer's proposed ramp formula was NOT adopted -
  it reintroduces a cliff at 60% of the bar and full credit at the bar; the
  existing continuous curve is strictly better.)
- **Default-true placeholder zeroed**: `no_recent_downgrade` (fired 100% of
  signals, 5 free pts) → 0 pts. EXTERNAL max 45→40.
- **Correlated-evidence subgroup caps**: trend-structure family (SMA stack/
  EMA/weekly, 48 raw pts) capped at 38; MACD family (cross/histogram/
  persistence, 23) capped at 18; volume/accumulation family (OBV/CMF/
  dollar-vol/accum-days, 27) capped at 20. One latent condition (a broad
  beta rally) can no longer be counted 3-5x. Bucket maxes: TREND 59,
  MOMENTUM 35, VOLUME_PA 48.
- **Sector-RS deduplicated**: SENTIMENT_MACRO's sector_rs_1m (6 pts) zeroed -
  it was the exact same number as EXTERNAL's industry_rs_positive (13 pts).
  SENTIMENT_MACRO max 40→34.
- **Breadth authority capped**: threshold breadth adjustment now maxes at +8
  (was +15) - breadth already has a full scoring route via its bucket; the
  true-panic block lives in market_filters' multi-signal gate.
- **Calendar log-only**: Fri/OpEx adjustments are computed and shown in every
  breakdown but NOT applied until proven (config `thresholds.calendar_enabled`).
- **data_coverage per signal**: every scored signal's threshold_breakdown now
  carries completeness %, stale indicators, and which external sources were
  actually seen - a buy is explainable as "real data vs degraded", not just
  a score.
- **Sanity tests**: `tests/test_scoring_sanity.py` proves every bucket's
  max_points is exactly achievable (maxed-out setup earns 100% of every
  denominator), junk scores <10, day mode is +3, calendar stays log-only.

Deliberately deferred (need closed-trade history / infra first, in order):
barrier-based outcome labels (target-before-stop instead of 5-day return),
the 8-case replay regression suite, per-mode (DAY/SWING) model calibration,
score-band→probability calibration, ablation runs per evidence family, and
Bayesian weight changes (already gated off until enough trades exist).

Post-change calibration: breakout leader 77.6% / pullback 67.7% / choppy-
regime leader 77.3% → BUY vs 54-57% bars; weak junk 17.4% vs 65.1% → HOLD.

## 2026-07-15 (round 4): real market-data providers + the Maverick argument bug

**Maverick root cause found and fixed** (from its own server logs): every
call was being REJECTED with "Missing required argument: 'ticker' /
Unexpected keyword argument: 'symbol'" - the server was up and reachable the
whole time, but `mcp_clients/maverick.py` sent `{"symbol": ...}` while the
tools expect `{"ticker": ...}`. Also removed the trailing-slash URL that
307-redirected every request. Maverick data should now actually arrive for
the first time since the parallel-scan era began.

**Multi-provider market-data layer** (`mcp_clients/market_data.py`) - per
the provider assessment: yfinance is scraper-grade and was the #1 failure
source, so it's demoted to non-critical fallback whenever real providers are
configured. All key-gated via `.env` (see `.env.template`); with no keys,
behavior is exactly as before:

- **Alpaca** (`ALPACA_API_KEY`/`ALPACA_API_SECRET`) - primary: real-time IEX
  snapshot (price + real two-sided bid/ask - kills the false SPREAD_WIDE
  vetoes from Yahoo's one-sided quotes), 1y daily bars, 5-min intraday bars
  for VWAP. When healthy, the yfinance price-history calls are skipped
  entirely.
- **Finnhub** (`FINNHUB_API_KEY`) - quotes backup + real dated company news
  (feeds the News tab and the sentiment scoring that was pinned at neutral).
- **Tiingo** (`TIINGO_API_KEY`) - EOD bars fallback + IEX quote backup.
- **Twelve Data** (`TWELVEDATA_API_KEY`) - last resort only (8 credits/min).
- **Marketstack** - assessed and rejected (100 requests/month is unusable
  for scanning).

Every provider has its own circuit breaker + free-tier rate limiter. Each
signal's `data_coverage.providers` now records which provider served
quote/bars/news, so a decision is auditable as "Alpaca bars + Finnhub news"
vs "yfinance fallback". Recommended: create free Alpaca + Finnhub keys and
put them in `.env` - those two alone cover quotes, bars, VWAP, and news.

## 2026-07-15 (round 5): deep-review adoption, cycle control, two-phase scoring (rule engine 8.5.0)

A second, deeper external review was assessed point-by-point alongside live
production evidence (207 fresh signals, all HOLD, best 53.1% vs a 57.1% bar,
EXTERNAL bucket at literal 0/40 on every one). All verified by the expanded
`tests/test_scoring_sanity.py` (8 tests, including the review's invariants:
rule-fire monotonicity, bucket independence, denominator achievability).

**Adopted:**
- **UNKNOWN != FALSE — bucket availability** (the review's data-provenance
  principle, and the direct fix for the live deadlock): when every EXTERNAL
  source (finviz/analyst/maverick) is down, that is a data outage, not
  bearish evidence. 75% of the bucket's 16% weight is redistributed pro-rata
  to available buckets; 25% is deliberately left dead (missing evidence
  still costs something). Fully audited per signal in
  `data_coverage.unavailable_buckets` + the breakdown string. Replay of the
  live BABA case: 52.6% HOLD -> 66.5% BUY with sources dark; the same stock
  with sources UP but genuinely negative scores lower (58.1%) - order
  preserved, as it must be.
- **Cycle kill + watchdog** (Trinath's ask): `POST /api/cycle/cancel` sets a
  cross-process flag; scheduler.py aborts all not-yet-started tickers
  between completions. `trading.max_cycle_duration_minutes` (20) is now
  actually ENFORCED the same way - it previously existed but did nothing,
  which is how cycles ran 20+ minutes.
- **Two-phase scoring** (the cycle-runtime fix): screener candidates get a
  LITE pass (bars/quote/indicators only - no maverick/finviz/scanner/news,
  which were ~11 MCP calls per candidate); only candidates within 8 pts of
  the buy bar earn the full fetch + rescore. Typically 2-5 promotions per
  cycle. Expected cycle time: from 330-400s toward ~90-150s.
- **Sector from yfinance info**: sector previously came ONLY from finviz, so
  when finviz died every sector-RS signal (13+8 pts) silently died with it
  (0 fires in 207 signals). Now falls back to yfinance's own sector field.
- **Trend-integrity pullback guard**: oversold RSI below a broken SMA50 is a
  falling knife, not a pullback - earns 4 pts, not 12.
- **ADX direction confirmation**: `adx_trending_bullish` = ADX>25 AND
  +DI > -DI (ADX alone is direction-blind; a crashing stock also has ADX 40).
- **True weekly resample**: weekly_trend_aligned now uses real 5-bar
  trading-week closes from the 1y history (>=20 weeks for SMA20, >=50 for
  SMA50), daily proxy only as labeled fallback.
- **ATR-normalized AVWAP band**: "near AVWAP" = within 0.5xATR (floor 0.5%,
  cap 2.5%) instead of a fixed 1.5% that means different things across
  volatility regimes.
- **ATR-aware initial stop** (`confirm_fill.py`): risk/share =
  max(1.2xATR, 1.5%) capped at the risk level's stop %, replacing flat 1.5%.
- **Screener sector-diversity cap**: max 30% of the shortlist (min 3) from
  one sector - a candidate count cap alone doesn't stop a single-theme
  cluster. Unknown sectors exempt (same UNKNOWN principle).
- **Screener learning gate softened**: min_track_record 5 -> 12 cycles
  before a ticker can be excluded as "low quality" (5 cycles isn't a
  statistical history).
- **Latent-factor ledger**: every signal logs raw-vs-capped points per
  correlated-evidence family (`threshold_result.latent_factors`) so cap
  bite can be measured before further tightening.
- **Terminology**: catalog now states max_points are EFFECTIVE CAPPED maxima
  (raw sums documented: TREND 69, MOMENTUM 40, VOLUME_PA 55).
- Timeouts tightened: per-MCP-call hard ceiling 45s->30s, run_async 60s->40s
  (circuit breakers now own the repeated-failure case).

**Rejected, with reasons:**
- The review's proposed qualification formula (zero below 0.6xmin_pct, ramp
  to the anchor curve at min_pct): re-introduces the exact cliff class it
  criticizes - a 1-pt data wobble at 0.6xmin_pct would flip a bucket between
  0 and ~35% credit. The existing everywhere-continuous anchor curve is
  strictly smoother; kept, and its semantics are now stated identically in
  code, catalog, and this README.
- Full TRUE/FALSE/UNKNOWN/STALE/SUSPECT per-rule state enum: the two
  highest-value slices are implemented (bucket availability + the existing
  stale-indicator circuit breaker + quote plausibility guard); a per-rule
  five-state model across ~45 rules is heavy machinery with no consumer yet.
- Percentile-calibrated breadth thresholds, score->probability calibration,
  purged walk-forward promotion gates, SEC Form-4 ingestion, licensed quote
  feeds: all correct as the NEXT phase, all blocked on the same thing -
  closed-trade history that doesn't exist yet. The system already complies
  with the review's core governance demand (watch/paper mode, manual
  execution, Bayesian auto-updates disabled, challenger promotion manual).

**Documentation policy** (per Trinath's ask): this README is the living
change log - every functional/rule/strategy change lands here AND in the
Strategy tab's catalog (`engine/rules_catalog.py`) in the same pass. The
`Trading_Platform_Complete_Reference*.docx` files predate all 2026-07-15
rounds and should be treated as historical.

## 2026-07-15 (round 6): Data Sources health panel

Trinath: "show me which MCPs are active and which have issues." New
**Data Sources** panel on the Monitor tab - one row per MCP/API (yfinance,
maverick, finviz, stock-scanner, alpaca, finnhub, tiingo, twelvedata) with a
live status derived from each source's own health reports:

- **OK** - recent success, circuit breaker closed
- **DEGRADED** - consecutive failures accumulating, breaker still closed
- **DOWN** - breaker open (shows the last error and when it retries) or
  repeated failures; maverick additionally distinguishes "localhost:8003
  unreachable" from "reachable but erroring"
- **NOT_CONFIGURED** - optional provider with no API key in `.env` (the row
  tells you which key activates it)
- **NO_DATA_YET** - nothing has reported since the last restart

Plumbing: every `SourceCircuitBreaker.record()` (and a throttled yfinance
reporter in ticker_analyzer, since yfinance has no breaker) persists to a new
`source_health` table; `GET /api/sources` merges that with provider key
configuration; the panel refreshes on demand. Cross-process by design - the
scheduler writes, the web server reads. Note: an "as far as I know all my
MCPs work" belief is exactly what this panel exists to verify - the Maverick
symbol/ticker argument bug ran invisibly for days while the server was
perfectly healthy.

## 2026-07-15 (round 7): third external review - validation + 4 real catches

This review was mostly a validation pass on rounds 5-6 (ADX direction,
weekly resample, ATR-AVWAP, bucket availability, caps, log-only calendar all
independently endorsed). Four genuinely new items were valid and are fixed:

- **Stale qualification text, final instance**: the Strategy catalog's
  `note` field still described the retired "0 at 60% of min_qualify_pct"
  ramp. Now states the anchor-table semantics identically to code/README,
  and a new unit test pins the curve itself: monotonically non-decreasing,
  continuous everywhere, and min_qualify_pct provably has zero effect on
  the multiplier (presentation-only).
- **VIX entry/exit contradiction**: TURBO could enter at VIX 32 while the
  hard vix_spike exit fires at 28 - buy a stock, get force-exited by the
  same reading hours later. Entry maxes realigned with a >=1-2pt buffer
  below the 28 exit: CONSERVATIVE 22 / MODERATE 25 / AGGRESSIVE 26 /
  TURBO 27; market_filters no_trade_above 30 -> 28.
- **Outage hygiene for learning**: outage-adjusted scores (EXTERNAL weight
  redistributed) are now excluded from the screener's qualify-rate
  statistics, and every pattern-DB snapshot carries an `external_outage`
  flag so future EV/similarity cohorts can filter them. data_coverage also
  gains a three-state `external_state` label
  (outage / available_negative / available_positive) - "sources down" and
  "sources say no" are different facts.
- **Exit-score duplicate authority removed**: vix_spike>=28 and
  earnings<=2d are hard exits in sell_rules.py; they no longer earn Exit
  Score points (kept as labeled informational entries). Earnings 3-4 days
  out still scores 6 - genuinely earlier warning, not a duplicate.
  MARKET_CONTEXT max 32->24, FUNDAMENTAL_RISK 25->21 (achievable sums).
- Breadth fields now carry explicit provenance labels
  (`breadth_proxy_type: sector_etf_proxy`, `coverage: 11`) so proxy breadth
  can never be conflated with true exchange-level A/D internals.

Review items acknowledged but deferred unchanged (all require closed-trade
history, all already documented as next-phase): AVWAP 0.25-0.75x ATR grid
calibration, EV confidence bounds, exit-band outcome calibration, point-in-
time vendor-rating snapshots, purged cross-validation for challengers.

## 2026-07-15 (round 8): no-Finviz-Elite fallback chain

The local finviz-mcp-server (tradermonty/finviz-mcp-server) is built for a
PAID Finviz Elite subscription and its install was found missing entirely.
Rather than depend on a paid scraper, EXTERNAL's key fields now have free,
official-API fallback chains:

- **Analyst consensus**: finviz -> yfinance `recommendationKey` (free, in
  the info payload already fetched) -> Finnhub free
  `/stock/recommendation` monthly Buy/Hold/Sell counts (6h-cached, fetched
  only on full analysis when cheaper sources are empty). Recorded in
  `data_sources.analyst`.
- **Short float**: finviz -> yfinance `shortPercentOfFloat`.
- **Sector**: already yfinance-first (round 5).
- finviz, when installed and healthy, still overrides these (it can no
  longer clobber them with N/A when empty).

Net effect: without finviz at all, EXTERNAL retains analyst (5) +
industry RS (13) + maverick (12) + short-float-driven SENTIMENT points -
only the finviz technical rating (10 pts) has no free equivalent, which is
acceptable: it's a redundant third-party read of technicals this engine
already computes itself. Installing finviz-mcp-server is now OPTIONAL.

## 2026-07-15b: finviz-mcp-server replaced with the `finviz` pip package

The stdio `finviz-mcp-server` binary (FINVIZ_MCP_PATH) referenced in round 8
above turned out to be unbuildable/unavailable on this machine - every call
was failing the "server binary not found" check, so finviz had actually been
dark in production regardless of the no-Finviz-Elite fallback chain work.

`mcp_clients/finviz_mcp.py` now scrapes finviz.com directly via the `finviz`
PyPI package (github.com/mariostoev/finviz, `pip install finviz`, added to
requirements.txt) instead of shelling out to a separate server process. No
API key, no binary to build, no FINVIZ_MCP_PATH env var needed anymore -
`FinvizMCP().get_fundamentals(ticker)` keeps the exact same return shape
callers already expect. Same 6h cache / 2-call semaphore / 3-strike circuit
breaker as before (still the right call for a scraper-backed source hit by
up to 70 candidates/cycle).

Two known gaps in this package version, confirmed against live finviz.com
before wiring it in (see the module docstring for detail):
- Sector/Industry/Country come back empty (its CSS selectors don't match
  finviz's current quote-links markup) - harmless, since ticker_analyzer.py
  already falls back to the yfinance-sourced sector whenever finviz's value
  is empty/"N/A".
- There is no scrapeable "technical rating" field (it's a JS-rendered
  TradingView gauge on finviz.com, not in the HTML at all) - `technical_
  rating` is now a computed Buy/Hold/Sell derived from finviz's own SMA20/
  50/200 and RSI(14) fields (`_derive_technical_rating()`), not a
  finviz-native value. `analyst_rating` is still finviz's real `Recom`
  field (1.0-5.0 analyst consensus), mapped to the same Strong Buy...Strong
  Sell labels the rest of the pipeline expects.

## 2026-07-15c: finviz_screen wired up (Screener column-misalignment fixed)

The `finviz.Screener` class (market-wide screening) was initially evaluated
and rejected above because its column output came back visibly misaligned
on a live test query (Ticker/Company/Sector columns shifted). Root cause,
found by diffing the raw HTML against the parsed output: finviz's Ticker
cell renders a company-logo `<a>` with a one-letter text fallback (e.g.
`<span>N</span>`) BEFORE the real `<a class="tab-link">NVVE</a>` ticker
link, both inside the same `<td>`. The pip package's row parser
(`scraper_functions.get_table()`) builds each row with
`column.xpath("td//text()")` - a flat, row-level text-node scrape with no
per-cell boundary - so that one `<td>` contributes 2 text nodes instead of
1, shifting every `zip(headers, row_data)` pairing after it by one position
for the rest of the row.

Fixed by monkeypatching `get_table()` (in-process only, upstream package on
disk is untouched) with a version that extracts one string per `<td>` via
`text_content()` - exactly how the package's own header parser already
works - and prefers the real `a.tab-link` text over the logo-fallback span
specifically in the Ticker cell. See
`mcp_clients/finviz_screen.py`'s `_patched_get_table()` docstring for the
full detail. Verified against live finviz.com on both the Overview and
Technical tables: every column, including Ticker, now comes back correctly
keyed.

With that fixed, `engine/screener.py`'s `finviz_screen` source is now REAL
(`_screen_finviz()`), using finviz's `ta_newhigh` signal (new 52-week
highs) - a price-structure breakout signal none of the other
yfinance-backed screener sources (all same-day %change/volume) can query
for directly. Enabled by default, priority 4, quota 2, 10-min cache, same
circuit-breaker/timeout hardening as finviz_mcp.py.

## 2026-07-15 (round 9): instrumentation phase (fourth external review)

The fourth review's headline - "don't change the model now; lock invariants,
make outages traceable, and pre-wire the learning phase" - matches this
project's current stance. Most of its section 1-6 asks were already
implemented in rounds 5-7 (it reviewed a slightly older snapshot): VIX
entry/exit alignment, exit-score duplicate-authority removal, three-state
external labels, outage exclusion from learning, per-source health state,
sector failover hierarchy, stale-veto introspection. Newly added this round
(12/12 tests pass):

- **Cumulative-evidence monotonicity harness**: a test that adds TREND
  evidence one rule at a time and asserts the composite never decreases -
  the structure cap may flatten additional evidence, never invert it. Plus
  a test pinning that a configured challenger can never alter the champion.
- **Challenger shadow harness** (the review's "simple challenger harness"):
  define `weights.swing_buy_challenger.bucket_weights` in config.yaml
  (commented example included) and every signal is ALSO re-weighted under
  that profile - same bucket points, caps, qual_mult, availability logic
  (with its own redistribution scale) - and logged side-by-side in
  `threshold_breakdown.challenger` with would_buy/agrees_with_champion.
  Never acted on. Zero extra data fetches.
- **Outage decision-impact telemetry**: outage-adjusted signals now log
  `baseline_score_without_redistribution` and `outage_changed_decision` -
  the exact counter the review asked for to detect outages becoming a
  hidden buy-regime.
- **AVWAP distance instrumentation**: the avwap_swing_low_bounce label now
  carries the actual distance in ATR units (e.g. `_0.38atr`), so the
  deferred 0.25-0.75xATR band calibration can run from logged signals alone.
- **Screener exclusion telemetry**: each cycle logs the size (and sample)
  of the unhealthy/low-quality exclusion sets, so the learning filter can't
  quietly become a permanent blacklist.

Declined from this round: ADX range-regime dampening (a scoring change -
contradicts the review's own "don't change the model yet"; revisit with
outcome data) and pairwise candidate-correlation caps (requires bulk price
history per candidate pair; the sector-diversity cap covers the bulk of the
risk for now).

## 2026-07-15 (round 10): auto-pick review sign-off + attribution telemetry

The fifth external review assessed the auto-pick pipeline specifically and
signed off on its structure ("you match or exceed best practices on
price/volume/spread filters, sector/market context, and breadth/volatility
awareness... the strongest remaining work is not more filters"). Its
structural asks were already in place; three attribution gaps were real and
are now closed (12/12 tests still pass):

- **Discovery Score decomposition persisted**: every screener candidate row
  now stores `last_source` (which discovery source surfaced it) and
  `last_decomposition` (rs_20d/50d/100d, trend_aligned, persistence bonus,
  final score) - so outcome analysis can attribute picks to components,
  not one opaque number.
- **Per-source shortlist telemetry**: each cycle logs how many shortlist
  slots each discovery source filled - the input for eventually retiring
  sources that are mostly noise.
- **Coverage + profile stamped on every signal**: `data_coverage` now
  carries `external_coverage_pct` (what fraction of the maverick/finviz/
  analyst/news feeds delivered) and `risk_level` - a decision is auditable
  as "AGGRESSIVE profile, 75% external coverage, analyst via finnhub".

Also affirmed per the review, no action needed: structural filters
(price/volume/spread) stay fixed as risk controls, not tuning knobs;
VOL_EXP stays bonus-only; hard vetoes stay unrelaxed; with the round-8
fallback chain, EXTERNAL redistribution only triggers when ALL providers
(finviz + yfinance recommendationKey + finnhub) fail together - vendor
quirks can't become hidden selection bias. Its advice to run
CONSERVATIVE/MODERATE during watch/paper phase (validating TURBO last) is
a config choice left to the operator: `risk_level` in config.yaml.

## 2026-07-15 (round 11): coverage - "how do we not miss the stock?"

Design question answered this round: a screener that only re-ranks the same
daily top-30 can leave a whole class of good names permanently unscored,
while loosening filters floods the engine with junk. Also answered two
operator questions directly:

- **"Run continuously without cycles?" - No.** The binding constraint is
  DATA QUOTA, not compute: continuous scanning is what got finviz banned
  and yfinance rate-limited on 07-14. Swing signals move on daily bars; the
  15-min cadence oversamples them already. The budget is better spent on
  BREADTH of names than frequency.
- **"More threads with different criteria?" - Already have it** (5 discovery
  sources fan out in parallel; 6 tickers score in parallel). More threads
  hit the same rate limits faster; they don't see more stocks.

What actually widens coverage (all live now, data budget unchanged):

- **Exploration/rotation slots** (`screener.exploration_slots`, default 3):
  each cycle, a few shortlist slots go to structurally-valid quality-gate
  survivors the engine has seen least recently (never-seen first). Over
  days, the entire eligible universe rotates through real scoring instead
  of the same leaders. Slots come out of max_candidates - no extra calls.
- **Research-mode scoring for vetoed names**
  (`screener.research_score_vetoed`, default true): names blocked by
  EXECUTION vetoes (spread/volume/price-range/earnings/timing) still get
  fully scored and logged - marked "RESEARCH ONLY", `passed` pinned False,
  never a live BUY. Answers "would ASTS have been a buy if the spread were
  acceptable?" in the signals record without loosening one guardrail.
  Data-quality vetoes are NOT research-scored (scoring fallback data would
  teach the learner garbage).
- **Near-miss telemetry**: every cycle logs "N BUY, M near-miss (within 5
  pts of bar), K scored" - the operator can now distinguish "zero buys
  because the tape offered nothing" from "zero buys because the bar is
  miscalibrated" at a glance.
- Already in place from earlier rounds, reaffirmed by the review: simple
  structural pre-filters with nuance pushed to scoring; multiple discovery
  sources with quotas; sector-diversity cap; "zero buys is sometimes the
  correct output" (the near-miss line now proves which kind of zero it was).

No sector/stock preference is imposed anywhere - the system ranks whatever
the tape offers and the exploration slots + learning stats are how it keeps
widening what it knows. (12/12 tests pass.)

## 2026-07-15 (round 12): universe sweep - the whole market, not just movers

Direct answer to "will it look at ALL stocks?": previously NO - discovery
only saw movers (gainers/volume surges/gaps/pre-market/sector top-3), so a
quietly accumulating name off every mover list could stay invisible
forever. Now:

- **Persistent `universe` table**: every active, tradable US equity (~10k
  symbols) via Alpaca's free assets endpoint (refreshed daily once keys are
  configured; paper keys work), plus organic accumulation - every symbol
  any source ever surfaces, and everything already in the ticker cache,
  joins permanently.
- **`universe_sweep` discovery source** (config
  `screener.sources.universe_sweep`, default on, `batch_per_cycle: 10`,
  QUOTA 4 guaranteed shortlist slots): each cycle draws the LEAST-recently-
  examined batch and runs it through the exact same quality gate, discovery
  ranking, and (lite-first) scoring as every other candidate. Swept names
  rotate to the back of the queue - verified by unit test.
- Coverage math at defaults: ~10 names structurally examined per cycle,
  ~260/day; the full liquid market completes a structural triage pass in
  weeks and keeps rotating thereafter. With Alpaca keys, raise
  batch_per_cycle to 25+ (bars are cheap at 200 req/min) and a full pass
  takes days. The per-cycle data budget stays bounded either way.
- Why not "another thread scanning continuously": threads don't create API
  quota - the sweep converts the same budget into breadth, which is the
  binding constraint (see round 11).

## 2026-07-15 (round 13): Alpha Vantage wired (movers + universe listing)

Alpha Vantage assessed for the screener: the free tier is ~25 requests/DAY -
useless for per-ticker scanning, ideal for two one-call jobs. Key lives in
`.env` (`ALPHAVANTAGE_API_KEY`), live-tested working:

- **`alpha_movers` discovery source**: TOP_GAINERS_LOSERS returns top-20
  gainers + most-active in one call (4h cache -> ~2 calls/day; QUOTA 2).
  A second vendor's independent mover ranking alongside the yfinance
  screens. Losers excluded (long-only engine). Junk is pre-filtered on the
  price/volume already in the response (the live payload's gainer list was
  dominated by $0.04-$0.09 warrants) so it can't waste gate/ranking calls.
- **Universe seeding fallback**: LISTING_STATUS delivers the full active US
  stock list in one call (7-day cache) - the universe sweep now fills its
  table with just the AV key, no Alpaca required.
- **Hard budget guard**: internal 20-calls/day ceiling (UTC reset) +
  circuit breaker + AV soft-throttle detection ({"Note"/"Information"}
  responses treated as failures). This source can never eat its own quota.
  Visible in the Data Sources panel as `alphavantage`.

NOT used from AV (and why): per-ticker OVERVIEW/NEWS_SENTIMENT/quotes - the
daily cap can't support scanning; those needs are already covered by
yfinance/Alpaca/Finnhub chains.

## 2026-07-15 (round 13): Alpha Vantage + Financial Modeling Prep wired

Two more free data vendors assessed and integrated, both strictly
budget-guarded (their keys are in `.env`; both appear in the Data Sources
panel with NOT_CONFIGURED/OK/DOWN status):

- **Alpha Vantage** (free ~25 req/DAY - the binding fact): wired ONLY for
  low-frequency, high-value calls with a 20/day self-cap:
  `TOP_GAINERS_LOSERS` (one call = ~60 mover tickers, 4h cache) powering
  the new `alpha_movers` discovery source (quota 2), and `LISTING_STATUS`
  (full US listing CSV) as a universe-sweep seed. Per-ticker AV endpoints
  (OVERVIEW etc.) deliberately NOT wired - they'd burn the quota in one
  cycle.
- **Financial Modeling Prep** (free ~250 req/day, 200/day self-cap):
  `biggest-gainers` + `most-actives` power the `fmp_movers` discovery
  source (quota 2, 2h cache), and `stock-list` is the preferred no-Alpaca
  universe seed (full US directory in one call). Premium-gated per-ticker
  endpoints not wired until a paid tier justifies them.

Universe seed priority: Alpaca assets -> FMP stock-list -> AV
LISTING_STATUS -> organic accumulation. Discovery now sees THREE
independent mover lenses (yfinance screens, AV, FMP) plus sector leaders,
gaps, pre-market, exploration slots, and the full-market sweep.

## 2026-07-16: placeholder-fill pass - 3 of 5 remaining PLACEHOLDER rules go REAL

Trinath asked which of the Strategy tab's PLACEHOLDER rules could actually
be filled with real data from what's already configured. Checked every
key/MCP in `.env` and the existing provider stack against all 5 remaining
placeholders; 3 turned out to be free-tier-accessible after all, via FMP
endpoints that survived their Aug-2025 legacy-API migration (verified live,
not assumed from docs):

- **above_avwap_earnings** (buy TREND, 8pt) / **below_avwap_earnings** (sell
  TREND_DETERIORATION, 8pt) / **BELOW_AVWAP** (hard veto #7, previously dead
  code): FMP's `/stable/earnings` gives a genuine past earnings report date
  (not a fiscal period-end). `engine/ticker_analyzer.py`'s new
  `_calc_earnings_avwap()` anchors the same cumulative-VWAP math
  `_calc_swing_low_avwap()` already used, at that date. Disclosed
  approximation: the daily bars this codebase fetches carry no date column
  (Alpaca's, the primary provider, never did), so the anchor bar position is
  estimated from calendar-days-elapsed via a 5/7 trading-day ratio rather
  than an exact date match - accurate to within a couple of trading days
  around holidays. **BELOW_AVWAP was previously unreachable dead code** (the
  guard `avwap_earnings > 0` always failed) and can now actually veto a buy -
  called out explicitly since a hard veto going live is a bigger behavior
  change than a scoring rule.
- **no_recent_downgrade** (buy EXTERNAL, 5pt) / **analyst_downgrade** (sell
  FUNDAMENTAL_RISK, 5pt, new): FMP's `/stable/grades` gives real dated
  analyst rating-change events (company, previous/new grade, action) -
  verified live, caught an actual KeyBanc AAPL downgrade in testing. Replaces
  the round-8/9 external review's default-True placeholder: a data outage
  now resolves to **no credit**, never a silent True, on both sides.
- **estimate_raised** (buy EXTERNAL, 6pt): FMP's `/stable/analyst-estimates`
  gives a real current consensus EPS, but only a snapshot - detecting a
  "raise" needs history to diff against, so a new `estimate_snapshots` table
  (`storage/database.py`) records one reading per ticker per day. Returns
  no-credit (None) until 30 days of history exist per ticker (expect 0 pts
  everywhere for the first month after this deploys), then fires on a
  genuine measured increase (>1%).

**unusual_options_bullish stays a placeholder, on purpose.**
github.com/erikmaday/unusual-whales-mcp was evaluated (Trinath's suggestion)
and confirmed to be the real, literal source - actual options-flow/sweep
alerts, not an approximation - but Unusual Whales has no free API tier
($50/mo minimum) and no key is configured. yfinance's free option-chain data
could only produce a call/put volume-skew approximation, a different and
weaker signal than "unusual flow." Per explicit instruction not to pass off
a partial/approximate source as the real thing, this was left as a genuine
placeholder rather than wired to the weaker proxy. **near_poc_support**
likewise stays a placeholder - no source in this stack gives multi-week
volume-profile data; Alpaca's already-fetched intraday bars are 5-day/IEX-only,
too shallow for a real point of control.

All three new FMP endpoints (`get_last_earnings_date`, `get_recent_downgrade`,
`get_consensus_eps`) live on `FMPProvider` in `mcp_clients/market_data.py`,
share its existing 200-req/day self-cap and circuit breaker, and are
individually cached (24h / 12h / 24h) - a small watchlist stays a tiny
fraction of the daily budget. `rules/swing_buy_rules.py`,
`rules/exit_scorer.py`, `rules/hard_vetoes.py`, and `engine/rules_catalog.py`
were all updated to restore the real point values / REAL tags and fix the
achievable max_points sums (verified against `tests/test_scoring_sanity.py`'s
maxed-ticker denominator check, which caught a stale TREND max during this
pass - fixture updated).

## Known issues to be aware of

- SQLite defaults to WAL mode; falls back to `DELETE` mode automatically if the
  filesystem doesn't support WAL locking (some mounted/network folders don't).
- `data/`, `engine/executor.py`, `engine/rules_engine.py`, `engine/claude_brain.py`
  are leftovers from earlier architecture pivots and aren't part of the active
  pipeline. Safe to ignore or delete; kept for now in case anything in them is
  still useful. `rules/buy_rules.py` (the old 15-rule engine) is in the same
  category as of this pass — still present, no longer called by `scheduler.py`.
- `server.py`'s `/api/config` endpoint writes `config.yaml` with `yaml.dump()`,
  which does **not** preserve comments — if you edit config via the web UI, the
  inline `#` comments throughout the file will be stripped on the next save. Editing
  `config.yaml` directly (which is hot-reloaded, no restart needed) avoids this.
- `.env`'s NVIDIA key has been flagged for rotation in-place (the format is kept,
  the value replaced with a rotation reminder) — go rotate it at
  https://build.nvidia.com when convenient; nothing in the active pipeline uses it.

## Running it

```bash
pip install -r requirements.txt
cp .env.template .env   # only needed if you re-enable the NIM fallback later

# terminal dashboard (default):
python3 main.py

# web UI (two processes - scheduler does the scanning, main.py --ui serves the page):
./run.sh --ui
```

`config.yaml` is hot-reloaded — edit watchlist, rule weights, risk limits,
`risk_level` (CONSERVATIVE/MODERATE/AGGRESSIVE/TURBO), `trading.mode`, or
`scan_interval_minutes` without restarting.
