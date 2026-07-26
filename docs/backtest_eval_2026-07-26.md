# Stage 1 Backtest Evaluation — 2026-07-26 run

**Run:** `output/backtest_results/20260726_085600` | 60 tickers, 2023-07-27 → 2026-07-26
**Result:** 29,882 candidate-days scored, **0 trades**, max score 52.94%, threshold floor 50.0%

## Verdict

The zero-trade result is **not** evidence that the strategy has no edge. It is a
**measurement artifact**: the score scale is compressed to a ~72.5% ceiling while the
threshold still assumes a 0–100% scale. The backtest is currently measuring the
scoring plumbing, not the strategy.

Evidence: the run is not marginal-but-honest. `max_score_pct` is **identical** (52.94)
across the 2026-07-24 and 2026-07-26 runs, and only **0.1%** of 29,882 candidate-days
reach 48%. A distribution with a hard right edge that doesn't move between runs is a
ceiling, not a signal.

---

## Root cause: three compounding compressions

### 1. The 1.25× redistribution cap — costs 17.0 pp of ceiling (largest single factor)

`rules/swing_buy_rules.py` L955–975. In Stage 1, EXTERNAL + SENTIMENT_MACRO +
MARKET_BREADTH are all unavailable = 42% of decision weight.

```
w_unavail = 0.42, w_avail = 0.58
scale_uncapped = 1 + 0.75 × (0.42/0.58) = 1.5431   → ceiling 89.5%
scale applied  = min(1.5431, 1.25)     = 1.25      → ceiling 72.5%
```

The cap's stated intent (2026-07-21 review) was *"no bucket may dominate the entire
score."* Because `scale` is applied **uniformly** to every available bucket, capping it
does not change the *relative* mix at all — TREND/MOMENTUM/VOLUME_PA keep the exact same
ratio to each other whether scale is 1.25 or 1.54. The cap therefore achieves **none** of
its stated purpose here and only lowers the ceiling. It is the wrong instrument: a
relative-dominance guard implemented as an absolute-magnitude clamp.

### 2. Deliberate 25% dead weight — costs 10.5 pp of ceiling

Intentional and defensible in isolation ("missing evidence should cost something").
The problem is that it is **never reflected in the threshold**, so the cost is paid twice.

### 3. `qual_mult` double-counts bucket completion — costs the most in practice

`_qualification_multiplier` (L62–98) is applied on top of a contribution that is
*already* `points/max_points`, making the effective contribution ≈ pct²:

| Bucket completion | Effective contribution | Penalty |
|---|---|---|
| 100% | 1.000 | 0 pp |
| 90%  | 0.855 | 15 pp |
| 80%  | 0.704 | **30 pp** |
| 70%  | 0.560 | 44 pp |
| 60%  | 0.420 | 58 pp |

The docstring's own design goal — *"above min_pct contributes proportionally"* — is not
what the code does; `points/max_points` was already the proportional term. Real
candidates live at 70–85% bucket completion, never 100%, so this is the dominant
real-world compressor.

### Net effect on a realistic candidate

| Candidate | Score | vs. 50% threshold |
|---|---|---|
| Perfect (100/100/100) + full vol bonus | 76.5% | clears |
| Excellent (90/85/80) + full vol bonus | 61.2% | clears |
| Strong (85/80/75), no vol bonus | 51.7% | barely |
| **Observed backtest max** | **52.94%** | +2.94 |

A genuinely strong setup lands at ~52%, against a threshold hard-floored at 50.0
(`dynamic_thresholds.py` L170) and pushed to 52–55% by any breadth/VIX adjustment. There
is roughly **2 points of headroom**, which is why the near-miss table is a wall of
0.37–3.05 pp deficits.

### Precedent: this exact bug was already fixed once

VOLATILITY_EXPANSION had its weight zeroed on 2026-07-15 for precisely this reason —
its 7% weight *"capped every stock not in a squeeze at a 93% maximum composite — a
permanent drag that contradicted the bucket's intent."* (L160–167). The current
situation is the same defect at **four times the magnitude** (72.5% vs 93%), reached by a
different mechanism.

---

## Status: P0 implemented and verified 2026-07-26

All three P0 fixes are in `rules/swing_buy_rules.py`. Regression tests added to
`tests/test_scoring_sanity.py` (5 new). Suite: **375 passed, 99 skipped**.

**Controlled A/B** — identical tickers (BABA/CLSK/HOOD), identical window
(2024-01-01 → 2025-01-01), identical indicator backend, only the scoring change differs:

| | Baseline | Fixed |
|---|---|---|
| Max score | 51.18% | **78.89%** |
| Mean score | 25.09% | 39.37% |
| ≥48% | 1.1% | 33.3% |
| Trades | 2 | **70** |

