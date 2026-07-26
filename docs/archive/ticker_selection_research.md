# Ticker Pre-Selection: Why Nothing Is Scoring Above ~48%

Research findings only — nothing below has been implemented yet.

## Bottom line

Across every scored ticker in the last 3 days of logs (249 scored ticker-cycles), the highest score reached was **48%**, the mean was **17.7%**, and **zero** tickers crossed 50% — the TURBO threshold, the lowest of the four risk profiles. This isn't one bad rule or one unlucky day; it's four compounding, mostly-fixable problems stacked on top of a genuinely mixed market:

1. **finviz's MCP server is not actually installed/runnable** — every call fails with a missing-file error, permanently zeroing ~21% of the EXTERNAL bucket for every ticker, every cycle.
2. **The screener's own design creates a data-starvation trap**: candidate tickers are scored "lite" (no maverick/finviz) by default, and only get real data if they already score close to the buy bar — but they can't score close to the bar without that same data. Only 2 tickers escaped this trap today, both scoring 50-51%, still short.
3. **The live "buy bar" is being pushed higher (57%, not the configured 50%) by the same weak-breadth conditions that are also capping every ticker's own score** — a double penalty from one root cause.
4. **Relative-volume data looks broken**, not just weak: literally every sampled ticker — including NFLX, ORCL, and other highly liquid names — shows 0.0-0.1x of average volume in mid-afternoon trading, which isn't realistic. This silently drags the VOLUME_PA bucket toward zero everywhere.

None of these are "the scoring bar is too strict" in the way it might look at first glance. Three of the four are data/plumbing problems, not judgment calls about what a good setup looks like.

---

## 1. What the data actually shows

**Score distribution, last 3 days (249 scored ticker-cycles, from `scheduler.log` + rotated logs):**

| Stat | Value |
|---|---|
| Max score | 48% |
| Mean | 17.7% |
| Median | 17% |
| Tickers ≥ 45% | 13 of 249 (5%) |
| Tickers ≥ 50% (TURBO bar) | **0** |
| Tickers ≥ 55% (AGGRESSIVE bar) | **0** |
| Tickers ≥ 60% (MODERATE bar) | **0** |
| Tickers ≥ 68% (CONSERVATIVE bar) | **0** |

Config is currently set to `risk_level: TURBO` (the most permissive profile, 50% bar) — and even that hasn't been cleared once. Your read that MODERATE/CONSERVATIVE would have produced zero trades is correct, but so would TURBO.

**A representative real example (PCG, today, 45.9%):**

```
Score: 45.9%  |  Threshold: 57%  →  FAIL  (11.2 pts short)
Threshold: Base 50% + stress 2.1% + breadth[weak] +5.0% (capped 7.1%) = 57%

Bucket breakdown:
  MOMENTUM               15.3 / 20.5   (75%)
  SENTIMENT_MACRO         9.4 / 15.0   (63%)
  TREND                  11.2 / 22.5   (50%)
  VOLUME_PA               4.5 / 15.0   (30%)
  EXTERNAL                2.8 / 16.0   (18%)  <- structurally capped, see below
  MARKET_BREADTH          0.7 / 11.0   ( 6%)  <- structurally capped, see below
```

This pattern — EXTERNAL and MARKET_BREADTH both near-zero while TREND/MOMENTUM are respectable — repeats across every ticker checked, watchlist or screener-sourced.

---

## 2. Root causes, ranked by how confident I am and how fixable they are

### 2.1 finviz is completely broken (high confidence, easy fix)

