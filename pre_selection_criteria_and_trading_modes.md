# Pre-Selection Criteria, Data Gaps, and Day/Swing/Hybrid Handling

This documents exactly what the system currently does, read directly from the code (`engine/screener.py`, `rules/hard_vetoes.py`, `rules/swing_buy_rules.py`, `rules/dynamic_thresholds.py`, `rules/market_filters.py`, `scheduler.py`, `config.yaml`) as of today's fixes, plus three same-day follow-up passes: (1) `mcp_clients/edgar_data.py`, `mcp_clients/defeatbeta_data.py`, `mcp_clients/market_data.py`, `engine/ticker_analyzer.py` — replaced the still-broken `edgar_insider_trades` path and added a keyless OHLCV fallback, see Section 2; (2) `engine/stop_state_machine.py`, `engine/position_sizing.py`, `engine/position_management.py`, `rules/swing_buy_rules.py`, `scheduler.py`, `config.yaml` — a real DAY/SWING/HYBRID separation (mode-aware bucket weights/thresholds/stops/position-sizing, plus a forced EOD flatten for DAY positions), see Section 3; (3) `rules/swing_buy_rules.py`, `scheduler.py`, `confirm_fill.py`, `engine/learning_loop.py`, `storage/database.py`, `engine/paper_trader.py`, `engine/live_trader.py`, `config.yaml` — found and fixed a real EV/pattern-database mode-keying bug, added a `signals.trade_mode` column for future calibration, and added a DAY-specific position cap, see Section 4. All 20 tests in `tests/test_scoring_sanity.py` (5 new) pass as of this revision, plus targeted isolated verification of the Section 4 changes (see Section 4 for details — the sandbox this was built in has no local Postgres, so the DB-backed pytest suites can't run here directly).

---

## 1. Pre-selection pipeline — three stages, in order

A ticker only reaches a final buy/hold decision after passing three stages: **discovery** (find candidates), **hard vetoes** (instant disqualifiers), then **the 7-bucket score** (the actual quality judgment). Any single veto blocks scoring entirely; nothing partial-credits its way past a veto.

### Stage A — Screener (candidate discovery, `engine/screener.py`)

Only runs if `screener.enabled: true` (currently on). Your hand-curated watchlist (VRT, ORCL, MU, FIX, ASTS, NFLX) always gets scored regardless of the screener; this stage only adds *extra* candidates.

**Sources (parallelized, one thread each):**

| Source | Status | Notes |
|---|---|---|
| rs_gainers | REAL | yfinance screen, sorted by %change |
| volume_surge | REAL | yfinance screen, sorted by day volume |
| gap_candidates | REAL | yfinance gap screener |
| pre_market_movers | REAL | same tool, gated to ~4:00–9:30am ET |
| sector_leaders | REAL | top movers per SPDR sector, rotation-weighted toward the strongest sectors (by real 1-month sector-vs-SPY RS) |
| universe_sweep | REAL | rotates 10 tickers/cycle through the full ~13,200-symbol Alpaca-listed universe |
| alpha_movers | REAL | Alpha Vantage top gainers/most-active, independent vendor |
| fmp_movers | REAL, **currently rate-limited** | FMP biggest-gainers — hitting HTTP 429 "Limit Reach" in production logs right now (free-tier quota exhausted) |
| fq_movers | REAL | FinanceQuery (keyless, quota-free Yahoo mirror) |
| finviz_screen | Implemented but **disabled in config** | new-52-week-high breakouts; `config.yaml` explicitly sets `enabled: false` |
| momentum_screen | **NOT IMPLEMENTED** | no market-wide screening tool available on any connected MCP |
| insider_buying | **NOT IMPLEMENTED** | same — per-ticker insider Form 4 data now has a direct, first-party SEC EDGAR client (`mcp_clients/edgar_data.py`, added today — see Section 2), but still no market-wide "screen for insider buying" tool on any connected source |

**Pre-filter** (identity + learned exclusions): drops anything already on the watchlist, already held, in a post-stop cooldown, on an active stale-data streak (≥3 consecutive bad cycles), or with a proven track record of ≥12 scored cycles and a real qualify rate ≤5% (self-heals — not a permanent blocklist).

**Quality gate** (real-time re-check before a candidate earns a scan slot): one live price/volume/spread check per survivor — same thresholds the hard vetoes enforce (price $10–$1000, avg volume ≥1M, or ≥2M in DAY mode, spread below the hard-veto ceiling). A candidate that fails here never reaches scoring. An MCP hiccup marks a candidate "unverified" (still gets a slot if capacity allows, but always ranked behind verified candidates).

