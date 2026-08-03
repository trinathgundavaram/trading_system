# What Would It Take to Beat the Market? — 2026-07-28 Analysis

**Scope:** Stage 1 historical replay (`engine/backtest_engine.py`), SWING mode, TURBO risk level. Synthesizes every backtest run from this session plus the unresolved findings in `docs/backtest_eval_2026-07-26.md`.

**Short answer up front:** we don't actually know yet whether this beats the market, because we have never computed the one number that would tell us — SPY's buy-and-hold return over the same window. Everything below is about trade-level quality (profit factor, expectancy), which is necessary but not sufficient. Section 5 fixes that gap.

---

## 1. Where the numbers actually stand right now

Five backtests, same underlying rules, different scope, run today:

| Run | Universe | Window | Filter | Cost | Trades | PF | Expectancy(R) |
|---|---|---|---|---|---|---|---|
| A | 60 tickers (screener) | 3yr | none | 25bps | 2,539 | 1.02 | 0.013 |
| B | 60 tickers (screener) | 3yr | standard-tier, no BULL | 25bps | 720 | 1.42 | 0.152 |
| C | 60 tickers (screener) | 5yr | standard-tier, no BULL | 25bps | 1,456 | 0.99 | ~0.00 |
| D | 44 tickers (B's universe minus 16 that lack 5yr history) | 3yr | standard-tier, no BULL | 25bps | 924 | 1.15 | 0.065 |
| D→C | same 44 tickers | 5yr | standard-tier, no BULL | 25bps | 1,456 | 0.99 | ~0.00 |

Decomposing B→C's 0.43 PF drop using D as the control: **removing the 16 recent-IPO tickers cost ~0.27 PF; extending the window back through 2021-2022 cost another ~0.16 PF.** Both effects are real. Neither one alone explains the gap.

**The clean, apples-to-apples number is Run D: PF 1.15, expectancy 0.065R, over 3 years on a fixed 44-ticker universe.** That's the number to trust more than B's 1.42 — B is flattered by a handful of high-flying recent IPOs (HOOD alone was the single largest dollar contributor).

## 2. Why the edge tops out where it does — five root causes, ranked by leverage

### 2.1 The exit model already got fixed once, and the fix that's still sitting on the table wasn't taken

`docs/backtest_eval_2026-07-26.md` did a real parameter sweep after the stop-machine ratchet was wired in (that fix is the reason we get realistic trailing-stop exits at all — before it, win rate was 29.8%, not the 50%+ we see now). That sweep found:

- **`stop_loss_swing_pct` (currently 8% for TURBO in `config.yaml`) was binding on 94 of 181 stop-outs** — clamping the stop tighter than the ATR-derived level justified, on higher-volatility names specifically. Widening it to 16% moved profit factor from 1.23 → 1.48 and expectancy from 0.119R → 0.202R on that test's universe, with **zero effect on low-ATR names** (median risk 2.66% never touches an 8% cap anyway) — a targeted fix, not a global loosening.
- **`r_multiple` and the trailing-stop parameters are already at or past their optimum.** Don't touch those.
- **This was never applied.** `config.yaml` line 235 still reads `stop_loss_swing_pct: 8`.

This is the single highest-confidence, lowest-effort lever on the table — it's already been tested once, just never shipped. **Recommendation #1: re-run the current filtered config (Run D) with `stop_loss_swing_pct: 14` and `16` and compare.**

### 2.2 42% of the intended signal is permanently dark in Stage 1

`config.yaml`'s `swing_buy.bucket_weights`: TREND 0.225, MOMENTUM 0.205, VOLUME_PA 0.15 (real, 58% combined) vs. EXTERNAL 0.16, SENTIMENT_MACRO 0.15, MARKET_BREADTH 0.11 (**42%, completely unavailable** — no free point-in-time historical source exists for analyst consensus, news/insider sentiment, or real sector breadth). Of that dark 42%, 75% gets redistributed to the live buckets and 25% is permanently dead weight (confirmed: max score in Run B's distribution is 80.59%, consistent with the 2026-07-26 fix's ceiling math).

This means **every number in this document describes a strategy running at ~58% of its designed signal set.** The live production system (which does have real-time analyst/sentiment/breadth data) is not the thing being backtested — a handicapped version of it is. This is the largest structural ceiling on what Stage 1 can ever show, and it's not fixable by tuning `config.yaml`; it requires either a paid point-in-time data source for at least one dark bucket, or accepting that Stage 1 backtest numbers will always understate the live system's real edge (and, symmetrically, can never prove the live system doesn't have hidden problems in those same buckets).

### 2.3 The edge is concentrated in higher-volatility, momentum-style names — not broad

Splitting Run B's 720 trades by liquid-mega-cap vs. everything else:

| Bucket | Tickers | n | PF | Expectancy(R) |
|---|---|---|---|---|
| Liquid/mega-cap | MSFT, GOOGL, GOOG, BAC, PFE, CMCSA, F, WFC, C, BNY, KMX, AGNC | 174 | 1.28 | 0.084 |
| Everything else | (CLSK, HOOD, XPEV, PLTR, HIMS, AEHR, RELY, APLD, BABA, etc.) | 546 | 1.44 | 0.174 |

The non-mega-cap bucket carries roughly **double the expectancy per trade.** This echoes the 2026-07-26 doc's much starker finding on an earlier config (PF 1.01 on a pure mega-cap holdout, byte-identical between two stop widths — i.e., no edge at all). The gap has narrowed since then (the entry filter and stop-machine fix both help mega-caps too), but the pattern hasn't gone away: **this strategy's real edge lives in momentum/high-beta names, and a chunk of every aggregate number is diluted by including tickers that structurally don't respond to this signal.**

### 2.4 No protection against a real bear market

The filter (`min_tier: standard, exclude_regimes: [BULL]`) only screens out one regime. The 5-year run's yearly breakdown:

| Year | PF | Note |
|---|---|---|
| 2021 | 1.06 | |
| **2022** | **0.53** | real bear market, rate-hike cycle |
| 2023 | 0.85 | |
| 2024 | 1.24 | |
| 2025 | 1.41 | best year |
| 2026 (partial) | 0.82 | |

2022 is the worst year by a wide margin, and it's exactly the kind of environment `exclude_regimes: [BULL]` does nothing about — BULL exclusion protects against melt-up chop, not drawdowns. `engine/regime_engine.py`'s `calculate()` already takes a VIX input; the entry filter could exclude BEAR and/or high-VIX regimes too, using data already computed in the replay loop (`regime.dominant_regime`, `vix_close`).

### 2.5 The filter's parameters were chosen by hindsight, not blind

Every test this session — the tier/regime filter, the sequential split, the windowed check — used a rule (`min_tier=standard`, `exclude_regimes=[BULL]`) that was picked by looking at the *full* 3-year aggregate. The sequential-split and windowed checks prove the edge doesn't live in only one calendar chunk, which rules out the worst form of overfitting, but they don't rule out a milder one: the two-parameter choice itself was informed by seeing the whole dataset. A genuine blind walk-forward — pick the best filter using only data through, say, 2024-12-31, freeze it, then run it unmodified on 2025-2026 — is the test that would actually settle this, and it hasn't been done. (Note: `learning/walk_forward.py` and `analytics/overfit_risk.py` already exist in this codebase for the *live* pattern-database learning loop, but they operate on live closed-trade history, not the Stage 1 backtest — they're not a substitute for this.)

## 3. Two secondary factors, real but smaller

- **Flat-bps cost model.** `engine/execution_costs.py`'s 25bps round-trip haircut is a single number applied identically to CLSK and MSFT. Real slippage scales with liquidity — `rules/execution_quality.py`'s existing `dollar_volume_bands` already model this for live signal scoring and could be reused to make the backtest's cost assumption liquidity-aware instead of flat. Direction of the likely correction: thinner names (several of the top PnL contributors) are probably slightly *more* expensive to trade than 25bps assumes, mega-caps slightly less.
- **No portfolio-level constraints.** Every trade is equal-weighted with no cap on concurrent positions or correlation. The "redeployment effect" that made the real entry-filter run (Run B) beat the post-hoc estimate is genuinely valuable, but it implicitly assumes unlimited parallel capital across 60 tickers. A capital-constrained replay (e.g., max N concurrent positions, forcing the engine to choose between simultaneous candidates) would be a more honest test of what's actually achievable.

## 4. Sample size — a standing caveat, not a fixable defect

Run D (the trustworthy apples-to-apples number) has 924 trades over 3 years; the clean filtered subset is smaller still. This project's own `analytics/overfit_risk.py` cites "a few hundred trades" as the review's recommended minimum before trusting any weight adaptation — we're in that range, not comfortably past it. This isn't something to "fix" so much as something to keep discounting the headline numbers for until more history (or a wider universe) accumulates.

## 5. The missing piece: did any of this actually beat the market?

Every number above is PF/win-rate/expectancy — internal measures of trade quality. None of them answer "would you have done better just buying SPY and holding it." That comparison has never been run this session, and it's the actual bar implied by the question "make the model beat the market."

I can't fetch this myself (no live network access in this environment). `scripts/spy_benchmark.py` (new, added alongside this analysis) computes SPY's buy-and-hold total return and CAGR over any window, and can infer the window directly from a results.json's trade dates:

```
python3 scripts/spy_benchmark.py --results-json output/backtest_results/20260728_071304/results.json
python3 scripts/spy_benchmark.py --results-json output/backtest_results/20260728_090713/results.json
```

Compare that CAGR against the strategy's own realized return — which itself needs one more thing this backtest doesn't currently compute: a compounded equity curve. Right now `summarize()` reports expectancy per trade, not cumulative portfolio growth under a real position-sizing rule. Those are different questions (expectancy_r tells you if each bet is good; CAGR tells you if the portfolio grew faster than the index), and only the second one is directly comparable to "beat the market."

## 6. Recommended order of operations

1. **Ship the stop-width fix that's already proven** (§2.1) — re-run Run D's config with `stop_loss_swing_pct: 14` and `16`, compare PF/expectancy. Cheapest, highest-confidence lever available.
2. **Compute the SPY benchmark** (§5) — establishes whether "beat the market" is even close, or a long way off, before investing more in trade-level tuning.
3. **Add a BEAR/high-VIX regime exclusion** to the entry filter (§2.4) — the plumbing already exists (`regime.dominant_regime`, `vix_close` are already computed per-candidate in `run_replay`), this is a config/filter change, not new infrastructure.
4. **Run one genuine blind walk-forward** (§2.5) — pick filter parameters using only pre-2025 data, freeze, test unmodified on 2025-2026. This is the test that would actually retire the "hindsight selection" caveat that's followed every result this session.
5. **Build a compounded equity-curve report**, not just per-trade expectancy — the thing that's actually comparable to SPY's CAGR.
6. Lower priority / higher effort: liquidity-aware cost model (§3), portfolio-level position constraints (§3), pursuing a real point-in-time data source for one dark bucket (§2.2).

## 7. Addendum (2026-07-28, later same day): both fixes implemented and tested — the BEAR/VIX gate has a real hole

Both §6 items 1 and 3 were built: `--stop-loss-swing-pct` (backtest-only override, `config.yaml`'s live default untouched) and `max_vix` alongside `exclude_regimes` in the entry filter (`exclude_regimes: [BEAR]` needed no new code — `dominant_regime` is a plain string).

Results on Run D's fixed 44-ticker universe:

| Config | Window | Trades | PF | Expectancy(R) |
|---|---|---|---|---|
| Run D baseline (standard-tier, exclude BULL) | 3yr | 924 | 1.15 | 0.065 |
| + stop=16% | 3yr | 729 | 1.19 | 0.091 |
| + exclude BEAR, max_vix=25 | 3yr | 718 | 1.25 | 0.109 |
| + both combined | 3yr | 608 | 1.27 | 0.109 |
| + exclude BEAR, max_vix=25 | **5yr** | 1,052 | **1.10** | 0.036 |

The regime/VIX gate is the stronger of the two levers, and stacking the stop-width fix on top adds only a little more (1.25→1.27) — diminishing returns once the regime gate is active.

**But the 5-year run reveals the gate has a real hole.** 2022's PF under this "protected" config: **0.42** — worse than the *ungated* 5-year run's 2022 PF of 0.53. Checking why: **all 108 of 2022's surviving trades are labeled `CHOPPY`, zero are `BEAR`.** `exclude_regimes: [BEAR]` and `max_vix: 25` never fire on a single one of them, because the regime classifier never calls 2022 "BEAR" in the first place.

Root cause, traced into `engine/regime_engine.py::calculate()`: the regime vote has 5 weighted inputs (SPY-vs-SMA200 30pts, SPY-vs-SMA50 20pts, VIX 25pts, Fear&Greed 20pts, Advance/Decline ratio 25pts). In Stage 1, **Fear&Greed and A/D ratio are hardcoded to neutral (`fg_score=50, ad_ratio=0.5`)** — no free point-in-time historical source exists for either (same "unavailable, not faked" gap as the buy-score's dark buckets in §2.2, except here it silently feeds a live decision instead of being flagged unavailable). Neutral F&G/A/D always contribute their two `choppy_ev` shares (15+15=30 points) no matter what the market is actually doing — a fixed thumb on the scale toward CHOPPY that a grinding, not-straight-down bear market (like much of 2022) doesn't generate enough SMA/VIX signal alone to overcome.

**This means "exclude BEAR" as currently buildable in this backtest cannot protect against a 2022-style year — it isn't that the exclusion logic is broken, it's that the label it depends on structurally under-fires.** The real fix is one of:
1. Build an independent realized-volatility filter directly from price data (e.g., trailing 20-day SPY realized vol, or the VIX level alone at a lower threshold than 25) instead of routing through `dominant_regime`.
2. Fix `regime_engine.calculate()`'s Stage-1 inputs — either find a real historical F&G/A/D proxy, or redistribute their 45 combined points to the 3 real inputs the way the buy-score's dark buckets already do (§2.2's 75%-redistribution treatment), so a real regime can win the vote without needing to fake data.
3. Accept CHOPPY exclusion too, despite it being the dominant regime bucket in this data (this guts most of the trade opportunity set — the blunt option, probably not the first choice).

This is worth more than the stop-width fix and the tier/BULL filter combined were ever going to be — it's the difference between a filter that looks like it protects against a real bear market and one that actually does.

## 7b. Addendum: the compounded equity curve (§6 item 5) is built

`scripts/equity_curve.py` replays a results.json's trades chronologically under a stated risk-per-trade sizing rule, compounds equity at each close, and reports CAGR, total return, max drawdown, and a month-end-resampled Sharpe ratio. It can also call `spy_benchmark.py` directly via `--compare-spy`. Results on the best config found so far (§7's stop=16 + BEAR/BULL-exclude + max_vix=25, 3yr fixed 44-ticker universe):

| Risk/trade | CAGR | Max drawdown | Sharpe |
|---|---|---|---|
| 0.5% | 10.7% | 13.4% | 1.07 |
| 1.0% | 20.4% | 25.6% | 1.06 |
| 2.0% | 35.8% | 46.4% | 1.04 |

Same config, 5-year window (the one that includes 2022):

| Risk/trade | CAGR | Max drawdown | Sharpe |
|---|---|---|---|
| 0.5% | 2.9% | 32.6% | 0.29 |
| 1.0% | 4.1% | 55.5% | 0.28 |
| 2.0% | **1.5%** | 81.8% | 0.27 |

Two things worth reading carefully here, not just the CAGR column: (1) **no capital/concurrency ceiling is enforced** — every qualifying trade across all 44 tickers is allowed to size simultaneously against current equity, which real available capital would never permit, so every number above is an upper bound, not a validated live-tradeable return; (2) in the 5-year run, CAGR at 2% risk/trade is *lower* than at 1% — a real overbetting signature (losing streaks compound against the account faster than winning streaks compound for it), not a data error. The 5-year Sharpe (~0.28) vs the 3-year Sharpe (~1.06) is the same story as §7's finding in a different unit: whatever edge exists degrades hard once a real bad year is included, and this filter combination still hasn't solved for that.

## 7c. The decisive number, finally: SPY over the same two windows

| Window | SPY CAGR | Strategy CAGR (0.5% / 1% / 2% risk) | Strategy max DD | SPY's own 2022 drawdown (approx, general knowledge) |
|---|---|---|---|---|
| 3yr (2023-08 to 2026-07) | **19.61%** | 10.7% / 20.4% / 35.8% | 13-46% | n/a (no 2022 in this window) |
| 5yr (2021-07 to 2026-07) | **12.53%** | 2.9% / 4.1% / 1.5% | 33-82% | ~-25% peak-to-trough |

**Verdict: this does not beat the market. Over the window that matters most (5 years, includes a real down year), it loses to simply buying and holding SPY by 8-10 percentage points of CAGR a year, while taking on 2-3x SPY's drawdown.** Over the shorter, friendlier 3-year window it looks competitive only at 1% risk/trade (20.4% vs 19.61% — close enough to be noise, not a demonstrated edge) and only "wins" outright at 2% risk/trade, a result that should be discounted heavily given §7b's own finding that higher risk-per-trade is not reliably better and flipped negative in the very next test (the 5-year window). The apparent edge in the 3-year number is best explained as this specific filter combination happening to fit a strong, mostly-bullish recovery period (2023-2025) well, not as a structural advantage over the index.

This closes the loop this whole analysis chain opened with: the honest answer to "does the current model beat the market" is **not currently, and not by a margin large enough to trust over any window that includes a real bad year.** Everything in §6/§7's recommendations (the regime-classifier fix in particular) is still the right next work if the goal is to close that gap — but it should be understood as working toward beating the market, not as having already gotten there.

## 7d. The regime-classifier-independent fix was built — it did not work either

`realized_vol_pct()` (2026-07-28) computes trailing annualized SPY volatility directly from price data, bypassing `dominant_regime` and its fake F&G/AD-ratio inputs entirely. Tested on the 5-year window, `min_tier: standard, exclude_regimes: [BULL], max_realized_vol_pct: 25`:

| Config | 2022 trades | 2022 PF | Full-run PF | Full-run expectancy(R) | CAGR @ 1% risk | Sharpe |
|---|---|---|---|---|---|---|
| Ungated (Run C) | 108 | 0.53 | 0.99 | ~0.00 | — | — |
| exclude BEAR + max_vix=25 | 108 (all CHOPPY) | 0.42 | 1.10 | 0.036 | 4.1% | 0.28 |
| **max_realized_vol_pct=25** | 79 (62 CHOPPY, 17 BEAR) | **0.24** | 1.07 | 0.023 | 1.5% | 0.18 |
| + stop=16 combined | 71 | 0.39 | 1.05 | 0.010 | (worse) | — |

This filter DID reject some 2022 candidates (79 remained of 108, vs. the previous gate's 108/108) and it DID pull in some real BEAR-labeled trades this time (17 of 79) — so the regime-classifier-independence worked exactly as designed. **But 2022's surviving trades got WORSE, not better (PF 0.24, the worst of any attempt), and the full-run numbers are worse across the board than the already-inadequate BEAR/VIX gate.**

The likely explanation: 2022's losses in this strategy aren't concentrated in the highest-volatility days. A flat realized-vol threshold cuts off the extreme-swing days (which include some of this momentum-following exit logic's biggest winners) while leaving the grinding, moderate-volatility chop untouched — and that grind is exactly where 2022's losses live. **This means the problem isn't day-level volatility or regime detection at all — three independent approaches (regime label, VIX, price-derived realized vol) have now all failed to protect against 2022, which points at something more structural: a sustained multi-month downtrend that no single-day snapshot can catch.**

**Recommendation, and a caution.** The one conceptually different idea left worth trying is a longer-horizon trend filter — e.g. "SPY below its 200-day SMA" or "SPY below its 200-day SMA for N consecutive weeks" — a classic systematic-trading regime filter designed specifically to sit out sustained downtrends, not day-level noise. That's worth one test. Past that, this is the point to stop hunting for a filter that fixes 2022 on this same 2021-2026 dataset — per §8's own warning, every additional threshold tried against the same fixed history raises overfitting risk rather than resolving it. If a 200-day-SMA-style filter doesn't work either, the honest conclusion is that this strategy currently has no good answer for a sustained bear market, and the real fix is a portfolio-level circuit breaker (e.g., "halt new entries entirely when SPY is deeply below its 200-day SMA," independent of any per-trade signal) rather than another entry-filter threshold.

## 7e. Built: the 200-day trend filter, and a curated sweep tool to test everything at once

`spy_trend_pct()` computes SPY's price as a % above/below its own 200-day SMA, directly from price data - no `dominant_regime`, no VIX, no realized-vol involvement. Wired into the entry filter as `min_spy_trend_pct` (0 = require SPY at/above its 200-SMA; negative values allow a cushion). This is the one idea in this whole chain that's structurally different from the three that already failed - a multi-month read instead of another same-day snapshot.

Rather than test it one command at a time (today's pattern so far), `scripts/sweep_backtest_configs.py` runs a curated set of 9 configs against the SAME cached price data (one fetch, many replays) and reports, per config: overall PF/expectancy, **2022's PF specifically** (the direct "would this hold against a 2022-like year" answer), a losing-years count, and CAGR/Sharpe/max-drawdown via the equity-curve simulator. This directly answers Trinath's ask - "changing the parameters... trying different rules, and see what would be the best" - within the backtest-only scope this whole session has stayed inside of (see the script's own docstring for why actual scoring-rule/indicator changes are explicitly OUT of scope here, not silently folded in).

Combos tested: baseline (standard-tier + exclude BULL), the trend filter at two thresholds (0 and -5), the trend filter stacked with the stop-width fix, with the realized-vol filter (defense in depth), with `r_multiple=4`, a "kitchen sink" combining all three, and `min_tier=strong` (highest-conviction-only) alone and combined with the trend filter. This has not yet been run - it needs a live price-data fetch this environment can't make. Run:

```
python3 scripts/sweep_backtest_configs.py
```

and paste the output for the next analysis pass.

## 7f. Addendum: the equity curve was assuming unlimited cash — fixed

Trinath's catch: "do not test for unlimited cash. go for a fixed start capital and continue with that portfolio." Correct catch — every CAGR/Sharpe number in §7b/§7c/§7d was computed by an equity-curve simulator that let every open trade size off current equity with no check on whether that much cash actually existed. With overlapping positions (this strategy holds several tickers at once), that silently assumes access to more capital than a real account has, which inflates both the number of trades that "fit" and the compounding effect.

`scripts/equity_curve.py::simulate()` now tracks real `cash` alongside `equity`. Each trade's **position value** (not just its dollar risk) is computed as `risked_dollars / (trade's own risk_pct / 100)` — e.g. risking 1% of equity at a 5%-away stop deploys 20% of equity in position value; at a tight 0.5%-away stop, the same 1% risk deploys 200% of equity, which a real account can't do. If a candidate's position value exceeds available cash at open, it's **rejected outright, not resized or queued** — `n_capital_constrained_rejected` counts these. Cash is debited on open and credited back (plus/minus P&L) on close. A `--unlimited-capital` flag reproduces the old assumption for direct before/after comparison. `scripts/sweep_backtest_configs.py` now takes `--starting-equity` (default $100,000) and reports a `CapRej` column per config.

This is expected to produce **lower, more realistic CAGR figures** than §7c's numbers once re-run — some candidate trades that previously "opened" for free will now get rejected for lack of available cash, especially in scenarios with several overlapping positions or tight stops. 17 new/updated unit tests in `tests/test_equity_curve.py` pin the rejection behavior, the cash-freed-on-close behavior, and that `--unlimited-capital` still exactly reproduces the old numbers; all 109 backtest/equity-curve/entry-filter/execution-cost tests pass with zero regressions.

**Re-run needed** (this sandbox has no live price-data access):

```
python3 scripts/sweep_backtest_configs.py --starting-equity 100000
```

paste the output — the `CapRej` and CAGR columns are the ones that matter most against the earlier (unconstrained) §7c/§7e numbers.

## 7g. The full sweep ran — verdict: none of these 9 configs beat SPY, and the trend filter fails 2022 the same way the other three did

Ran on Trinath's machine, 44 tickers × 5yr, capital-constrained at $100,000: **12,260s (~3.4 hours)**, mostly per-day replay compute, not the network fetch (bar caching only shaved the fetch time off configs 2-9, not the ~1,000-2,000s of hard-veto/scoring/regime computation each config still has to redo). Results:

| Config | N | PF | ExpR | PF-2022 | n22 | LossYrs | CAGR | MaxDD | Sharpe | CapRej |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline (standard, exclude BULL) | 925 | 1.05 | 0.014 | 0.42 | 111 | 2/6 | 2.8% | 37.7% | 0.26 | 383 |
| + trend≥0 (SPY at/above 200sma) | 825 | 1.07 | 0.016 | **0.06** | 39 | 3/6 | 2.1% | 43.3% | 0.21 | 356 |
| + trend≥-5 (5% cushion) | 899 | 1.07 | 0.018 | 0.32 | 86 | 2/6 | 2.9% | 38.2% | 0.26 | 379 |
| + trend≥0 + stop=16 | 773 | 1.03 | 0.008 | 0.04 | 39 | 3/6 | -2.2% | 41.3% | -0.08 | 315 |
| + trend≥0 + realized_vol≤25 | 815 | **1.09** | **0.023** | 0.08 | 29 | 3/6 | 2.7% | 41.6% | 0.25 | 353 |
| + trend≥0 + r_multiple=4 | 812 | 0.98 | -0.015 | 0.06 | 39 | 3/6 | -1.2% | 42.9% | -0.00 | 362 |
| kitchen sink (trend+stop16+rm4) | 764 | 1.04 | 0.004 | 0.04 | 39 | 3/6 | 1.1% | 41.2% | 0.15 | 328 |
| strong-tier only | — | — | — | — | — | — | — | — | — | — |
| strong-tier + trend≥0 | — | — | — | — | — | — | — | — | — | — |

(strong-tier rows: **zero trades**, both of them, over the full 44-ticker/5-year window.)

**The headline number: best CAGR across all 9 configs is 2.9% (trend≥-5), against SPY's 12.53% over the identical 5-year window (§7c).** Not close — the best backtest-only config still leaves more than 4x of SPY's return on the table. Worse, every config's max drawdown (37.7%-43.3%) is *larger* than SPY's own ~25% peak-to-trough in this window (§7c) — this isn't "less return for less risk," it's less return for *more* risk, the worst combination a filter search can produce. Best risk-adjusted number is a Sharpe of 0.26 (baseline and trend≥-5, tied) — weak on its own terms, before any SPY comparison.

**The trend filter fails 2022 the same way the other three defenses did, and by a similar mechanism.** `min_spy_trend_pct=0` cuts 2022 participation from 111 trades to 39 — but PF-2022 on those 39 survivors is **0.06**, dramatically worse than baseline's already-bad 0.42. This is the fourth independent same-window defense this session to fail against 2022 (regime-label BEAR exclusion, VIX cap, price-derived realized-vol cap, now SPY's own 200-day trend) — and the failure mode is consistent across all four: each filter removes *some* bad trades, but whatever gets through anyway is disproportionately worse, not better, than the unfiltered baseline. The likely mechanism here specifically: SPY briefly poking back above its 200-SMA during a still-declining 2022 (a classic bear-market dead-cat bounce) is exactly the kind of moment that both passes a trend filter and precedes the next leg down — the filter is catching the bounce, not avoiding it. `trend≥-5`'s looser cushion barely filters anything (86 of 111 2022 trades still get through) and its PF-2022 (0.32) sits closer to baseline for the same reason: it isn't really gating on the trend signal at this threshold.

**Stacking more filters made things worse, not better.** `r_multiple=4` alone flips PF below 1 (0.98, negative expectancy) — a wider profit target just means more trades give back gains before hitting it. `stop=16`, which looked like a clean win in isolation back on 2026-07-26 (docs/backtest_eval_2026-07-26.md), turns CAGR negative (-2.2%) once stacked with the trend filter on this universe/window — a genuine interaction effect, and a concrete example of why §8's "don't stack filters based on hindsight" caution exists: an effect measured in one context doesn't necessarily compose with another.

**Both strong-tier configs produced zero trades over 5 years across 44 tickers.** That's not "the filter is strict," that's the filter and the universe/window combination never intersecting even once — worth flagging as a scoring-calibration question (is `strong` tier's ≥85% threshold reachable at all under this scoring system's actual distribution, once BULL-regime days — which is plausibly where high scores concentrate — are excluded?) rather than a result to read anything into.

**Capital was a real, binding constraint, not a footnote.** 315-383 candidates were rejected per config for lack of available cash out of roughly 900-1,300 that passed the entry filter — confirming §7f's capital-constraint fix materially changes these numbers versus the earlier unconstrained runs (e.g. baseline's 1%-risk CAGR: 4.1% unconstrained in §7c vs 2.8% constrained here).

**Where this leaves the "make it beat the market" question.** This sweep was the most systematic test available within backtest-only parameter space — 9 curated configs spanning tier, regime, VIX, realized-vol, trend, stop-width, and take-profit knobs, the exact ask ("changing the parameters... trying different rules, and see what would be the best"). None of them beat SPY; several make results actively worse than doing nothing. Per §8, this is the point to stop re-slicing this fixed 2021-2026 dataset with more filter combinations — the next filter tried here would be fitting noise, not finding signal. The two paths that remain: (1) accept the strategy, in its current form, is not shown to beat the market and treat it as a research/paper-trading system rather than a live-capital claim, or (2) pursue an actual change to `rules/swing_buy_rules.py`'s scoring/indicator logic — a different, bigger conversation than anything in this sweep, and one that requires this project's release gate (classify_change.py, a validation receipt) precisely because it would be a real decision-function change, not a backtest-only knob.

`scripts/sweep_backtest_configs.py` now also supports resuming a killed/crashed run: `--out` (default `sweep_checkpoint.json`) is written after every config finishes, not just at the end, and a re-run skips whatever's already in that file (`--fresh` to force a clean restart). This run predates that change, so its structured per-combo JSON wasn't saved — only the printed log (`sweep_20260728_171105.log`) survives as a record.

## 8. What not to do

Stop searching for a better filter by re-slicing this same fixed dataset. This session alone tried: unfiltered, standard-tier only, exclude-BULL only, standard+exclude-BULL, three different universes, two different windows, a sequential split, a stop-width variant, a BEAR/VIX gate, a price-derived realized-vol gate, a 200-day SPY trend filter, and a 9-config curated sweep combining all of the above (§7g) — on the same underlying 2021-2026 price history. Every additional parameter tried against the same fixed data raises the odds that whatever looks best is fit to noise, not signal. **As of §7g, this line of investigation is exhausted, not paused** — four independent, conceptually different attempts at bear-market protection all failed against 2022 by the same mechanism (removing some bad trades while letting through disproportionately worse ones), and the full curated sweep confirms no combination of the levers this backtest exposes gets within reach of SPY's buy-and-hold return. The filter that's currently testable via `config.yaml`/CLI flags is a reasonable one to keep testing forward on new, not-yet-seen data (§2.5's blind walk-forward, still not done) — it is not a reasonable one to keep re-deriving backward from this same window. Any further "does X filter help" question against 2021-2026 should be treated as curve-fitting until proven otherwise.

**§9 below is the one exception this closing note doesn't apply to** — a genuinely new mechanism (forced position-level exits, not another entry-time threshold), not another re-slice of the same nine-config space.

## 9. A new strategy: forced regime exits, not just entry filters (built, not yet run)

Trinath's ask after §7g: "come up with a whole new strategy that can beat SPY across all years... same framework but different rules." Two scope questions first, both answered explicitly:

1. **Backtest-only, new module — recommended, chosen.** This does not touch `rules/swing_buy_rules.py`, `engine/regime_engine.py`, or any live/paper trading path. Your real Robinhood positions keep running on the current, unmodified logic throughout. Actually redesigning the live decision function is a different, bigger undertaking requiring this project's release gate (`classify_change.py`, a validation receipt) — available if you want it, but not what got built here.
2. **Core mechanism: a regime-timing overlay with forced exits — recommended, chosen** over redesigning the stock-picking logic. This is the direct answer to something every one of §7g's nine configs shared as a blind spot: every filter tried only blocked *new* entries. None of them ever closed a position that was *already open* when the market turned — which is exactly why the 200-day trend filter still failed 2022 (§7g): it blocked most new entries during the downtrend, but the ~39 that slipped through rode the stop machine's own exit logic all the way down with no further defense.

**Honest framing, stated up front rather than left implicit: no legitimate systematic strategy beats a benchmark in literally every calendar year.** That's not a bar any real backtest should claim to clear, and this one doesn't either. What a trend-timing overlay with forced exits can plausibly do — based on decades of published research on this general technique, not on this codebase's own results, which the sweep below will actually show — is meaningfully shrink drawdown during a sustained downtrend like 2022, at some cost to upside capture during fast, choppy recoveries (both the hysteresis band and the forced exit itself give back some of the snapback rally before the trend signal re-confirms). Whether this specific implementation does that well enough on this universe/window to be worth anything is what the sweep needs to answer — it hasn't run yet.

**What was built** (`engine/regime_timed_backtest.py`, new file, ~330 lines):

- `compute_regime_timeline()` — SPY price vs. its own trailing SMA, with a **hysteresis band** (state only flips when price clears the band by `buffer_pct` in either direction; inside the band it holds the previous state) so a whipsaw right at the line doesn't flip the regime every few days. This is a well-known systematic-trading technique (in the spirit of Meb Faber's 10-month-SMA timing model), not something curve-fit to this dataset — `buffer_pct=0` reduces it to a raw crossing test for an A/B check of whether the band itself earns its keep.
- `simulate_forward_exit_regime_gated()` — line-for-line the same real stop-machine replay as `backtest_engine.simulate_forward_exit()` (same production `engine/stop_state_machine.py` calls, same point-in-time stop-carry discipline), with exactly one addition: if yesterday's regime read says risk-off, the position is **force-closed at today's open**, before the stop/target check even runs. Recorded as `exit_reason="rule_exit"` (an existing member of `rules/common.py`'s closed `EXIT_KINDS` vocabulary, not a new ad-hoc string) plus an explicit `regime_forced_exit=True` flag.
- `run_regime_timed_replay()` — reuses the exact real `rules/hard_vetoes.py`/`rules/swing_buy_rules.py` entry pipeline `backtest_engine.run_replay()` calls (imported, not copied), so **stock selection is byte-identical to production**. The only two differences: a candidate also needs yesterday's regime read to be risk-on before it's even scored, and exits go through the regime-gated simulator above.
- Point-in-time discipline: the regime state used to gate any decision on day N always comes from day N-1's close (`_regime_asof_yesterday()`, bisected over SPY's own trading calendar) — never same-day, matching every other `asof` helper in this codebase.

**Tested before handoff**, not just written: 13 new unit tests (`tests/test_regime_timed_backtest.py`, synthetic bars, no network) pin the hysteresis band's flip/hold behavior, the no-look-ahead property, that a regime exit fires at the *open* and takes priority over a same-day stop, and that turning the mechanism off (`regime_by_date=None`) reproduces the plain simulator exactly (zero behavioral drift when unused). 122 tests pass across the full backtest/equity-curve/entry-filter/exit-replay/regime-timed suite, zero regressions. Beyond unit tests, the full pipeline was smoke-tested end-to-end against synthetic random-walk price data (both a single run and a checkpoint-resume run) to catch integration bugs before committing hours of real yfinance time to it — both ran clean, produced sane trade records, and correctly force-exited mid-hold on the injected synthetic crash.

**Not yet run against real data** — this sandbox has no live network access, same limitation as every other sweep this session. `scripts/sweep_regime_timed_configs.py` sweeps 6 curated `(sma_period, buffer_pct)` combos — `(200,0)` raw-crossing baseline, `(200,3)`/`(200,5)` classic 200-day with a deadband, `(100,3)`/`(150,3)` faster trend reads, `(250,5)` a slower, more patient one — over the same 44-ticker/5-year universe, capital constraint, and cost model as §7g's table, so the results are directly comparable. Reuses today's bar cache (no re-fetch if §7g's sweep already ran on this machine), a resumable checkpoint (`sweep_regime_checkpoint.json`, written after every config), and per-ticker progress printing.

**Run it** (ideally via the caffeinated wrapper — this is likely another multi-hour run, each config replays the full universe/window just like §7g's did):

```
./scripts/run_sweep_caffeinated.sh scripts/sweep_regime_timed_configs.py --starting-equity 100000
```

or directly: `python3 scripts/sweep_regime_timed_configs.py --starting-equity 100000`

Read the `FcdExit` column first — it's the count of trades this new mechanism actually force-closed mid-hold, i.e. whether the new lever did anything at all on this data. Then `PF-2022` against §7g's baseline (0.42) and its best entry-filter attempt (0.06) — this is the real test of whether forced exits succeed where nine entry-only filters failed. Then `CAGR`/`MaxDD`/`Sharpe` against SPY's 12.53% CAGR / ~25% max DD (§7c) for the final verdict. Paste the output for the next analysis pass.

### 9a. First real run was misleading — comparison error, now fixed

The first full sweep ran (44 tickers, 5yr, 6 configs, 3,765s) and every single one came back badly worse than §7g: CAGR -9.4% to -14.5%, max drawdown 46-60%, PF 0.92-0.97, roughly 2,700-2,985 trades per config against §7g's ~800-925.

**That result was an artifact of a comparison error I made, not evidence about the regime-timing mechanism.** `run_regime_timed_replay()`, as first written, never applied `cfg["backtest"]["entry_filter"]` — the same `min_tier=standard, exclude_regimes=[BULL]` filter every single config in §7g's table was built on. Isolating the regime-timing mechanism for a clean test meant it also got isolated from the filter everything else was being compared against. The tell: trade rate on that run was 13.6 trades/ticker/year — essentially identical to §1's **unfiltered** Run A (14.1 trades/ticker/year, PF 1.02), not §7g's filtered baseline. The huge, catastrophic-looking CAGR drop was mostly a regression to the unfiltered baseline's known-worse quality (compounded harder by ~2,000+ capital-constrained rejections from the 3x higher trade volume competing for the same $100k), not a verdict on forced exits.

**Fixed**: `run_regime_timed_replay()` now also applies the same `entry_filter_from_cfg()`/`passes_entry_filter()` stack `run_replay()` uses, exactly like every §7g config. `scripts/sweep_regime_timed_configs.py` turns this filter **ON by default** (`--no-entry-filter` to turn it off), and every config's name now encodes filter state (`"sma=200 buffer=3% filter=on"`) specifically so the existing `sweep_regime_checkpoint.json` from the flawed run — which used unsuffixed names — can never silently collide with, and be skipped as "already done," a corrected run. Both filter states can coexist in the same checkpoint file, clearly labeled; the flawed run's data isn't lost, it's just correctly relabeled as "regime timing alone, no proven filter" — a legitimate, if secondary, comparison point once understood that way, not the main event.

A new regression test (`test_entry_filter_in_cfg_actually_rejects_candidates`) pins this specific failure mode going forward — asserts a filtered run rejects at least one candidate and never produces more trades than an unfiltered run on identical data. 14 tests in this file, 123 across the full related suite, all passing. The fix was also smoke-tested end-to-end (both filter states, checkpoint coexistence confirmed with synthetic data) before handoff.

**Re-run needed** — the corrected command is identical to before, since the filter is now on by default:

```
./scripts/run_sweep_caffeinated.sh scripts/sweep_regime_timed_configs.py --starting-equity 100000
```

This is now the real, apples-to-apples test against §7g's table. Paste the output.

### 9b. The corrected run: real, if modest, improvement — still not a market-beating result

Ran on the fixed 44-ticker/5yr/$100k universe, 6,760s, entry_filter ON (apples-to-apples with §7g):

| Config | N | PF | ExpR | PF-2022 | n22 | LossYrs | CAGR | MaxDD | Sharpe | CapRej | Blocked | FcdExit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| §7g baseline (no regime timing) | 925 | 1.05 | 0.014 | 0.42 | 111 | 2/6 | 2.8% | 37.7% | 0.26 | 383 | — | — |
| sma=200 buffer=0% | 827 | 1.09 | 0.020 | 0.11 | 37 | 3/6 | 3.9% | 34.5% | 0.33 | 355 | 12,897 | 39 |
| sma=200 buffer=3% | 794 | 1.08 | 0.022 | 0.11 | 30 | 3/6 | 4.3% | 33.7% | 0.36 | 342 | 12,050 | 6 |
| sma=200 buffer=5% | 766 | 1.04 | 0.008 | 0.09 | 27 | 2/6 | 2.7% | 32.4% | 0.25 | 332 | 12,933 | 3 |
| sma=100 buffer=3% | 806 | 1.07 | 0.022 | 0.21 | 37 | 3/6 | 1.8% | 36.7% | 0.20 | 349 | 13,895 | 9 |
| sma=150 buffer=3% | 772 | 1.08 | 0.025 | 0.16 | 26 | 3/6 | 3.8% | 31.8% | 0.33 | 338 | 14,295 | 5 |
| **sma=250 buffer=5%** | 741 | **1.14** | **0.046** | 0.07 | 44 | **1/6** | **4.1%** | **28.1%** | **0.36** | 313 | 13,110 | 2 |

**This is the first result all session where a single config beats the established baseline on every axis at once** — PF, expectancy, CAGR, max drawdown, Sharpe, and losing-year count, not one metric improving at another's expense (the usual pattern, e.g. the stop=16 combos in §7g that traded CAGR for a slightly better PF). `sma=250 buffer=5%` (the slowest, most patient trend read of the six tried) is the standout: CAGR 4.1% vs baseline's 2.8%, max drawdown 28.1% vs 37.7% (a real ~10-point cut), Sharpe 0.36 vs 0.26, and only 1 losing year out of 6 instead of 2.

**What's NOT fixed: 2022 itself.** PF-2022 stays weak across every config (0.07–0.21) — similar to or worse than the entry-only filters that already failed in §7g. The drawdown improvement isn't coming from 2022 trades becoming profitable; it's coming from taking far fewer of them (`n22` drops from 111 to 27–44, driven by ~12,000-14,000 candidate-days blocked system-wide by the regime gate) plus a handful of forced exits (`FcdExit`, 2-39) cutting losses on positions caught mid-flip. The mechanism is working as designed — less exposure during a bad stretch, some in-flight damage control — it just isn't turning 2022 into a good year, only a smaller bad one.

**Still nowhere close to SPY.** Even the best config's 4.1% CAGR is roughly 8.4 percentage points/year short of SPY's 12.53% over this same window (§7c). This is the best backtest-only result of the entire session — genuinely better than every one of §7g's nine configs and both original attempts at this module — but it is not a market-beating strategy.

**One more honest flag before treating `sma=250/buffer=5%` as a real finding rather than the best of six guesses**: this is 1 config out of a curated 6, tested on the same 2021-2026 window this whole session has now probed extensively (§8's standing caution applies here too, even though this is a structurally new mechanism, not another re-slice). Two things worth doing before trusting it:

1. **A sequential split** (`scripts/analyze_backtest_windows.py --split-date`, already built, never run against this module) — does the edge hold in the back half of the data on its own, or does it live entirely in one favorable stretch?
2. **The blind walk-forward** recommended since §2.5 and never actually done in this whole session — pick `sma=250/buffer=5%` using data only through some cutoff (say, 2024-12-31), freeze it, then test unmodified on 2025-2026. This is the test that would actually retire the "best of N on a fixed dataset" caveat, for this finding and for every other filter choice this session has made.

Also worth noting: `sma=250` was the SLOWEST/most patient of the six trend reads tried, and it won — a mild signal that an even longer lookback might do better still, though testing that immediately would be exactly the kind of "keep re-slicing the same fixed data" pattern §8 warns against without the walk-forward validation first.

### 9c. The sequential split ran — a real caution, not a clean pass

`scripts/dump_regime_timed_results.py` dumped `sma=250/buffer=5%`'s 742 trades; `scripts/analyze_backtest_windows.py --split-date 2024-01-01` (existing tool, unmodified) ran against them. Year-by-year:

| Year | N | PF | ExpR |
|---|---|---|---|
| 2021 | 97 | 1.38 | 0.128 |
| 2022 | 44 | **0.07** | -0.653 |
| 2023 | 47 | 1.19 | 0.055 |
| 2024 | 159 | 1.19 | 0.089 |
| 2025 | 249 | 1.43 | 0.164 |
| 2026 (partial) | 146 | 1.02 | -0.024 |

Sequential split at 2024-01-01: **ref (2021-2023, n=188): PF 0.91 — net losing.** held_out (2024-2026, n=554): PF 1.24 — solidly positive.

The tool's own verdict line reads "the edge persisted into the untouched later period (held_out PF 1.24 >= 1.0)" — technically correct against its blunt >=1.0 threshold, but read at face value it undersells the real finding. **An investor running this exact rule starting in 2021 would have been net underwater for its first two and a half years**, before 2024-2025's favorable stretch arrived. 2022 itself, again, is a disaster on a per-trade basis (PF 0.07) — the mechanism reduced how many 2022 trades happened (44, vs baseline's 111), it did nothing for the quality of the ones that still occurred.

**Why this matters for trusting `sma=250/buffer=5%` specifically**: it was picked from 6 candidates using full-period (2021-2026) hindsight, and its edge turns out to be concentrated almost entirely in the back half — the same stretch that includes 2025's strong year. That's a meaningfully different, more concerning shape than "a stable edge that happens to also hold up later" — it looks more like *the config that best fit the recent favorable period got selected*, which is exactly the selection-bias risk `sequential_split_stats()`'s own docstring warns this specific test cannot rule out (the parameter choice itself already saw both sides of any split drawn after the fact).

**Verdict, unchanged from §9b in direction, weaker in confidence**: the regime-timing mechanism genuinely does what it was built to do (cut exposure during a bad stretch), and 2024-2026's results are real and encouraging. But `sma=250/buffer=5%` as a *specific parameter choice* has not been shown to generalize — a search over 2021-2023 alone might well have picked a different `(sma_period, buffer_pct)` entirely. The next, more definitive test is the genuine blind walk-forward flagged in §9b item 2 and §2.5 before it: pick the best combo using ONLY 2021-2023 data, freeze that specific choice, then test it unmodified on 2024-2026. That is the test that would actually settle whether this generalizes — this session has recommended it four separate times now (§2.5, §6 item 4, §9b, here) and never yet run it.

### 9d. The blind walk-forward ran — the finding does not survive it

`scripts/walk_forward_regime_timed.py` (new, this session), TRAIN=2021-07-29..2024-01-01, TEST=2024-01-01..2026-07-29, selection metric fixed in advance as TRAIN-period CAGR, capital-constrained at $100,000:

| TRAIN config | N | PF | ExpR | CAGR | MaxDD | Sharpe |
|---|---|---|---|---|---|---|
| **sma=250 buffer=5%** | 328 | 0.83 | -0.105 | **-3.7%** | 33.6% | -0.15 |
| sma=200 buffer=5% | 224 | 0.75 | -0.150 | -6.0% | 32.4% | -0.33 |
| sma=150 buffer=3% | 373 | 0.75 | -0.146 | -6.0% | 33.3% | -0.29 |
| sma=200 buffer=3% | 233 | 0.77 | -0.131 | -6.8% | 33.7% | -0.39 |
| sma=200 buffer=0% | 258 | 0.76 | -0.125 | -8.4% | 34.5% | -0.52 |
| sma=100 buffer=3% | 408 | 0.69 | -0.168 | -8.9% | 38.4% | -0.46 |

Every single TRAIN-period CAGR is negative — 2021-2023 was a genuinely bad stretch for this whole approach, not just for the eventual full-period winner. `sma=250/buffer=5%` is simply the least-bad of six losers on TRAIN, and it wins blind selection: **this matches §9b's full-period winner exactly**, a real point in its favor as a parameter choice.

**Then it was frozen and run once, untouched, on TEST (2024-01-01..2026-07-29): 817 trades, PF 1.17, CAGR -5.1%, MaxDD 22.6%, Sharpe -0.22.**

This is the finding that actually settles it, and it's a clear negative. **PF 1.17 (trade-level profitable) alongside CAGR -5.1% (compounded loss) is a sequence-of-returns signature** — a damaging early stretch within the TEST window, hitting a freshly-capitalized $100k account with the capital-constraint mechanism (§7f) likely starving subsequent good trades of position size, produced a net loss despite the underlying trades being profitable in aggregate. This is not a data error; it is exactly the kind of path-dependent, compounding-order risk that trade-level PF/expectancy numbers structurally cannot see, and precisely why §9b's original headline (4.1% CAGR, computed as one continuous 2021-2026 simulation) cannot be trusted as a standalone property of 2024-2026: in that run, the account arrived at 2024 already carrying TRAIN's losses, a materially different capital base than a fresh $100k, and the SAME 2024-2026 trade sequence produced a different compounded outcome under each starting condition.

**Conclusion.** The regime-timing mechanism itself is a real, verified capability — it does reduce 2022-style exposure and force-close positions on a trend break, exactly as designed (§9, §9b). But `sma=250/buffer=5%` as a *return-generating parameter choice* does not survive the one test built specifically to validate it: selected blind, frozen, and evaluated once on data it never touched, it lost money on a period it was never allowed to see. Whatever CAGR the full-period run reported was contingent on a specific capital path, not a repeatable edge. This closes the loop on the regime-timed-overlay line of investigation for now — per §8's standing discipline, the honest move is not to search further parameter combinations on this same fixed 2021-2026 window (that would just be curve-fitting the walk-forward itself), but to treat this specific finding as settled: not yet a market-beating strategy, and the mechanism that does work (exposure reduction) needs a sizing/capital-allocation fix, not another trend-lookback search, before it could plausibly become one.

### 9e. The sizing fix ran on real data — it helps the mechanism it targets, but a second confound (reject-vs-resize) hid that under the fixed-capital model, and a data-refresh discrepancy means this isn't a byte-for-byte replay of §9d

§9d's diagnosis was specific: PF 1.17 (trade-level profitable) alongside CAGR -5.1% (compounded loss) is a sequence-of-returns signature, not a signal-quality problem. `scripts/equity_curve.py::simulate()` sizes every new position off *current* equity — after a losing stretch draws current equity down, every subsequent position (including the recovery trades that would otherwise repair the drawdown) gets sized smaller, which can turn a trade sequence with positive total R into a net loss purely as an artifact of *when* the losses landed relative to the wins, not the wins' and losses' magnitudes.

**The fix.** `simulate()` now takes a `sizing_mode` argument. `"current_equity"` (unchanged, still the default — every prior result in §7–§9d used this and is unaffected) sizes off equity right now. The new `"high_watermark"` mode sizes off *peak* equity ever reached, so a losing stretch no longer shrinks the dollar size of the trades that would recover it. The explicit, un-free tradeoff: while underwater, high-watermark sizing risks a *larger percentage of actual current capital* than current-equity sizing would — it is not a strict improvement, it trades one risk for a different one, which is exactly why it's an opt-in mode rather than a default.

**Synthetic proof the mechanism does what it claims** (`tests/test_equity_curve.py`, hand-verified by arithmetic, not just asserted): two trades, a full stop-out (-1R) followed by a strong recovery (+1.05R), 10% risk/trade, $100k starting capital.

| Sizing mode | Trade 1 (risked / P&L) | Trade 2 (risked / P&L) | Final equity |
|---|---|---|---|
| `current_equity` | $10,000 / -$10,000 | $9,000 (10% of drawn-down $90k) / +$9,450 | **$99,450 — net loss**, despite +0.05R total |
| `high_watermark` | $10,000 / -$10,000 | $10,000 (10% of peak, unchanged) / +$10,500 | **$100,500 — net gain**, identical trade sequence |

Same two trades, same total R, opposite sign of outcome — this is precisely the §9d pathology in miniature, and `high_watermark` is a direct countermeasure to it. A companion test confirms the two modes are byte-identical whenever equity only ever rises (peak == equity, so there's nothing for the modes to disagree about) — the fix only activates during an actual drawdown, which is the point.

**The real run — and an honest reproducibility caveat first.** `scripts/dump_regime_timed_results.py --sma-period 250 --buffer-pct 5.0 --start 2024-01-01 --end 2026-07-29` was re-run to get raw TEST-period trades (the walk-forward checkpoint only ever stored aggregates). It produced **761 trades**, not the 817 §9d's official TEST run recorded, and — under `current_equity` sizing, fixed $100k capital, the exact settings §9d used — this fresh dump's CAGR came back **+9.9%**, not §9d's -5.1%. Same config string, same nominal window, different trades, different sign. The most likely cause: `run_regime_timed_replay`'s bar-fetch window is keyed to `end`, and the two runs happened on different calendar days, so each hit the on-disk bar cache differently and pulled a slightly different trailing slice of (possibly since-revised) price history — not a bug in the sizing-mode work, but a real reminder that a live-data backtest isn't bit-for-bit reproducible across two runs taken days apart. **This means the comparison below is not a clean isolation of "the same 817 losing trades, re-sized" — it's an independent second sample over the same window/config**, which is a weaker but still real test.

**Within that one consistent 761-trade sample, the two sizing modes gave a directly contradictory-seeming pair of results:**

| Mode | Sizing | CAGR | MaxDD | Trades executed | Rejected (insufficient cash) |
|---|---|---|---|---|---|
| Fixed $100k capital (§7f, §9d's mode) | `current_equity` | **9.9%** | 18.3% | 329 | 432 |
| Fixed $100k capital | `high_watermark` | **1.3%** | 19.7% | 293 | 468 |
| `--unlimited-capital` (isolates pure sizing effect) | `current_equity` | **20.5%** | 35.7% | 761 | 0 |
| `--unlimited-capital` | `high_watermark` | **24.0%** | 41.5% | 761 | 0 |

Under fixed capital, `high_watermark` looks *worse* (1.3% vs 9.9%) — the opposite of the synthetic proof. But under `--unlimited-capital`, where every one of the 761 candidates executes and the only variable left is position size, `high_watermark` wins as designed (24.0% vs 20.5%, consistent with the synthetic result and the theory). The explanation is a second, previously-unconsidered confound: §7f's fixed-capital model **rejects, rather than resizes,** a candidate whose requested position exceeds available cash. `high_watermark` requests *larger* dollar positions than `current_equity` during any drawdown (that's the entire point of the fix), and a larger request against the same limited cash gets rejected more often (468 vs 432 rejections, 36 fewer trades actually taken). So on the fixed-capital model, `high_watermark`'s benefit-per-executed-trade is real but gets swamped by a new opportunity-cost tax: good trades that current_equity sizing could afford now get turned away outright.

**Conclusion.** The core mechanism is confirmed, not falsified — `high_watermark` genuinely improves realized CAGR when every signal is allowed to trade (24.0% vs 20.5%, the cleanest apples-to-apples cut of this data). But as a drop-in replacement inside the fixed-capital, reject-don't-resize model every other result in this doc uses, it currently underperforms, because it collides with that model's rejection rule rather than because the sizing idea is wrong. This is not yet a solved problem: the honest next step, if this line is worth continuing, is either (a) changing the capital model to partially resize a rejected candidate down to available cash instead of rejecting it outright, or (b) accepting `current_equity` as the safer default under fixed capital and treating `high_watermark` as a result that only helps in an unconstrained-capital regime. Given the reproducibility caveat above, any of this should be re-checked against a same-day dump-and-compare (both `equity_curve.py` calls run against one `dump_regime_timed_results.py` output, same run, same day) before being treated as final. `--sizing-mode` is wired into `scripts/sweep_regime_timed_configs.py` and `scripts/walk_forward_regime_timed.py` as well (both default to `current_equity`, so re-running either without the flag reproduces §9b/§9d exactly).

### 9f. The partial-resize capital model was built and run — it closes almost the entire gap, but doesn't clearly beat the existing baseline, and it's specifically a fix for `high_watermark`, not a free upgrade for `current_equity`

§9e's conclusion named the actual next step: "changing the capital model to partially resize a rejected candidate down to available cash instead of rejecting it outright." Built now. `simulate()` takes a new `capital_mode` argument — `"reject"` (default, unchanged, everything in §7–§9e used this) skips a candidate outright when its requested position value exceeds available cash; `"partial_resize"` instead opens it at whatever smaller size DOES fit, with `dollar_risk` scaled down in the same proportion as `position_value` so the trade's own stop-distance-implied risk stays internally consistent. A candidate that would need $0 cash to "fit" (cash already fully exhausted by an overlapping open position) still gets rejected outright under `partial_resize` too — resizing to a $0 position isn't a smaller trade, it's not a trade.

**Synthetic proof first** (`tests/test_equity_curve.py`, hand-verified): a full stop-out (-1R) that consumes 60% of a $100k account, followed by a +1.05R recovery attempt sized at 60% of *peak* (high_watermark, unchanged by the loss) — the exact §9e collision, in miniature. `reject` skips the recovery trade entirely (final equity stays at $40,000, the loss just sits there). `partial_resize` opens the recovery trade at whatever cash remains ($40,000, not the full $60,000 requested), still profits from the win, and ends at $82,000 — dramatically better than `reject`, though (correctly) short of a full recovery to $100,000, since the capital constraint is still real, just not a hard wall anymore.

**The real run, same 761-trade TEST-period sample §9e used** (`sma250_buffer5_test_period.json`, $100k fixed capital, 1% risk/trade):

| Sizing mode | Capital mode | CAGR | MaxDD | Sharpe | Trades executed | Rejected | Resized |
|---|---|---|---|---|---|---|---|
| `current_equity` | `reject` (§9e baseline) | **9.9%** | 18.3% | 0.72 | 329 | 432 | — |
| `high_watermark` | `reject` (§9e, broken) | **1.3%** | 19.7% | 0.21 | 293 | 468 | — |
| `high_watermark` | **`partial_resize` (new)** | **9.2%** | 21.4% | 0.63 | 387 | 374 | 147 |
| `current_equity` | `partial_resize` (new, for comparison) | **8.1%** | 21.3% | 0.60 | 408 | 353 | 152 |

**Read honestly, not just mechanically:**
- The fix does what §9e diagnosed it should: `high_watermark`'s CAGR jumps from 1.3% to 9.2% — nearly all the way back to `current_equity`'s 9.9%, confirming the rejection rule, not the sizing idea, was the thing actively suppressing the mechanism.
- **But `high_watermark` + `partial_resize` still does not clearly beat the plain §7f/§9d baseline** (`current_equity` + `reject`, 9.9% CAGR, 0.72 Sharpe, 18.3% MaxDD). It's now *competitive* — slightly lower CAGR, meaningfully lower Sharpe, and a deeper drawdown (21.4% vs 18.3%, the un-free tradeoff §9e's docstring warned about: more capital deployed during exactly the periods it's underwater). This is a genuine improvement over a broken result, not a new best result.
- **`partial_resize` is not a free upgrade in general** — applying it to `current_equity` (which wasn't colliding with the rejection rule the way `high_watermark` was) made things slightly *worse* (9.9% → 8.1% CAGR, 0.72 → 0.60 Sharpe). Letting more marginal, cash-starved trades open at reduced size adds noise when the sizing mode wasn't the thing broken in the first place. `--capital-mode` therefore defaults to `reject` and should stay that way for `current_equity`; `partial_resize` is specifically indicated for `high_watermark`, not a blanket setting to turn on everywhere.

**Conclusion.** §9e's mechanism is now confirmed AND usable under a real fixed-capital model, not just under the `--unlimited-capital` isolation test. But "usable" is not the same as "an improvement worth shipping" — on this specific real TEST-period sample, the fixed version of `high_watermark` sizing lands close to, not clearly above, the existing `current_equity` baseline this whole document's other numbers are built on. Given a deeper drawdown and lower Sharpe for essentially the same CAGR, there is not yet a case for changing the platform's default sizing mode. `--capital-mode partial_resize` is wired into `scripts/equity_curve.py` (`--sizing-mode`/`--capital-mode` both opt-in, `current_equity`/`reject` remain the unchanged defaults) — not yet wired into the sweep scripts, since there's no evidence yet that it's worth sweeping over.

## 10. The core, unfiltered stock-picking engine itself was walk-forward tested — it does not beat SPY, on TRAIN or on a frozen blind TEST, under any of its own real configurations

Every test in sec 7–9 examined something layered ON TOP of `rules/swing_buy_rules.py`'s own picks — an entry filter, a regime-timing exit overlay, a separate intraday strategy. None of them asked the more basic question directly: does the core engine's own stock selection, with zero extra gate, have real edge? `scripts/walk_forward_core_engine.py` (2026-07-31) answers this. It calls `engine/backtest_engine.py`'s `run_replay()` — the exact pipeline paper trading runs — with **no `entry_filter` override at all**, and sweeps the two real, already-configured knobs `scripts/sweep_backtest_configs.py`'s own docstring drew the safe-to-sweep line at: `risk_level` (the four real tiers already in `config.yaml` — CONSERVATIVE/MODERATE/AGGRESSIVE/TURBO, not synthetic values) and `sell_rules.take_profit.r_multiple` (2.0/3.0/4.0). Same universe (44-ticker "Run D"), same 5-year window and 2024-01-01 split as sec 9d, locked before any result was seen.

**TRAIN (2021-08-02..2024-01-01, includes the 2022 bear market) — all 12 real configurations:**

| Config | CAGR | PF | Sharpe |
|---|---|---|---|
| CONSERVATIVE, r=4.0 (**TRAIN winner**) | **2.1%** | 1.02 | 0.08 |
| CONSERVATIVE, r=3.0 | 1.8% | 0.95 | 0.08 |
| TURBO, r=2.0 | 1.8% | 0.93 | 0.10 |
| TURBO, r=4.0 | 1.5% | 0.95 | 0.03 |
| CONSERVATIVE, r=2.0 | -0.5% | 0.96 | -0.07 |
| MODERATE, r=4.0 | -1.0% | 0.95 | -0.04 |
| AGGRESSIVE, r=4.0 | -2.7% | 0.96 | -0.10 |
| AGGRESSIVE, r=3.0 | -3.8% | 0.95 | -0.19 |
| AGGRESSIVE, r=2.0 | -4.9% | 0.92 | -0.13 |
| MODERATE, r=3.0 | -7.9% | 0.93 | -0.43 |
| **TURBO, r=3.0 (config.yaml's LIVE setting)** | **-9.1%** | 0.95 | -0.57 |
| MODERATE, r=2.0 | -14.6% | 0.94 | -0.83 |

Every single config PF is at or below ~1.0. **SPY's own TRAIN-period CAGR over this window (computed from the real Alpaca SIP daily closes already fetched and validated for the intraday work — an independent data source from the yfinance feed `run_replay()` used, cross-checking the comparison): 3.5%.** Even the best of 12 real configurations (2.1%) did not beat SPY on TRAIN. config.yaml's actual currently-deployed setting (TURBO, r=3.0, sec 9d/9f's r_multiple default) scored **-9.1% CAGR** on this same window — a real, currently-live configuration that would have lost money against a benchmark that gained 3.5%.

**TEST (2024-01-02..2026-07-30, frozen, blind) — the TRAIN-selected winner only, per the pre-committed rule:** CONSERVATIVE/r=4.0 scored **CAGR 10.6%, PF 1.1, Sharpe 0.72, MaxDD 15.2%** on 1,124 trades. In isolation this looks like a real improvement over TRAIN's 2.1% — **it is not.** SPY's own CAGR over the identical TEST window was **19.14%** (total return +56.9% — an exceptionally strong bull run). The strategy's shortfall against SPY actually WIDENED from TRAIN to TEST: 1.4 points behind on TRAIN (3.5% − 2.1%), 8.5 points behind on TEST (19.14% − 10.6%). The TRAIN→TEST CAGR jump is not the strategy generalizing well — it's the whole market rallying hard in the test window, with the strategy capturing roughly half of that rally, same broad shape SPY itself shows: TRAIN CAGR 3.5% → TEST CAGR 19.14%, a >5x jump in the BENCHMARK alone, so a similarly-shaped jump in the strategy's own absolute number carries no information about the strategy's own skill once read against what the market was doing.

**Conclusion — the most decisive finding across this whole document.** This is not "another filter failed" or "another overlay didn't quite clear the bar." This is the foundational stock-selection signal itself, completely unfiltered, tested across every one of its own real, already-configured risk/reward settings, on a genuine blind walk-forward: none of the 12 configurations beat SPY on TRAIN, and the single config selected blind on TRAIN also did not beat SPY on a frozen TEST it never saw during selection — nor did the currently-deployed live setting (TURBO, r=3.0), which actively lost money on TRAIN. Every filter and overlay this session built (sec 7g's nine configs, sec 9's regime-timing exits, sec 9e/9f's sizing fix, the intraday momentum module) was trying to improve something layered on top of a base signal that, at its root, does not appear to carry real edge over passively holding SPY. That reframes the standing question this whole document has been chasing: the issue is very unlikely to be "which filter/overlay/parameter is missing" and much more likely to be that `rules/swing_buy_rules.py`'s scoring logic itself — the bucket weights, the indicator choices, the actual definition of what makes a "good" entry — needs to change, which is a different and larger conversation than anything backtest-only parameter sweeping in this document has attempted, and was explicitly out of scope for every sweep here (see `scripts/sweep_backtest_configs.py`'s own docstring on why it stopped short of that line).

## 11. SPY mean reversion (RSI(2)/Connors-style) walk-forward tested — real, positive, risk-adjusted edge on blind TEST, but fails the pre-committed CAGR bar because the strategy is in the market too rarely to compound

Sec 10's finding located the likely root cause in `rules/swing_buy_rules.py`'s own scoring logic, not in any filter/overlay/sizing mechanism — but it didn't rule out that a genuinely different signal family (not a variant of the existing momentum/trend engine at all) could still have real edge. Trinath proposed RSI(2)/Connors-style mean reversion on SPY as that different direction: long-only, buy short-term oversold dips within a longer uptrend, exit on a snap-back — the opposite signal shape from everything else in this document. Built as `engine/rsi2_mean_reversion_backtest.py` (13 new unit tests, all passing — RSI(2) cross-checked against an independent plain-Python Wilder-seeded implementation, not just against itself) and walk-forward tested via `scripts/walk_forward_rsi2.py` (2026-08-02), against the same real SPY daily OHLCV derived from this session's Alpaca SIP minute-bar fetch (2016-01-04 to 2026-07-30) — no network call needed to run this one.

**Design, locked before any result was seen:** trend filter close > SMA(200); entry when RSI(2) < threshold while trend holds, filled at the NEXT day's open (not the signal day's own close — the conservative, no-lookahead assumption used everywhere else this session); exit at the close either strategy crosses back (`sma5`: close > SMA(5)) or (`rsi70`: RSI(2) > 70), with a 10-trading-day time-stop as a backstop. Grid: `rsi_threshold ∈ {5, 10} × exit_mode ∈ {sma5, rsi70}` (4 combos — Trinath's own spec called for minimal tuning over already-well-studied canonical values, not a wide search). Cost assumption fixed (not swept) at 10bps round-trip, the midpoint of the 5-10bps range the spec itself suggested for one of the most liquid instruments that exists. Split: fixed calendar date 2023-01-01 (TRAIN 2016-2022, ~7 years spanning the 2018 selloff, 2020 crash, and 2022 bear market; TEST 2023-2026, ~3.5 years, never touched during selection). Selection metric: highest TRAIN CAGR (via `scripts/equity_curve.simulate()`, `risk_pct=100` — exploiting the fact that "100% of capital in the trade, 0% out" with no overlapping positions makes that call mathematically identical to real full-allocation sizing), subject to a 30-trade TRAIN floor, matching sec 10's convention.

**TRAIN — all 4 combos:**

| rsi_threshold | exit_mode | n_trades | win% | PF | expectancy_r | CAGR | MaxDD | Sharpe |
|---|---|---|---|---|---|---|---|---|
| 5 | sma5 | 27 | 70.4% | 1.19 | 0.0012 | 0.6% | 8.5% | 0.11 |
| 5 | rsi70 | 26 | 65.4% | 1.04 | 0.0003 | 0.1% | 9.2% | -0.02 |
| 10 | sma5 | 47 | 70.2% | 1.31 | 0.0015 | 1.3% | 8.5% | 0.31 |
| **10 / rsi70 (TRAIN winner)** | | 45 | 73.3% | **1.39** | 0.0021 | **1.7%** | 9.0% | 0.39 |

All 4 combos show real, positive trade-level quality (win rates 65-73%, PF 1.04-1.39) — a different signature from sec 10's core-engine sweep, where PF sat at or below ~1.0 across all 12 configs. But TRAIN CAGR is thin everywhere (0.1%-1.7%) because trade frequency is low: 26-47 trades over ~7 years, meaning the strategy is flat (in cash) for most of the period even though the trades it does take tend to win.

**TEST — frozen winner (rsi_threshold=10, exit_mode=rsi70), run once, unmodified:** 37 trades, win_rate 78.4%, PF **2.05**, expectancy_r 0.0037, CAGR **4.0%**, total return 14.2%, MaxDD **4.5%**, Sharpe **0.81**. Trade quality did not degrade from TRAIN to TEST the way every prior walk-forward this session showed — it *improved* (PF 1.39→2.05, win rate 73.3%→78.4%, Sharpe 0.39→0.81), a genuinely different and encouraging pattern versus sec 8's intraday result (PF 1.251→1.01) and sec 10's core-engine result (shortfall vs SPY widening from TRAIN to TEST).

**Pre-committed success criteria check (locked in the module docstring before this run):**

| Criterion | Bar | Actual | Result |
|---|---|---|---|
| profit_factor | > 1.1 | 2.05 | PASS |
| Sharpe | >= 0.5 | 0.81 | PASS |
| max_drawdown_pct | <= 35% | 4.5% | PASS |
| CAGR | >= 7% | **4.0%** | **FAIL** |

**Verdict: does not clear all pre-committed success criteria.** Three of four bars pass comfortably — this is not a fragile, coin-flip result the way sec 8's intraday PF-net=1.01 was, and it is not a signal that fell apart out of sample the way sec 10's core engine did. The single miss is CAGR, and the reason is legible rather than mysterious: at 100% allocation only while a position is open, and only ~11 trades/year each held a handful of days, the strategy simply isn't in the market often enough to compound past 4% CAGR even with genuinely good per-trade odds (78% win rate, PF 2.05) and a very low 4.5% drawdown. This reads as a **capital-utilization problem, not a signal-quality problem** — the edge that is there looks real on this evidence, it's just too infrequently expressed to hit the CAGR bar as a standalone, fully-allocated single-instrument strategy. Two directions worth naming without having built or tested either (both change scope beyond what was asked this round): running the same signal concurrently across a small basket of similarly liquid, mean-reverting instruments to keep capital deployed more of the time, or accepting the lower CAGR as the cost of the very low drawdown/high Sharpe profile if the eventual use case tolerates a smaller allocation of total capital to this sleeve rather than the whole account. Neither has been evaluated — this section reports the strategy exactly as specified and tested, not a modified version of it.

## 12. RSI-2 SPY's role, locked: a satellite sleeve for risk-adjusted improvement, not a raw-CAGR competitor to SPY — plus a real portfolio-blend backtest

Trinath's 2026-08-02 direction ("Path A"): stop trying to force RSI-2 SPY to beat SPY on raw CAGR — that was never its shape (sec 11: ~4% CAGR at full allocation, ~78% win rate, PF ~2.0, Sharpe ~0.8, MaxDD ~4.5%, ~11 trades/year, ~10-20% time-in-market). Instead, treat it explicitly as **a high-Sharpe, low-CAGR satellite sleeve**: equity exposure with bond-like drawdowns, sized alongside a passive SPY (or broader market) core to improve the BLENDED portfolio's risk-adjusted characteristics, not to replace or outperform the core on its own. This is the sleeve's locked, documented role going forward — not a temporary framing to be "fixed" later. Sizing the sleeve (Trinath named 20-40% of equity allocation as an example range) is an explicit strategic choice for Trinath to make, not a backtest question this document answers.

**Portfolio-blend backtest** (`scripts/portfolio_blend_rsi2_spy.py`, 2026-08-02) — combines the frozen sec-11 sleeve (rsi_threshold=10, exit_mode=rsi70, 10bps cost) with passive buy-and-hold SPY at several illustrative weights, using real daily-return data, reported over two windows:

**TEST (2023-01-01 to 2026-07-30), the honest, blind, out-of-sample window:**

| RSI2 weight | SPY weight | CAGR | Vol | Sharpe | MaxDD |
|---|---|---|---|---|---|
| 0% (100% SPY, baseline) | 100% | 20.5% | 14.8% | 1.33 | 19.0% |
| 20% | 80% | 17.2% | 12.0% | 1.38 | 16.2% |
| 30% | 70% | 15.5% | 10.6% | 1.42 | 14.7% |
| 40% | 60% | 13.8% | 9.2% | 1.45 | 13.3% |
| 100% (sleeve only) | 0% | 3.8% | 4.4% | 0.88 | 4.5% |

**FULL (2016-01-04 to 2026-07-30), a larger sample but explicitly NOT blind** (the sleeve's own parameters were selected using TRAIN performance inside this same window, so read this table as illustrative context, not independent validation):

| RSI2 weight | SPY weight | CAGR | Vol | Sharpe | MaxDD |
|---|---|---|---|---|---|
| 0% (100% SPY, baseline) | 100% | 13.2% | 17.9% | 0.78 | 34.2% |
| 20% | 80% | 11.2% | 14.4% | 0.81 | 28.8% |
| 30% | 70% | 10.1% | 12.6% | 0.83 | 25.9% |
| 40% | 60% | 9.1% | 10.9% | 0.85 | 23.1% |
| 100% (sleeve only) | 0% | 2.1% | 4.2% | 0.52 | 9.0% |

(Sanity check: the 100%-sleeve row's CAGR/MaxDD closely reproduce sec 11's own walk-forward numbers — 3.8% vs 4.0% CAGR, 4.5% vs 4.5% MaxDD on TEST — the small CAGR gap is purely a different, both-legitimate annualization convention, 252-trading-day daily compounding here vs 365.25-calendar-day trade-close compounding in `scripts/equity_curve.py`; Sharpe differs more because this script computes a true daily Sharpe over all 252 days/year including flat zero-return days, while `equity_curve.py`'s Sharpe resamples to month-end and annualizes by sqrt(12) — not the same measurement, so don't expect an exact match, only a directionally consistent one, which it is.)

**Read honestly:** in BOTH windows, blending in the sleeve at every weight tested (20/30/40%) monotonically LOWERS CAGR and volatility and MaxDD relative to 100% SPY (expected — the sleeve's own return is far below SPY's and it spends most days flat), and monotonically RAISES Sharpe within that same 20-40% range (TEST: 1.33→1.45; FULL: 0.78→0.85) — the diversification mechanism Trinath's framing predicted is real and shows up in both an honest blind window and a larger illustrative one, not just one or the other. Sharpe does NOT keep rising all the way to 100% sleeve weight, though (TEST: 1.45 at 40% → 0.88 at 100%; FULL: 0.85 at 40% → 0.52 at 100%) — there is a sweet spot in the blend, not a monotonic "more sleeve is always better" relationship, which matters if Trinath or future-Trinath is tempted to push the allocation past the 20-40% range he named. One more honest caveat: TEST-period SPY itself had an unusually strong run (sec 10 already flagged this — 19-20% CAGR is well above SPY's long-run historical average), so the "cost" of diversifying away from 100% SPY looks larger in this specific table than it would in a more typical SPY environment; the FULL window's more moderate SPY CAGR (13.2%) is probably the more representative baseline to weigh the CAGR-vs-Sharpe tradeoff against.

**Documented role (per Trinath's explicit request, so future review doesn't try to "fix" this into something it isn't):** RSI-2 SPY is a high-Sharpe, low-CAGR sleeve intended to improve a portfolio's risk-adjusted returns and reduce drawdowns when blended with a passive core — not to beat SPY on raw CAGR standalone, and not a candidate for further filters, regime overlays, or parameter tuning (sec 9's regime overlay and sec 8's intraday momentum both show what happens when a clean, canonical signal gets "enhanced" — it doesn't reliably help, and it did not clear this session's own success-criteria bar in either case). The canonical RSI(2)/SMA(200)/SMA(5)-or-RSI(2)>70 rule set validated in sec 11 is the version that cleared 3 of 4 pre-committed criteria; it should be implemented as-is if operationalized, not iterated on further within this line of research.