The right edge of the distribution moved from 51 to 79, which is the ceiling being
released. `simulate_forward_exit` executed for the first time in this codebase's history.

> **Caveat on absolute numbers.** `pandas_ta` has no wheel for Python < 3.12, so both arms
> ran on `engine/ta_fallback.py` with `TP_REQUIRE_REFERENCE_TA=0`. That is valid for the
> A/B (the divergence is common to both arms and cancels) but these figures are **not**
> comparable to the 08:42 production run. Re-run on Python 3.12+ before treating any P&L
> number below as real.

### What the fix revealed — read this before celebrating

Wider run, 6 tickers × 3 years, fixed code: **302 trades, 29.8% win rate, profit factor
1.1, avg outcome +0.42%, avg hold 6.9d** (exits: 208 stop_loss, 71 take_profit, 23 time).
The 1-year 3-ticker window came in at profit factor **0.99**, avg **−0.06%**.

That is a thin-to-nonexistent edge. The zero-trade result was hiding a measurement bug;
removing it does not create an edge, it just makes the edge measurable for the first time.
The stop_loss:take_profit ratio of roughly 3:1 against a 3.0 R-multiple target is the
first thing to look at — the exit model may be badly matched to the entry signal.

**The P1 work below is now the real work.** P0 only bought the ability to measure.

---

## What needs to be done

### ~~P0 — Make the scale self-consistent~~ ✅ done 2026-07-26

1. **Fix the redistribution cap to guard what it claims to guard.** Replace the scalar
   `min(scale, 1.25)` with a genuine per-bucket effective-weight check
   (`effective_weight / sum(effective_weights) ≤ some share`), or drop the cap when
   redistribution is uniform. `effective_weights` is already computed at L985 — the data
   for a correct guard exists; it just isn't the thing being clamped.
   *Recovers 17.0 pp of ceiling.*

2. **Normalize the threshold to the achievable scale.** The score's denominator now
   changes with data availability, but the threshold is a fixed 0–100 constant. Either
   express the score as a % of *achievable* (`weighted_sum / (w_avail × scale)`) or scale
   the threshold by the same factor. Without this, every future outage silently raises the
   real bar. Also revisit the hard `max(50.0, ...)` floor — it makes every favorable
   regime/breadth credit a no-op at TURBO.

3. **Resolve the `qual_mult` double-count.** Pick one: either contribute
   `points/max_points` (linear, drop `qual_mult` from the product) or contribute
   `qual_mult` alone. Applying both is not a documented design choice — the docstring
   describes single application.

---

## Follow-up analysis: why the edge looks nil (2026-07-26, second pass)

**The near-zero profit factor is mostly a backtest fidelity artifact, not a strategy
result.** Do not tune `config.yaml` against these numbers yet.

### The giveback is the whole story

| Exit reason | n | median MFE | median MAE | median hold |
|---|---|---|---|---|
| stop_loss | 208 | **+3.33%** | −8.32% | 3d |
| take_profit | 71 | +24.39% | −1.50% | 8d |
| time_based_close | 23 | +8.91% | −2.44% | 20d |

The 208 losers did not go straight down. They went **up** first — 53.8% reached +3%,
38.0% reached +5%, and 17.8% reached +10% — and then round-tripped through a fixed stop
for a full ~6.3% loss. Meanwhile the winners barely dip: only 24.4% ever traded 3% below
entry, only 7.8% ever traded 5% below.

That combination — losers that rally first, winners that never dip — is the signature of a
missing trailing stop, not a bad entry signal.

### The backtest is not replaying the real exit logic

`config.yaml`'s `stop_machine.SWING` defines a 6-state ratcheting stop that production
uses on every live cycle:

```
breakeven_r: 0.5        -> at +0.5R, stop ratchets to entry + 0.05R
profit_protect_r: 1.0   -> at +1.0R, lock 0.25R and trail 1.5xATR
trend_trail_r: 2.0      -> at +2.0R, aggressive 1.5xATR trail from high watermark
```

`simulate_forward_exit` implements **none** of it — a fixed stop and a fixed 3R target for
the entire hold. The module docstring is honest that this is a "KNOWN SIMPLIFICATION,"
but with 302 trades now flowing it is no longer a minor one:

| Stage reached before actual exit | n | of those, still ended NEGATIVE |
|---|---|---|
| breakeven_r (0.5R) | 213 / 302 | **123** |
| profit_protect_r (1.0R) | 163 / 302 | 73 |
| trend_trail_r (2.0R) | 111 / 302 | 23 |

123 trades hit the breakeven trigger and still took a full stop-out. In production those
exit at roughly flat.

