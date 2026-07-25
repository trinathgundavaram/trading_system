# Evaluating a Third "Short-Term Hold" Tier (beyond Day / Swing)

Read directly from the live code and config as of 2026-07-24: `config.yaml`, `rules/swing_buy_rules.py`, `rules/hard_vetoes.py`, `rules/sell_rules.py`, `rules/dynamic_thresholds.py`, `engine/position_management.py`, `engine/position_sizing.py`, `engine/rotation.py`, `engine/stop_state_machine.py`, `scheduler.py`, `pre_selection_criteria_and_trading_modes.md`.

## Bottom line up front

The system already has three internal modes — DAY, SWING, HYBRID — but HYBRID is not a third *behavior*, it's a router that classifies every trade into DAY or SWING after entry. There is no existing "hold longer than a swing trade for a bigger move" tier.

A useful version of that tier is buildable, and the codebase's own HYBRID-classification pattern (`_classify_hybrid_leg()` in `scheduler.py`) is the right template — reuse the SWING entry engine, then apply a *different post-entry treatment* (stop, target, hold time, rotation exemption, earnings handling) to trades that clear a higher conviction bar. That is a small, contained change.

A full parallel scoring engine (its own bucket weights, its own threshold table, like DAY has) is also buildable but is a much larger change, and the project's own recent history is a direct warning about that path: the DAY/HYBRID rebuild three weeks ago introduced a real bug (EV lookups silently querying a mode key, `"HYBRID"`, that never gets written) that went undetected until a dedicated audit found it. Every additional mode-keyed code path (EV lookup, pattern-database write, position sizing, stop ceiling, rotation eligibility, backtest cap) is another place that exact bug class can recur.

My recommendation: **don't build this yet.** The system doesn't have enough live, mode-segmented trade history to justify a new mode's parameters (stop %, target R-multiple, hold-days ceiling) — the project's own `pre_selection_criteria_and_trading_modes.md` explicitly deferred equivalent calibration work ("items #2/#3") for exactly this reason. Below is the full breakdown of what exists, what a third tier would need, and a phased path if you want to move forward anyway.

---

## 1. What the model actually does today

### Three config-level trading modes (`trading.mode`)

| | DAY | SWING | HYBRID (current live setting) |
|---|---|---|---|
| Scan cadence | 5 min | 15 min | 5 min |
| Entry scoring | Own bucket weights (`weights.swing_buy_day`) — MOMENTUM/VOLUME_PA/BREADTH up, TREND/EXTERNAL down | Standard weights (`weights.swing_buy`) | Same as SWING (full evidence-based scoring, not a stripped intraday read) |
| Hard-veto volume floor | 2,000,000 avg vol | 1,000,000 | 1,000,000 |
| Spread veto ceiling | 0.50% | 1.00% | 1.00% |
| Base buy threshold | `buy_score_threshold_day_pct` (TURBO 55%) | `buy_score_threshold_pct` (TURBO 50%) | Same as SWING |
| Stop ceiling | `stop_loss_day_pct` (TURBO 4%) | `stop_loss_swing_pct` (TURBO 8%) | Resolved per-leg after classification |
| Time-of-day vetoes | Dead zone 11:30–1:30, no new entries after 3:30pm, forced flatten at 15:55 | None | None |
| Position sizing | `day_size_multiplier` (0.5×) applied | 1.0× | Resolved per-leg |

**HYBRID is not a third behavior** — it's a router. Every HYBRID buy scores through the SWING engine, then `_classify_hybrid_leg()` tags the filled trade DAY (needs to clear threshold+3% AND show real intraday character: volume ≥1.5× avg or same-day move ≥2%) or SWING (everything else). Only after that tag is assigned does it inherit DAY's tighter stop/target/sizing/forced-close, or SWING's looser ones.

### How long a "swing" trade can actually run today

This is the detail most relevant to your question. `engine/position_management.py`'s `_check_time_stop()`:

```
if days_held >= 10 and abs(profit_r) < 0.3: flag "no progress, re-evaluate"  (not a forced exit)
max_days by risk level: CONSERVATIVE 10, MODERATE 14, AGGRESSIVE 20, TURBO 30
if days_held >= max_days and profit_r < 1.0: forced exit
```