Every finviz call fails with:
```
MCP error get_stock_overview: [Errno 2] No such file or directory:
'/Users/trinathrao/finviz-mcp-server/venv/bin/finviz-mcp-server'
```
The binary doesn't exist at the path the app expects. This isn't rate-limiting or a scraping block — it's a missing install. It fails 3 times, trips the circuit breaker, sits dark for 15 minutes, and repeats — so finviz has effectively never worked. That's `finviz_technical_rating` (10 of EXTERNAL's 48 points, ~21%) permanently unreachable for every single ticker.

This looks like the single highest-confidence, lowest-effort fix available: either reinstall/repoint finviz-mcp-server, or formally retire it and redistribute its weight (same "unavailable bucket" redistribution logic the codebase already has for EXTERNAL going fully dark — just needs to extend to a permanently-dead individual source).

### 2.2 FMP's grades/earnings endpoints are also dead (confirmed, plan issue)

```
fmp: circuit breaker forced OPEN for 24.0h (HTTP 402 - not entitled under current FMP plan)
fmp-ratings: circuit breaker forced OPEN for 24.0h (HTTP 402 - not entitled under current FMP plan)
```
This is the same finding from our scheduler investigation — still active today. It zeroes `estimate_raised` (6 pts) and pushes `no_recent_downgrade` (2 pts) toward its default. Combined with finviz, up to **30 of EXTERNAL's 48 points (62%)** are structurally unreachable for most tickers most of the time, leaving only `analyst_Buy` (yfinance fallback) and `sector_rs_1m_positive_proxy` (computed internally) as the rules that reliably fire — which is exactly the 2.8/16 pattern seen in every packet checked.

Fix requires either upgrading the FMP plan to cover these endpoints, or accepting they're dead and folding them into the "unavailable" redistribution instead of scoring them as silent zeros.

### 2.3 The "lite scoring" catch-22 (high confidence, structural/design issue)

This is the one I'd flag as most important, because it's not a broken dependency — it's how the pipeline is designed to work:

- Hand-picked watchlist tickers always get **full** analysis (maverick + finviz + scanner + news) every cycle.
- Screener-discovered candidates (the majority of what's scanned — 30 candidates vs. 6 watchlist tickers per cycle) default to **"lite"**: maverick and finviz are skipped entirely to save time/cost.
- A lite candidate only gets promoted to a full rescore if its lite score already lands within **8 points** of the live buy threshold (`PROMOTE_MARGIN = 8.0`, `scheduler.py`).

In the 40 most recent packets sampled, **38 were "lite" and only 2 were "complete"** — and those 2 were watchlist tickers, not screener discoveries. Over the last 3 days, only **11 promotions total** happened (roughly 3-4/day), and today only 2 (BP at 51.3%, PBR at 50.2%) — both still short of the effective bar.

The trap: maverick + finviz are worth up to 22 of EXTERNAL's 48 points. A lite candidate is missing that data, so its score is structurally lower — which keeps it outside the 8-point promotion window — which means it never gets the data that might have gotten it promoted. It's a closed loop that, combined with 2.1 and 2.2 above (meaning even a "full" rescore often can't get real maverick/finviz data anyway), leaves almost every screener candidate permanently under-evidenced.

### 2.4 Weak breadth penalizes twice (medium-high confidence, design tradeoff)

The dynamic threshold system (`rules/dynamic_thresholds.py`) adds a `breadth[weak] +5.0%` term (capped 7.1%) plus a `stress +2.1%` term when market conditions look shaky — pushing today's *effective* bar from the configured 50% to **57%**. At the same time, MARKET_BREADTH is a bucket in the score itself, and it's scoring **0.7/11 (6%)** for essentially every ticker right now (ad_ratio 0.46 — just under the 0.50 "good" line, negative McClellan, `spy_ad_aligned` false).

So the same underlying condition (mixed/weak breadth) both lowers every ticker's ceiling *and* raises the bar they need to clear. That may be intentional risk management, but it's worth a deliberate decision rather than an emergent side effect — right now it means a genuinely choppy week can mathematically guarantee zero signals regardless of how good individual stock-level setups are.

Worth noting for balance: current regime is CHOPPY (not BEAR/CRISIS), VIX is a calm 16.9, and F&G is a neutral-ish 43. This isn't a market meltdown — it's an unremarkable, slightly indecisive tape that the current setup treats fairly harshly.

### 2.5 Relative volume (rvol) looks like a broken calculation, not real data (high confidence something's wrong, cause not yet pinpointed)

Every ticker sampled — watchlist and screener, thinly-traded and mega-caps alike — shows **0.0x-0.1x of average volume** in the mid-afternoon:

```
VRT   140,883    (0.0x avg)
ORCL  996,854    (0.0x avg)
NFLX  2,543,587  (0.1x avg)
```

It is not plausible that NFLX and ORCL are trading at 0-10% of their normal volume simultaneously, several hours into the session, on an otherwise unremarkable day. This reads as a data-pipeline bug: `td.volume` (day-cumulative volume from a live quote) and `td.avg_volume` (yfinance's `averageVolume`) are computed in different code paths in `engine/ticker_analyzer.py` (roughly lines 401-404 and 552/561/619-620) and may be picking up mismatched units, a stale/partial volume field, or overwriting each other in the wrong order. I haven't pinned the exact defect down — that's implementation-phase work — but the evidence that *something* is wrong is very strong.

This quietly caps the VOLUME_PA bucket's `rvol` sub-score (up to 10 pts) near zero for every ticker, on top of everything above.

### 2.6 Screener discovery quality (lower confidence, not deeply investigated)

I didn't do a deep pass on whether the screener's discovery sources (rs_gainers, volume_surge, gap_candidates, sector_leaders, universe_sweep) are actually surfacing the market's best candidates or just whatever's moving today. Given 2.1-2.5 already fully explain the low scores, I'd treat this as a secondary question worth revisiting only after the data problems above are fixed — a low score caused by missing data looks identical to a low score caused by a genuinely mediocre candidate, and there's no way to tell them apart until the data is actually flowing.

---

## 3. Improvement options (not yet implemented — for your review)

Roughly ordered by effort vs. expected impact:

**Quick, low-risk, high-confidence:**
- Fix or retire finviz (reinstall the MCP server at the expected path, or repoint it, or formally mark it permanently unavailable so its 10 points get redistributed instead of silently zeroed).
- Investigate and fix the volume_ratio/rvol calculation — likely a short, targeted fix in `ticker_analyzer.py` once the exact defect is confirmed (I'd suggest logging both raw values with their source for one cycle to nail it down before touching the fix).
- Decide FMP's fate: upgrade the plan to cover grades/earnings, or formally retire those two rules from EXTERNAL (fold them into the existing "unavailable bucket" redistribution math instead of scoring them as zero).

**Medium effort, structural:**
- Rework the lite/promotion catch-22. Options to weigh against each other: raise `PROMOTE_MARGIN` from 8 points; base the promotion check on the *nominal* threshold instead of the inflated dynamic one; promote a small fixed quota of top-scoring lite candidates every cycle regardless of margin; or extend the existing "fully unavailable bucket" redistribution logic to partial unavailability (missing 2 of 3 external sources should get some relief too, not zero).
- Revisit whether MARKET_BREADTH should both shrink the achievable score AND raise the threshold for the same condition — pick one lever, not both, or make the interaction deliberate and tunable.

**Worth a look once the above are fixed:**
- Re-examine screener discovery quality with real, complete data flowing — right now any conclusion about candidate quality is confounded by the data gaps above.

I'd suggest tackling the quick wins first (finviz, FMP decision, volume_ratio bug) since they're the most clear-cut and lowest-risk, then re-measure the score distribution before deciding whether the lite/promotion and threshold-stacking issues still need structural changes or resolve themselves once real data is flowing.

---

## Appendix: what I looked at

- `scheduler.log` + rotated logs (`.1`, `.2`, `.3`) spanning 2026-07-20 through today, for score lines, breaker events, and regime/breadth state.
- `output/pending_prompts/*.md` — full per-ticker bucket breakdowns (packet_builder.py output) for both screener and watchlist tickers.
- `rules/swing_buy_rules.py` — full 7-bucket scoring engine, bucket weights/maxes, dynamic threshold call, EXTERNAL-unavailability redistribution logic.
- `engine/screener.py` — discovery, quality gate, and the outcome-learning/exclusion mechanism from the earlier conversation.
- `engine/ticker_analyzer.py` — where `td.volume`/`td.avg_volume`/`volume_ratio` and the maverick/finviz "lite" fetch gating are computed.
- `config.yaml` — risk-level thresholds (CONSERVATIVE 68% / MODERATE 60% / AGGRESSIVE 55% / TURBO 50%), screener learning knobs.
- `mcp_clients/maverick.py` — availability/circuit-breaker mechanics.