**Discovery Score** (re-ranks every survivor, regardless of source, 0–100):
- Relative strength vs SPY (20d/50d/100d blend) — **40%**
- Trend alignment (price > SMA20 > SMA50 > SMA200) — **25%**
- Volatility compression (squeeze/NR7/NR4/inside-day) — **20%**
- Today's %change (capped contribution) — **15%**

Plus a **persistence/outcome bonus** (±10 pts, capped): once a ticker has ≥5 scored cycles of history, this shifts from "did it keep reappearing" to "did it actually qualify when scored" (70% weight) and "how strong did it score even when it didn't qualify" (30% weight) — a candidate chronically blocked by stale data (≥50% of cycles) gets an active −15 penalty, not just zero.

**Slot allocation:** fixed per-source quotas (e.g. rs_gainers 4, universe_sweep 4, volume_surge/gap/sector/alpha/fmp/fq 2 each) so no single source can crowd out the rest; unused quota rolls over to the highest-scoring candidates from any source. Capped at `screener.max_candidates` (30), scaled by regime (BULL ×1.5, BEAR ×0.35, CRISIS ×0.2) and risk level (CONSERVATIVE ×0.6 … TURBO ×1.25 — you're currently on TURBO).

**Sector-diversity cap:** no more than 30% (min 3) of the final shortlist from one sector.

**Exploration slots:** 3 guaranteed slots for structurally-valid candidates the engine has seen *least* recently, so the shortlist doesn't just keep re-scanning the same leaders every cycle.

### Stage B — Hard vetoes (`rules/hard_vetoes.py`) — any one fires → skip, no scoring

Checked in this order, first hit wins:

1. **EARNINGS_RISK** — composite score >80/100 (earnings today=80pts, tomorrow=70, in 2 days=60, ≤4 days=40, ≤7 days=20; plus options-expected-move and historical-earnings-move sub-scores — see Section 2, these two never actually contribute, they're placeholders)
2. **SPREAD_WIDE** — (ask−bid)/price exceeds the "veto" tier: 1.00% for swing/hybrid, 0.50% for day
3. **LOW_VOLUME** — avg volume below 1M (2M for day mode)
4. **PRICE_RANGE** — price outside $10–$1000
5. **REG_NEWS** — negative regulatory news (can never fire today — see Section 2)
6. **BELOW_AVWAP** — price below the earnings-anchored VWAP
7. **STALE_QUOTE** — quote older than 30 min (2 min for day mode), only when a real provider timestamp was actually measured
8. **KILL_SWITCH** / **DAILY_LOSS** / **PROFIT_LOCK** — account-level circuit breakers
9. **COOLDOWN** — still inside the post-stop-loss re-entry cooldown
10. **DEAD_ZONE** (day mode only) — 11:30am–1:30pm ET
11. **TOO_LATE** (day mode only) — after 3:30pm ET
12. **BAD_DATA** — data completeness below 40%
13. **ALREADY_OPEN** — already holding this ticker
14. **STALE_DATA_CIRCUIT_BREAKER** — 3+ of RSI/MACD/TREND/VWAP/BREADTH silently fell back to a default this cycle

A veto whose code is SPREAD_WIDE / LOW_VOLUME / PRICE_RANGE / EARNINGS_RISK / DEAD_ZONE / TOO_LATE still gets a **research-only score** computed and logged (for the learning database) — the trade itself stays blocked, but you can see what it would have scored. STALE_QUOTE/BAD_DATA vetoes are *not* research-scored (garbage data would just teach the learner garbage).

### Stage C — Market-wide gates (checked once per cycle, not per ticker)

Two layers:
- **Coarse gate** (`engine/market_context.py`): F&G in range, VIX under the ceiling, no macro blackout, kill switch off. Any failure skips the *entire* cycle.
- **Scored gate** (`rules/market_filters.py`): starts at 100, subtracts for VIX/F&G/macro/breadth issues; needs ≥40 to proceed. Breadth is graded (excellent/good/weak/very_weak/panic), not a hard block on its own. A genuine crisis hard-blocks only when McClellan <−70 **AND** A/D <0.30 **AND** VIX >35 **AND** SPY below its 200DMA all agree simultaneously.

### Stage D — The 7-bucket score (`rules/swing_buy_rules.py`)

Six weighted decision buckets (sum to 100%) plus one additive bonus bucket. Stock weights shown; ETF weights differ (noted in parentheses):

| Bucket | Weight (stock / ETF) | Max raw pts | What it measures |
|---|---|---|---|
| TREND | 22.5% / 29.0% | 67 | Price vs SMA20/50/200 (capped family), EMA9>EMA21, ADX+directional confirmation, Donchian 20d breakout, price vs earnings-anchored VWAP, weekly trend alignment, RS vs SPY (1mo) |
| MOMENTUM | 20.5% / 21.5% | 35 | Dual-path RSI (pullback-in-uptrend vs momentum-zone vs overbought), dual-path Stochastic, MACD cross + histogram + persistence (capped family) |
| VOLUME_PA | 15.0% / 15.0% | 48 | Time-normalized RVOL, OBV rising, CMF, dual-path Bollinger position, multisession VWAP, ATR-normalized AVWAP-swing-low bounce, OBV new-high/divergence + CMF + $-volume expansion + accumulation-days (capped families) |
| EXTERNAL | 16.0% / 6.5% | 54 | Maverick sentiment, technical rating (TradingView preferred, finviz fallback), analyst consensus, sector-vs-SPY RS proxy, unusual options activity, analyst estimate revision, recent-downgrade absence |
| SENTIMENT_MACRO | 15.0% / 8.5% | 34 | News sentiment, sector RS (1-day), Fear & Greed in the 35–75 "healthy" band, insider net buying, short float <20%, yield curve not inverted past −0.5 |
| MARKET_BREADTH | 11.0% / 19.5% | 68 | A/D ratio, %above 20/50-EMA, new-highs/new-lows ratio, McClellan, 5-day A/D slope, SPY/breadth alignment, breadth acceleration |
| VOLATILITY_EXPANSION (bonus only, not weighted into the 100%) | 0% | 14 | TTM squeeze firing, NR7/NR4 compression, inside day — added on top, capped so total never exceeds 100% |

Every bucket uses a **continuous qualification multiplier** (0% of max→0.0×, 30%→0.35×, 50%→0.60×, 100%→1.0×) — there is no hard cliff anywhere; a bucket at 20% of its own max still contributes something, just proportionally less.

**Correlated-evidence caps** prevent one underlying fact from being counted multiple times: TREND's 5 structure rules capped at 38 of their 48 raw points; MOMENTUM's MACD family capped at 18 of up to 19; VOLUME_PA's OBV sub-family capped at 9, and the whole accumulation family capped at 20.

**Partial data-outage handling:** if some EXTERNAL-bucket sources are down (e.g. only finviz or only FMP's estimate/downgrade endpoints), the bucket's effective denominator excludes the confirmed-unavailable rules (fixed today — previously this was all-or-nothing and could actually score *worse* than measured-negative data, which was a real bug, now corrected and covered by a regression test). 75% of a fully-dark bucket's weight redistributes to the other buckets; 25% is deliberately left dead (missing evidence still costs something, it just isn't treated as bearish).

**Dynamic buy threshold** (`rules/dynamic_thresholds.py`) — the bar a score must clear:

```
final = base(profile) + max(regime_adj, vix_adj) + calendar(log-only, off by default)
        + transition_prob×0.08 + mode_adj(+3 for DAY) + breadth_tier_adj + EV_bonus
```
- Base by risk profile: CONSERVATIVE 68%, MODERATE 60%, AGGRESSIVE 55%, TURBO 50% (you're on TURBO).
- Total stress/calendar/transition/mode/breadth adjustment capped at +20%.
- Breadth tiers (reduced today to stop double-penalizing alongside the MARKET_BREADTH bucket): excellent −3%, good 0%, weak +3%, very_weak/panic +5%.
- EV bonus only applies once real pattern-database history exists (never punishes a cold start).
- Final threshold is floored at 50%, ceilinged at 85%.

**Screener candidates get a cheaper "lite" first pass** (bars/quote/indicators only, skipping maverick/finviz/scanner/news — the EXTERNAL bucket's real evidence). A lite candidate within 15 points of its profile's *nominal* base threshold (fixed today, was comparing against the fully-inflated threshold, which is why CONSERVATIVE/MODERATE were promoting nothing) earns a full re-fetch and rescore.

---

## 2. Missing / placeholder data — what's genuinely still fake

These fields are **hardcoded to a neutral default**, not fabricated, but they mean the rules that depend on them can never fire:

| Field | Value | Consequence |
|---|---|---|
| `poc_price` | 0.0 | `near_poc_support` rule (VOLUME_PA) never fires — no volume-profile/POC data source exists anywhere in this stack |
| `options_expected_move_pct` | 0.0 | EARNINGS_RISK veto's options-based sub-score never contributes (only the days-to-earnings component works) |
| `historical_earnings_move_avg_pct` | 0.0 | Same — EARNINGS_RISK's historical-move sub-score never contributes |
| `news_classified` | `[]` | REG_NEWS hard veto can never fire — no regulatory-news classification pipeline exists |
| `rs_percentile` | 50.0 (static) | Not read by any live rule currently |

**Screener sources not implemented at all:** `momentum_screen`, `insider_buying` — no market-wide (as opposed to per-ticker) screening tool exists on any connected MCP for these.

**Currently rate-limited / capped, live right now:** FMP's movers endpoint (HTTP 429, free-tier quota exhausted) and FMP's grades/analyst-estimates endpoints (feed `estimate_raised`/`recent_downgrade`, intermittently 402/exhausted — no confirmed free Finnhub equivalent exists, so this wasn't backfilled to avoid recreating the same "silently dead API" problem with unconfirmed data).

**`edgar_insider_trades` (stock-scanner MCP): still broken, confirmed in production today, despite this morning's param-name fix.** The `ticker` vs `symbol` rename didn't hold — live logs from a real cycle run today show the exact same failure recurring: `MCP error -32602: Invalid arguments for tool edgar_insider_trades: [...] path: ["ticker"]`. This tool has now been observed contributing zero signal across two separate fix attempts; treat it as non-functional until proven otherwise, not "fixed."

**Insider trades now have a second, working source ahead of it: a direct SEC EDGAR client (`mcp_clients/edgar_data.py`, no npx/MCP middleman).** Built today to route around the above — hits `data.sec.gov`/`www.sec.gov` directly (keyless, just a descriptive User-Agent), resolves ticker→CIK, pulls the last 5 Form 4 filings, and parses non-derivative (open-market) transactions into the same shape `engine/ticker_analyzer.py`'s `_parse_scanner()` already expects, so it overrides `insider_trades` ahead of stock-scanner's dead tool with no changes needed to the scoring/parsing logic. First live run surfaced a real bug — `submissions.json`'s `primaryDocument` for XSL-viewer-routed filings (e.g. `xslF345X05/primary_doc.xml`) points at a pre-rendered HTML page despite the `.xml` extension, not the raw XML — which broke parsing for every ticker (`mismatched tag` at an identical line/column, since it was the same boilerplate HTML each time). Fixed by stripping the `xslNNNNN/` folder and fetching the raw doc from the accession root instead. A second live run completed with **zero** parse errors, confirming the fix — but that run hard-vetoed every candidate on stale quotes (market closed, nothing promoted past the lite pass), so `insider_net_direction`/`insider_buys_30d`/`insider_sells_30d` being populated with real, non-empty transaction data is not yet independently confirmed end-to-end. Falls back to whatever stock-scanner supplies (currently nothing) if the direct fetch also comes up empty — never worse than before, best case materially better once a full/non-lite candidate actually exercises it.

**`tradingview_technicals`, `options_unusual_activity` (stock-scanner MCP): unchanged, still unverified.** No new evidence gathered on these today — the response-shape caveat from this morning's fix still stands as-is.

**New optional OHLCV fallback: `defeatbeta-api` (`mcp_clients/defeatbeta_data.py`), built today, not yet active.** A keyless, dataset-backed (Hugging Face parquet via DuckDB) source with no per-request rate limit — wired into `mcp_clients/market_data.py`'s bars provider chain as the last-priority fallback, and now makes `bars_capable()` true even with zero provider API keys configured, closing the gap where a no-keys setup fell straight through to yfinance's scraper-grade `yfinance_get_price_history` (the same endpoint still visibly rate-limit-adjacent in production logs today — "MCP response ... was a markdown table, not JSON," repeated dozens of times per cycle). Confirmed via live logs it's correctly inert right now: `"defeatbeta-api not installed/importable ... this fallback stays inert"` — the optional `pip install defeatbeta-api` hasn't been run, so today's cycles still ran on the pre-existing Alpaca/yfinance path unchanged. Caveat if installed: the dataset refreshes ~weekly, so it's a real option for TREND's SMA200/long-trend rules but not a substitute for real-time bars on MOMENTUM/VOLUME_PA's short-window rules — which is why it's ranked last, not promoted above Alpaca/Tiingo/TwelveData/FinanceQuery.

**finviz:** already migrated off the old dead binary to an in-process `finviz` PyPI scraper; current circuit-breaker trips are most likely finviz.com rate-limiting/IP-blocking, not a missing install — can't fully confirm the exact HTTP status from this sandbox since per-call errors only persist to Postgres.

---

## 3. Day vs. Swing vs. Hybrid — how the split actually works

**2026-07-22 rewrite: this section now describes a real, implemented DAY/SWING/HYBRID separation, not a labeling-only scheme.** Everything below this point was built and tested today (all 20 tests in `tests/test_scoring_sanity.py` pass, including 5 new ones added specifically for this change), directly in response to the gap the previous version of this section documented: entry scoring, thresholds, stops, position sizing, and exits used to be identical across all three modes except for a flat +3% threshold nudge and scan cadence. They no longer are. `config.yaml`'s `trading.mode` is currently **DAY** (changed since the last revision of this doc, which was written while it was set to HYBRID — worth calling out because it's exactly the config that made Section 4's EV mode-keying bug bite hardest: every EV lookup this system has ever made under a DAY config was silently returning "insufficient" no matter how much trade history existed, until today's fix).

### What's different per mode at ENTRY (candidate discovery → veto → score → threshold)

| | DAY | SWING | HYBRID |
|---|---|---|---|
| Scan cadence | 5 min | 15 min | 5 min (same as DAY) |
| Hard-veto volume floor | 2,000,000 avg vol | 1,000,000 | **1,000,000** (falls through to swing's default — "hybrid" ≠ "day" in the veto check, unchanged) |
| Hard-veto quote staleness | 30 min → 2 min | 30 min | **30 min** (same fallback, unchanged) |
| Dead-zone / too-late time vetoes | Yes (11:30am–1:30pm, after 3:30pm ET) | No | **No** (unchanged) |
| Spread hard-veto ceiling | 0.50% | 1.00% | **1.00%** (unchanged) |
| **Bucket weights** (`rules/swing_buy_rules.py`'s `score()`) | **NEW: `weights.swing_buy_day`/`swing_buy_etf_day`** — VOLUME_PA/MOMENTUM/MARKET_BREADTH up, TREND/EXTERNAL down (see config.yaml's comment for the full rationale) | `weights.swing_buy`/`swing_buy_etf` (unchanged) | **Same as SWING** — HYBRID scores through the swing engine, full stop |
| **Base threshold** (before dynamic_thresholds' own adjustments) | **NEW: `risk.<profile>.buy_score_threshold_day_pct`** (base + 5, e.g. TURBO 50→55) | `buy_score_threshold_pct` (unchanged) | **Same as SWING** |
| Dynamic threshold mode adjustment (`rules/dynamic_thresholds.py`) | +3% (unchanged, now layered ON TOP of the higher DAY base above — the two represent different costs, see config.yaml's comment: a structurally-better setup requirement vs. same-day spread/noise cost) | +0% | **+0%** (unchanged) |
| Quality-gate min avg volume (screener) | 2,000,000 floor (unchanged) | config value (unchanged) | 2,000,000 (unchanged, screener-side `mode=="day"` check) |

**Important, and easy to miss:** the DAY row above only applies when `trading.mode` is literally **DAY**. HYBRID's `mode` string passed into `score()`/`check_vetoes()` is `"hybrid"`, which fails every `mode == "day"` check by design — HYBRID's *entry* scoring is untouched by any of today's changes, exactly as documented before. What changed for HYBRID is everything AFTER entry — see below.

### Where HYBRID actually diverges: post-decision trade classification, and what it NOW controls

Once a HYBRID buy signal clears the bar, `_classify_hybrid_leg()` in `scheduler.py` tags the resulting trade as **DAY** only if *both*:
- it clears a stricter bar than the live threshold (final threshold **+3%**, mirroring DAY's own penalty), **and**
- it shows real intraday character (volume ≥1.5× average, or a same-day move ≥2%)

Otherwise it's tagged **SWING**. This classification is computed once, right after `buy_result.should_buy` (moved earlier in `scheduler.py`'s `_evaluate_ticker()` today specifically so the rest of the pipeline below could use it), stored in `positions.trade_mode`, and — as of today — **actually drives real behavior**, not just the Portfolio tab's day/swing split:

- **Position sizing** (`engine/position_sizing.py`'s `calculate(..., mode=effective_mode)`) — a DAY-classified HYBRID leg (or a pure DAY-mode buy) gets `position_sizing.day_size_multiplier` (default 0.5) applied on top of the existing score/EV/volatility/regime/portfolio/execution-quality chain. A SWING leg gets `1.0` (no change from before).
- **Risk-per-share seed** (`scheduler.py`, right before the position row is opened) — now reads `stop_loss_day_pct` instead of `stop_loss_swing_pct` for a DAY leg. This one value cascades into two places without touching their code: `sell_rules.py`'s R-multiple take-profit target (`entry + risk_per_share * r_multiple`) and `stop_state_machine.py`'s ATR-based stop fallback.
- **Live stop ceiling** (`engine/stop_state_machine.py`'s `calculate()`) — reads `position['trade_mode']` directly from the DB row every cycle (not just at entry) and caps the ATR-based stop at `stop_loss_day_pct` instead of `stop_loss_swing_pct` for any position tagged DAY, for as long as it stays open. A position with no `trade_mode` at all (legacy rows, or any position opened before this change) is completely unaffected — verified by `test_day_position_gets_tighter_stop_than_swing`.
- **Forced EOD flatten** (`engine/position_management.py`'s `run_loop_b()`) — see next section.

### NEW: forced EOD flatten for DAY positions

`config.yaml`'s `trading.day_eod_flatten_enabled` (default `true`) and `day_eod_flatten_time_et` (default `"15:55"`, 5 minutes before the close). Every cycle, `run_loop_b()` checks every open position tagged `trade_mode == "DAY"` against the cutoff (`_check_eod_flatten()` in `engine/position_management.py`); once reached, `_evaluate_priority()` returns a **priority-1, urgent** `exit_full` action — the same priority tier as the kill switch and daily-loss-limit account circuit breakers, and it reuses the EXACT SAME execution path those already use (`scheduler.py`'s existing `pa.get("urgent")` handling: a simulated fill in WATCH mode, a real Robinhood sell in EXECUTE + auto_trade). No new order-execution code was written — this only adds a new, high-priority REASON to fire the mechanism that already existed for THESIS_BROKEN/kill-switch exits. A SWING or HYBRID-classified-SWING position is never checked against this cutoff at all (`trade_mode != "DAY"` short-circuits `_check_eod_flatten()` immediately) and can still carry overnight exactly as before.

### What's still identical across all three modes

- **Exit rule TRIGGERS** (`rules/sell_rules.py`'s hard exits: stop_loss/trailing_stop/take_profit/earnings_approaching/vix_spike, and `rules/exit_scorer.py`'s 6-bucket soft Exit Score) — no mode branching was added to either file's logic. What changed is the DISTANCE those triggers fire at for a DAY position (tighter stop ceiling, tighter take-profit target, both via the risk_per_share seed above) and a NEW trigger that sits above them in priority (EOD flatten) — not the trigger logic itself.
- **The scoring engine's rule logic and caps** — TREND/MOMENTUM/VOLUME_PA/EXTERNAL/SENTIMENT_MACRO/MARKET_BREADTH's individual rules, correlated-evidence caps, and the continuous qualification multiplier are all identical math regardless of mode. Only the six buckets' relative WEIGHTS and the composite's base threshold change for DAY - see the table above.
- **`risk.max_trades_per_day`** — still a global daily-trade-count budget across DAY+SWING+HYBRID combined; no day-specific daily-loss cooldown exists yet (see Section 4's "not implemented" note on this).

**Net effect:** with `trading.mode: HYBRID`, the system still scans on DAY's fast 5-minute cadence and scores every candidate through the SWING engine at entry, exactly as before — that part of the design is deliberate (HYBRID's whole point is judging a candidate on its full multi-day evidence, not a stripped-down intraday-only read, before ever deciding to trade it as a day leg). What's new is that once a HYBRID signal is classified DAY after the fact, it now actually BEHAVES like a day trade for the rest of its life — smaller size, tighter stop, tighter take-profit, and a mandatory same-day close — instead of carrying the DAY label with zero enforcement. A pure `trading.mode: DAY` setting (the CURRENT setting, as of this revision) gets its own reweighted entry scoring and higher bar, on top of all the position-level behavior above, closing the gap this section previously flagged as needing "new code, not a config change."

---

## 4. Today's third pass — a real EV mode-keying bug, a data-capture gap, and one new risk lever

This followed a review of Section 3's design that came back with four proposed enhancements (mode-level position caps, tuning HYBRID's DAY-classification thresholds, empirical bucket-weight re-calibration, and "separate day vs swing EV"). Investigating the fourth item — which sounded like it might already be solved, since `engine/ev_engine.py`'s `get_ev_for_signal()` already took a `mode` parameter — surfaced a real, previously-undiscovered bug that had nothing to do with whether the *feature* existed and everything to do with whether it actually *worked*.

### The bug: patterns were always recorded as "SWING", regardless of the account's actual mode

`learning/pattern_database.py`'s `find_similar_trades()` genuinely does filter its SQL query by mode (`db.get_patterns(mode=mode, closed_only=True)` → `WHERE mode = ?`) — the EV *lookup* side was always correctly mode-aware. The bug was on the *write* side: `scheduler.py`'s `_evaluate_ticker()` recorded every new pattern-database row with a hardcoded literal `pattern_db.record_entry(ticker, "SWING", features)` — never `"DAY"`, never the account's actual configured mode, regardless of `trading.mode` or a HYBRID leg's post-entry DAY/SWING classification.

The result: for any account running `trading.mode: DAY` (today's actual live setting) or `trading.mode: HYBRID`, the EV lookup inside `rules/swing_buy_rules.py`'s `score()` was querying `mode="DAY"` (or, for HYBRID, `mode="HYBRID"` — itself a second bug, see below) against a pattern table where literally every row said `"SWING"`. Zero matches, every single time, no matter how many trades accumulated. `ev_result["ev"]` stayed `None` and `confidence` stayed `"insufficient"` forever — the EV bonus in the dynamic threshold calculation and the EV-confidence multiplier in position sizing were both silently inert for as long as the account has run in DAY or HYBRID mode. This wasn't a partial gap or a "needs more data" situation — it was structurally impossible for it to ever produce a match, verified directly: see the reproduction below.

A second, related bug: `rules/swing_buy_rules.py`'s EV lookup passed the raw `mode.upper()` through as the pattern-DB mode key — for a HYBRID-configured account, that's the literal string `"HYBRID"`, which is never written anywhere (a HYBRID leg only ever gets recorded as `"DAY"` or `"SWING"` after `_classify_hybrid_leg()` resolves it — see Section 3). So even after fixing the write-side bug, a HYBRID account's EV lookups would still have matched zero rows forever under the old code, querying a bucket that structurally can't exist.

**Fix (all backward-compatible, no schema changes beyond one new nullable column — see below):**

- `scheduler.py`: `record_entry(ticker, "SWING", features)` → `record_entry(ticker, effective_mode, features)` — `effective_mode` is the same already-resolved "DAY"/"SWING" value Section 3's position sizing and stop-ceiling logic already use (a HYBRID leg is classified before this point in the same code path).
- `rules/swing_buy_rules.py`: the EV lookup's mode key changed from `mode.upper()` to `"DAY" if is_day_mode else "SWING"` — the same two-way boolean the bucket-weight/threshold logic above it already uses, so a HYBRID account's EV lookups now pool with SWING (matching HYBRID's SWING-equivalent treatment at scoring time) instead of querying a phantom `"HYBRID"` bucket.
- `scheduler.py`'s `_has_open_pattern()`, `_close_due_patterns()`, `_latest_open_pattern_id()`, and `confirm_fill.py`'s `_most_recent_open_pattern()` all previously hardcoded `mode="SWING"` when looking up a ticker's open pattern — silently blind to any DAY-mode pattern. All four now query across all modes for that ticker.
- `scheduler.py`'s background learning-loop trigger (`maybe_run_learning_loop`) previously passed `mode=trading_mode.upper()` straight through — same `"HYBRID"`-never-exists issue as the EV lookup, meaning the walk-forward/champion-challenger learning loop has never actually triggered for a HYBRID-configured account. Now runs once for `"DAY"` and once for `"SWING"` when `trading.mode` is HYBRID (both real buckets a HYBRID account actually populates), unchanged for plain DAY/SWING configs.

**Verification (no local Postgres in this sandbox — see below — so verified with isolated, dependency-free reproductions instead of the DB-backed pytest suite):**

```
DAY pool EV:    4.0  (confidence: low, n=20)   — from 20 winning DAY-mode patterns
SWING pool EV: -3.0  (confidence: low, n=20)   — from 20 losing SWING-mode patterns
HYBRID pool EV: None (confidence: insufficient, n=0)  — reproduces the exact pre-fix bug:
  querying mode="HYBRID" against DAY/SWING-only storage matches nothing, proving this
  is precisely what every HYBRID EV lookup was silently doing before today.
```

All 20 existing tests in `tests/test_scoring_sanity.py` still pass unchanged (this fix doesn't touch scoring math, only which pattern-DB pool an EV lookup reads from).

### New: `signals.trade_mode` column — closes a real data-capture gap for items #2/#3

The enhancement review's items #2 (tune HYBRID's DAY-classification thresholds) and #3 (empirically re-calibrate DAY/SWING bucket weights from real outcomes) were both correctly framed by the review itself as *calibration, not code* — decisions that need real trade data to make responsibly, not something to hardcode a guess for today. Checking whether that data is actually being captured surfaced a gap: the `signals` table (every BUY/HOLD/SELL decision, with the full bucket/threshold/EV breakdown, via `storage/database.py`'s `log_signal()`) had **no column recording which mode a given signal was evaluated under at all**. There was no way to segment historical signals by DAY vs SWING vs HYBRID after the fact — item #3's "wait for real trade data, then calibrate" plan had no way to actually filter for the right subset of data once it existed.

Added `signals.trade_mode` (nullable TEXT, via the existing `_add_column_if_missing` migration pattern — zero risk to existing rows/queries): `scheduler.py` now passes `trade_mode=(effective_mode if this was a BUY, else the account's raw configured trading_mode.upper())` into every `log_signal()` call. Rows logged before this migration stay `NULL`; every row from now on carries a real mode label, so whenever there's enough closed-trade history to responsibly revisit items #2/#3, the data to segment by is already there instead of needing another data-capture pass first.

**Items #2 and #3 themselves were deliberately NOT implemented today** — changing the HYBRID classification thresholds (score margin, volume/move ratios) or the DAY/SWING bucket weights without real outcome data to justify the change would be guessing dressed up as tuning, on a system already trading. The honest next step for both is: let `signals.trade_mode` and `pattern_database.mode` accumulate real DAY vs SWING history, then revisit with `learning/walk_forward.py` and `learning/bayesian_updater.py` (both already mode-aware, both already wired to read config-driven weights) once there's enough of it — not a code change today.

### New: `trading.max_day_positions` — mode-specific position cap (enhancement item #1)

The only one of the four enhancement items that was genuinely bounded, concrete, and didn't need to wait on data: a cap on concurrently-open DAY positions specifically, separate from the existing global `trading.max_positions` (10) that counts DAY+SWING+HYBRID-resolved positions together.

Added `trading.max_day_positions` (default `5` in `config.yaml`; falls back to `trading.max_positions` — i.e. no additional restriction — if unset, so existing configs are unaffected unless this is explicitly set lower). Enforced in both `engine/paper_trader.py`'s `execute_buy()` and `engine/live_trader.py`'s `execute_buy_live()`, checked BEFORE the existing global max-positions/rotation logic. Deliberately does **not** rotate — the existing rotation logic can sell a weak SWING holding to make room for a strong new candidate, which is reasonable when both are competing for the same capital/risk budget, but a DAY leg hitting its own cap shouldn't be able to force out a SWING position to make room for one more same-session trade; it just skips the buy and gets another chance next cycle (or once an EOD flatten frees a DAY slot). Verified with isolated fake-DB unit tests (4 cases: cap hit → blocked, under cap → proceeds, SWING buys unaffected by the DAY cap, unset config falls back to the global cap) since the DB-backed pytest suite (`tests/test_paper_trading.py`, `tests/test_live_trader.py`) can't run in this sandbox — see below.

**Still not implemented:** a DAY-specific daily-loss cooldown separate from the account-wide `DAILY_LOSS` circuit breaker (`rules/hard_vetoes.py`'s veto #8). That touches the kill-switch/daily-P&L calculation path directly, which is higher-risk to change without a live cycle to observe it against — flagged here as the next candidate for this lever, not built today.

### A sandbox limitation worth being explicit about

This entire third pass was built and verified in a sandbox with no local Postgres server (`storage/database.py` migrated fully to Postgres — no sqlite fallback remains) and no permission to install one. `tests/test_paper_trading.py`, `tests/test_live_trader.py`, and `tests/test_rotation.py` all fail here with `psycopg2.OperationalError: connection refused` — this is a pre-existing environment gap, not something today's changes caused (confirmed identically in the prior DAY/SWING/HYBRID pass). Everything DB-dependent above was instead verified with hand-built fake-DB objects exercising the exact code paths changed, plus the full `tests/test_scoring_sanity.py` suite (DB-independent, all 20 pass) and `py_compile` across every touched file. Worth running the real pytest suite once on a machine with Postgres reachable, as a final check before trusting this in the next live cycle.