The forced max-hold exit **only fires if the position is below +1R.** A swing trade that's working (up more than 1R) is never time-stopped — it rides the ATR trailing-stop machine (`engine/stop_state_machine.py`) indefinitely: breakeven → profit-protect → trend-trailing. So "let a good swing trade run longer for a bigger return" is already partially the system's behavior today, *if the trade is profitable and nothing else forces it out first.*

What actually caps a genuinely strong winner's runway isn't the time-stop — it's two other things:

1. **`sell_rules.py`'s `earnings_approaching` hard exit** — fires unconditionally whenever `0 <= days_to_earnings <= 2`, regardless of mode, regardless of how strong the trend is. A multi-week swing thesis gets force-closed in front of every single quarterly print. There's no "this is a high-conviction long-term thesis, tolerate the earnings event" path today.
2. **`rotation.py`** — a position becomes rotation-eligible (can be sold to free capital for a new candidate) once `days_held >= min_hold_days` (default 3) and its health score drifts to ≤55. A slow-burning, still-fundamentally-sound long-term thesis that just isn't showing hot short-term technicals can get rotated out for a shinier new candidate after only 3 days.

So the real gap isn't "the system can't hold something longer" — it's that nothing currently *protects* a long-hold thesis from earnings-driven exits or rotation churn, and nothing on the **entry side** selects for "this looks like a good multi-month thesis" vs. "this looks like a good multi-day swing."

---

## 2. What "short-term hold, additional tier" could mean — and which one you actually want

Two different things could be meant by this, and they lead to very different designs:

**Interpretation A — a tier *between* day and swing** (e.g., hold 1–3 days, more patient than a day trade, less committed than a swing). This mostly already exists: it's what a HYBRID-classified-SWING trade with a short actual hold is. There's little marginal value in formalizing this as its own mode — SWING already covers 1 day to 30 days depending on risk level and how the trade performs.

**Interpretation B — a tier *beyond* swing** (e.g., hold 4–12+ weeks, aiming for a bigger % move, more tolerant of chop, fewer round trips, leaning on the fundamental/EXTERNAL evidence more than short-term momentum noise). This is a genuinely different thing the system doesn't have today. Call it **POSITION** mode. Everything below assumes this is the one you mean, since it's the one that adds real capability rather than relabeling something that already exists.