**Counterfactual, breakeven ratchet alone** (stages 4 and 5 not modelled): profit factor
**1.10 → 2.71**, avg trade **+0.42% → +2.97%**.

> **This counterfactual is an upper bound, and deliberately labelled as such.** It converts
> losers to breakeven but does *not* model the offsetting cost — a trade that reaches
> +0.5R, pulls back to scratch, and would have gone on to +3R gets cut short. MFE/MAE do
> not preserve intra-trade ordering, so that cost cannot be estimated from the summary
> data at all. The true figure is somewhere in **1.10–2.71**, and the only way to find it
> is to replay the state machine bar-by-bar. Treat 2.71 as motivation to build that, never
> as a result.

### Secondary lever: the score threshold

Re-filtering the same 302 trades by score (not a re-run — entries would differ):

| Bar | n | win rate | avg | PF |
|---|---|---|---|---|
| ≥45 (current) | 299 | 30.1% | +0.50% | 1.12 |
| ≥55 | 204 | 32.4% | +1.01% | 1.25 |
| ≥60 | 155 | 34.8% | +1.58% | 1.40 |
| **≥65** | 94 | 37.2% | +2.48% | **1.60** |
| ≥70 | 37 | 29.7% | +0.55% | 1.12 |

Monotonic improvement to ~65, then it breaks down at 70 (n=37 — that bucket is noise, and
chasing it would be curve-fitting). Pearson r(score, outcome) = **+0.087**: real but weak.
So the scoring model does carry signal, and there is room to raise the bar — but this is
the *second* lever, worth maybe 0.5 PF, against the exit model's 1.0+.

### Order of operations

1. Replay the stop state machine in the backtest. Everything else is unmeasurable until
   this is done.
2. Re-measure on real `pandas_ta` (Python 3.12+).
3. *Then* sweep `stop_loss_swing_pct` / `r_multiple` / threshold against a faithful model.

Tuning parameters now would compensate for a modelling gap rather than a market fact, and
those values would be wrong the moment they went live — where the real stop machine is
already running.

---

## Third pass: stop machine replayed, parameters swept (2026-07-26)

`simulate_forward_exit` now replays `engine/stop_state_machine.py`'s `calculate()`
bar-by-bar — the same function `engine/position_management.py` calls live — with
production's `should_advance()` ratchet. 9 new tests in
`tests/test_backtest_exit_replay.py`; suite at **384 passed**.

The one that matters is `test_stop_is_priced_off_the_previous_bar_not_the_current_one`.
The stop in force during bar *i* is computed from bar *i−1*'s close. Re-pricing from bar
*i*'s own close and then testing bar *i*'s low against it would be look-ahead that
flatters trailing stops **specifically** — i.e. it would manufacture exactly the
improvement this change is supposed to measure.

### Effect of the exit fix alone (same 6 tickers, nothing else changed)

| | Fixed stop | Stop machine |
|---|---|---|
| Win rate | 29.8% | **53.5%** |
| Profit factor | 1.10 | 1.23 |
| Trades | 302 | 396 |
| Exits | 208 SL / 71 TP | 181 SL / **155 trailing** / 55 TP / 5 time |

The earlier 2.71 counterfactual was an upper bound and did not survive contact, exactly as
flagged: the ratchet rescues losers *and* cuts winners (take-profits fell 71 → 55). Real
answer 1.23, not 2.71.

### P&L attribution — where the money actually is

| Exit | n | avg | **total** | median MFE |
|---|---|---|---|---|
| stop_loss | 181 | −5.92% | **−1071.5pp** | +1.54% |
| take_profit | 55 | +18.40% | **+1012.2pp** | +25.02% |
| trailing_stop | 155 | +1.93% | +299.4pp | +8.18% |
| time_stop | 5 | +1.75% | +8.8pp | +4.68% |

55 trades produce essentially all the profit. 181 produce all the loss, and their median
MFE of +1.54% says they never worked at any point — these are bad entries, not mismanaged
exits.

### Sweep results (full re-runs; entries are path-dependent so nothing is re-filtered)

Read these on **expectancy_r**, not avg%. The replay equal-weights percentage returns, so
a wider stop is flattered mechanically unless risk is held at 1R.

| Config | n | win | PF | **exp_R** |
|---|---|---|---|---|
| stop=8 (current) | 396 | 53.5% | 1.23 | 0.119 |
| stop=5 | 491 | 50.7% | 1.26 | — |
| stop=12 | 344 | 57.6% | 1.40 | — |
| **stop=16** | 330 | 58.8% | 1.48 | **0.202** |
| stop=25 (cap off) | 325 | 59.1% | 1.44 | — |
| stop=16, trail=0.75 | 370 | 56.8% | 1.28 | 0.157 |
| stop=16, trail=2.5 | 317 | 59.0% | 1.50 | 0.201 |
| stop=16, r=4.0 | 324 | 58.3% | 1.44 | 0.202 |
| stop=16, threshold=62 | 257 | 59.5% | 1.44 | 0.178 |