If you actually meant A, most of what you want is a config change (widen SWING's own time-stop and stop-loss %), not a new mode — worth confirming before building anything.

---

## 3. Recommended design if you build POSITION mode: reuse HYBRID's pattern, don't build a parallel scorer

### Why not a full parallel entry engine (like DAY has)

DAY mode required its own bucket-weight table (`weights.swing_buy_day`), its own base threshold, its own veto floors, its own scan cadence — because intraday setups are *evidentially different* (you don't have days of trend to lean on, so weight shifts toward MOMENTUM/VOLUME/BREADTH). A "hold longer for a bigger swing" thesis is not evidentially different in the same way — it's the *same* multi-day evidence (TREND, EXTERNAL, SENTIMENT_MACRO) the SWING engine already scores, just held under a different risk/target contract. Building a fourth weight table and threshold set for this would be solving a problem that doesn't exist and would add exactly the kind of mode-keyed surface area that caused the EV bug.

### The lower-risk design: post-entry classification, same as HYBRID's DAY/SWING split

1. Score and enter through the existing SWING engine — no change to `rules/swing_buy_rules.py`'s bucket weights.
2. After a SWING (or HYBRID-resolved-SWING) buy clears, add a `_classify_position_leg()` step (mirroring `_classify_hybrid_leg()`) that promotes the trade to `trade_mode = "POSITION"` only when it clears a **materially higher conviction bar** on the evidence that matters for a longer hold, e.g. (illustrative, needs real calibration, not hardcoded today):
   - Buy score well above the dynamic threshold (e.g. threshold + 10–15%, not +3% like DAY's bar)
   - TREND bucket near its qualification ceiling (long-term trend, not just a short pop)
   - EXTERNAL bucket (analyst/fundamental evidence) above a floor — this is the bucket DAY mode *deweights*, so require the opposite here
   - ATR% on the low side of the volatility bands already in `position_sizing.volatility_atr_pct_bands` (a "let it run" thesis works better on a stock that isn't already violently volatile)
   - Not already inside the earnings-proximity window that would otherwise veto entry (still respect the existing EARNINGS_RISK veto at entry — the difference is what happens if earnings arrives *while already holding*, not whether you enter near one)
3. Everything downstream reads `trade_mode` exactly the way it already does for DAY vs SWING:
   - **Stop ceiling** (`stop_state_machine.py`): new `stop_loss_position_pct` per risk profile, wider than swing (e.g., roughly 1.5–2× the swing figure — CONSERVATIVE ~8%, TURBO ~14–15%, needs real backtesting, not a guess baked into this doc).
   - **Take-profit target** (`sell_rules.py`): higher `r_multiple` for POSITION (e.g. 5–8R vs SWING's 3R) — same ATR×1.5 risk-per-share convention, just a farther target.
   - **Max hold days** (`_check_time_stop`): its own ceiling per risk profile, clearly higher than SWING's (e.g. 60–90 days), same "only forces exit if profit_r < 1.0" logic — so a POSITION winner is never time-stopped for being patient, only for being wrong.
   - **Position sizing** (`position_sizing.py`'s `calculate(..., mode=...)`): a `position_size_multiplier`, analogous to `day_size_multiplier` — likely *below* 1.0×, not above. A longer hold accumulates more calendar days of overnight-gap and macro-event risk per dollar committed than a swing trade does, even though the per-trade stop % is wider; sizing should reflect that, not assume "longer hold = same or bigger size."
   - **Rotation exemption** (`rotation.py`): POSITION legs need either a much higher `min_hold_days` before becoming rotation-eligible, or exclusion from the victim pool entirely. Today's global `min_hold_days: 3` guardrail is built for swing/day churn management and would defeat the entire point of a patience tier if applied unchanged.
   - **Earnings handling** (`sell_rules.py`'s `earnings_approaching` rule): needs to become mode-aware. Right now it's an unconditional force-exit for *every* mode. For POSITION, the honest options are: (a) don't force-exit, just tighten the stop heading into the print, or (b) force a partial reduction rather than a full exit. Either is a real behavior change to a file the docstring explicitly calls "hard exits, single trigger wins, deliberately" — worth deciding deliberately, not smuggling in as a side effect.
   - **Backtest ceiling** (`config.yaml`'s top-level `backtest.max_hold_days: 20`) — this is a *global* cap today, not mode-aware. If you don't raise this for POSITION specifically, any backtest of the new tier will force-close every simulated position trade at 20 days regardless of what the live max-hold config says, making the backtest results meaningless for validating the tier.
   - **EV / pattern-database mode key** (`learning/pattern_database.py`, `rules/swing_buy_rules.py`'s EV lookup, `scheduler.py`'s `record_entry`, `_has_open_pattern`/`_close_due_patterns`/`_latest_open_pattern_id`, `confirm_fill.py`'s `_most_recent_open_pattern`, `maybe_run_learning_loop`) — this is the exact list of places the DAY/HYBRID bug touched. A third mode key means auditing every one of these again for the same "queries a string that never gets written" failure mode. This is the single highest-risk part of adding a third tier, precisely because it already happened once with only two modes.
   - **`signals.trade_mode` column** — already a nullable TEXT column, no schema change needed; POSITION is just a third valid string.

### What does NOT need to change

- The 6-bucket scoring math itself (rule logic, correlated-evidence caps, continuous qualification multiplier) — identical across modes today, and there's no reason for POSITION to change that.
- `rules/hard_vetoes.py`'s entry-side vetoes — POSITION candidates should clear the same bar SWING candidates do at entry; the divergence is entirely post-entry.
- Watchlist/screener discovery — unchanged.

---

## 4. Is now the right time? Readiness assessment

Three separate signals point to "not yet, but here's what needs to happen first":

**1. No live, mode-segmented data exists to calibrate against.** `signals.trade_mode` and `pattern_database`'s mode key were only *just* fixed to record correctly — before today's Section 4 fix, DAY/HYBRID accounts had been silently recording zero usable EV history the entire time they'd been live. The project's own doc is explicit that bucket-weight/threshold tuning "needs real outcome data... not something to hardcode a guess for today" — and that's for tuning an *existing* mode with a wrong-but-fixed pipeline. Setting a brand-new tier's stop %, target R-multiple, and hold-days ceiling with zero data behind them is the same mistake at a higher stakes level (wider stops, bigger size-per-trade risk, longer capital lockup).

**2. The DB-backed test suite can't currently run in this environment** (`psycopg2.OperationalError: connection refused` — no local Postgres, confirmed in the same doc for `test_paper_trading.py`, `test_live_trader.py`, `test_rotation.py`). Any change touching position sizing, rotation, or live/paper trade execution — which a new mode necessarily does — can only be verified with hand-built fake-DB unit tests until Postgres is reachable somewhere. That's a real gap between "looks correct" and "verified correct" for exactly the kind of change (capital-affecting, multi-file, mode-keyed) a third tier is.

**3. The exact bug class this would risk recreating was found and fixed only recently.** Adding a third mode key multiplies the surface area of "a lookup queries a string that's never actually written" by another dimension, in a codebase that just spent a dedicated pass finding and fixing that specific failure mode for two modes. The honest prior here is that a third mode, rushed, reproduces a version of the same bug somewhere in the EV/pattern-database chain.

**Config also shows a live discrepancy worth flagging on its own:** `config.yaml`'s `trading.mode` is currently `HYBRID` and `risk_level` is `TURBO`, but `pre_selection_criteria_and_trading_modes.md`'s Section 3/4 narrative describes the mode as having been `DAY` "as of this revision." The doc is stale relative to the live config — not a blocker for this decision, but worth knowing the doc's mode-specific claims should be re-verified against current `config.yaml`, not assumed current.

---

## 5. Recommended path if you want to proceed anyway

1. **Fix the earnings/rotation gap first, independent of any new mode.** Make `earnings_approaching` mode-aware (or at minimum, add a "high-conviction override" flag on any position) and give rotation a higher `min_hold_days` tier for positions you've explicitly flagged as long-term. This alone captures a meaningful share of "let a good thesis run longer" without a new mode, a new stop table, or new EV-keying risk.
2. **Let signals.trade_mode data accumulate for real DAY/SWING history** — the project's own EV engine treats n=20 as only "low" confidence and champion/challenger review needs 30 closed trades to reach significance; a new POSITION tier's parameters deserve at least that much real evidence, not launch-day guesses.
3. **Get Postgres reachable** so `test_paper_trading.py`/`test_live_trader.py`/`test_rotation.py` can actually run against any change that touches sizing, rotation, or execution — currently the only thing standing between "verified" and "verified via hand-built fakes."
4. **If/when you build it, use the post-entry classification design (Section 3), not a parallel scoring engine** — smaller surface area, reuses vetted scoring math, and follows a pattern (`_classify_hybrid_leg`) this codebase has already built and tested once.
5. **Audit the full EV/pattern-database mode-key chain as its own explicit checklist item**, not an afterthought — this is the one part of the change most likely to silently fail exactly like it did before.

---

## 6. Open questions worth deciding before any code changes

- Which interpretation did you actually mean — a tier between day/swing (mostly exists already via SWING's own hold-days flexibility), or a tier beyond swing aiming for a bigger, slower move (Section 3's POSITION design)?
- Should a POSITION-classified trade survive its own earnings print, or just tighten its stop around it? These have very different risk/return profiles and this doc doesn't have live data to recommend one over the other.
- Do you want POSITION-eligible capital to come out of the existing `max_positions` (25) pool, or a dedicated cap like `max_day_positions` (5) — i.e., should a run of great short-term setups be able to starve the long-hold tier of capital, or vice versa?
- Is "not implemented yet" for `momentum_screen`/`insider_buying` screener sources (per the pre-selection doc) something you want addressed before or independent of this tier — they'd plausibly matter more for a fundamentals-leaning POSITION tier than they do for DAY/SWING.