**Only one lever works.** The 8% cap was binding on 94 of 181 stop-outs — clamping the
stop tighter than the ATR justified and stopping trades out before they could work.

- **Trail is already right.** Tightening to 0.75 costs 0.045R. The 6.28pp median giveback
  on trailing exits is the price of letting the 55 big winners run, not a leak to plug.
- **r_multiple is inert.** 3.0 and 4.0 are identical to three decimals.
- **Raising the threshold HURTS** (0.202 → 0.178) — the exact opposite of what the broken
  exit model implied (where ≥65 looked like PF 1.60). Anyone who had acted on that reading
  would have made the system worse. This is why the re-measure-first call was right.

### The holdout, and the real bottom line

16% was fitted on six volatile names including two bitcoin miners. Re-run on liquid
mega-caps (MSFT, GOOGL, BAC, PFE, CMCSA, F):

| Holdout config | n | win | PF | exp_R | median risk |
|---|---|---|---|---|---|
| stop=8 | 389 | 52.4% | 1.01 | 0.0045 | 2.66% |
| stop=16 | 389 | 52.4% | 1.02 | 0.0043 | 2.66% |

**Byte-identical.** Median risk 2.66% means the 8% cap never binds on low-ATR names, so
the change is a no-op there. That is a good property — the fix is targeted, not a global
loosening — but it also means:

> **The strategy has no edge on liquid mega-caps: PF 1.01, expectancy 0.004R.** The entire
> measured edge lives in high-volatility names over 2023–2026, a window containing a large
> crypto/momentum run. Until that is validated across regimes and a wider universe, the
> honest position is that this is an edge in one volatility bucket during one favourable
> period — not a validated strategy.

### Recommended, in order

1. **Do not change `config.yaml` yet.** Every number above is from `ta_fallback`, not the
   `pandas_ta` backend every threshold was derived on. Re-run on Python 3.12+ first.
2. **Then raise `stop_loss_swing_pct` 8 → ~14-16** for TURBO. Low-ATR names are unaffected;
   high-ATR names stop being clamped. Confirm position sizing scales inversely with stop
   width before doing this, or dollar risk per trade rises with it.
3. **Leave trail, `r_multiple` and the threshold alone.** Swept, all at or past optimum.
4. **Attack the 181 stop-outs, not the exits.** Median MFE +1.54% — the entry admits trades
   that immediately go against. That is a scoring problem, and it is where the remaining
   upside is.
5. **Walk-forward across regimes** before any capital decision. `learning/walk_forward.py`
   already exists and is unused here.

### P1 — Now the real work: does the strategy have an edge?

4. **Re-run on Python 3.12+ with real `pandas_ta`.** Every threshold in `config.yaml` was
   derived on that backend. Nothing below should be tuned against fallback numbers.

5. **Investigate the 3:1 stop:target ratio.** 208 stops vs 71 targets against a 3.0
   R-multiple is the single loudest signal in the new output. Either the ATR-tiered
   initial stop is too tight for a 6.9-day hold, or the 3.0R target is unreachable in that
   window. Sweep both; they are `config.yaml` values, not code.

6. **Then, and only then, judge the entry signal.** A 30% win rate is fine at 3.0R and
   fatal at 1.0R — the entry cannot be evaluated until the exit model is settled.

7. **Replay the trailing-stop state machine.** `simulate_forward_exit` uses a fixed stop +
   fixed target, not `engine/stop_state_machine.py`'s 6-state trailing logic. With 302
   trades now flowing, this Stage-1.5 gap is the largest remaining fidelity difference
   between backtest and live.

### P2 — Known gaps, correct to leave but worth tracking

7. `day_of_week` in `swing_buy_rules.py` (~L1097) uses `datetime.datetime.now()`, not the
   simulated date. Currently harmless (`calendar_enabled: false`) but wrong the moment
   calendar adjustments are switched on in a replay.
8. Synthetic bid/ask makes SPREAD_WIDE and the execution-quality spread term
   non-discriminating — already documented in the module docstring, not a defect.
9. Vetoes look healthy: LOW_VOLUME 2,710 / PRICE_RANGE 5,709 of ~38k. No action.

---

## The thing not to do

Do not lower the base threshold to make trades appear. It would produce trades from a
scale that is still internally inconsistent, and the resulting win rate would be
uninterpretable — a curve-fit to a plumbing bug rather than a measurement of edge.
