"""Human-readable description of the CURRENT buy/sell rule set, for the UI's
Strategy tab (server.py's /api/strategy). This is a maintained catalog, not a
runtime introspection of rules/swing_buy_rules.py / rules/hard_vetoes.py - it
must be kept in sync by hand if those files change (same trust model this
codebase already uses elsewhere, e.g. scheduler.py's NYSE_HOLIDAYS_2026 comment
"VERIFY/UPDATE EACH YEAR", or bayesian_updater.py's BUCKET_WEIGHT_BOUNDS, which
also separately hardcodes the 7 bucket names/weights rather than importing
them). Point values below were transcribed directly from
rules/swing_buy_rules.py and rules/hard_vetoes.py on the date this file was
written - if you change a point value or add a rule there, update it here too.

Every rule is tagged REAL, PROXY, or PLACEHOLDER, matching the same honesty
convention engine/ticker_data_adapter.py and engine/market_breadth.py use:
REAL = backed by a live data source today. PROXY = a real, calculated
approximation of the true signal (e.g. sector-ETF breadth standing in for
true NYSE-wide breadth). PLACEHOLDER = no data source wired yet, so this rule
essentially never fires - not a bug, just an honest gap.

Sell rules ARE catalogued here now (SELL_RULES_CATALOG below) - they used to
be skipped ("config-driven, no catalog needed") back when the soft exit
signals lived in config.yaml. That soft layer was replaced by a real
weighted 6-bucket Exit Score engine (rules/exit_scorer.py, hardcoded bucket
weights just like the buy side), so it deserves the same documentation the
buy-side buckets get. The remaining hard exits (stop_loss/take_profit/
trailing_stop/earnings_approaching/vix_spike) are still config-driven -
the UI reads those live from /api/config.

2026-07-23: full-framework audit (Trinath asked for the Strategy page to
cover "every rule ever used in the whole framework," including pre-selection)
- nine modules that were fully live in production had never been transcribed
here: the market-wide gate (engine/market_context.py + rules/market_filters.py),
the DAY/SWING/HYBRID mode-separation logic, account-level risk guardrails
(rules/risk_rules.py), portfolio-level exposure caps (engine/portfolio_risk.py),
the Execution Quality score (rules/execution_quality.py), the position-sizing
pipeline (engine/position_sizing.py), the probabilistic EV/P(win) decision
layer that replaces the score-vs-threshold cliff (rules/probabilistic_decision.py
+ engine/ev_engine.py), the regime classifier (engine/regime_engine.py), and
the portfolio rotation engine (engine/rotation.py). SCREENER_FILTERS_CATALOG's
stage 3 was also enriched with the actual Discovery Score formula/persistence
bonus/sector cap/exploration slots/lite-pass logic, and HARD_VETOES_CATALOG's
EARNINGS_RISK entry now shows its real point breakdown - both existed in code
and in pre_selection_criteria_and_trading_modes.md but were only summarized
one level up in this catalog before. Same "maintained by hand, not runtime
introspection" trust model as everything else in this file - if any of these
nine modules' logic changes, this file needs a matching edit.
"""

BUY_RULES_CATALOG = {
    "engine": "rules/swing_buy_rules.py - 6 weighted decision buckets + 1 additive volatility "
              "bonus. Each bucket's contribution is (points/max_points) * weight * qual_mult, "
              "where qual_mult is a CONTINUOUS anchor-table curve on the bucket's %-of-max "
              "(0%->0.0, 30%->0.35, 50%->0.60, 100%->1.00, linear between anchors) - there is NO "
              "hard qualification cliff anywhere; min_qualify_pct feeds only the displayed "
              "qualified/unqualified flag. Correlated-evidence subgroup caps (2026-07-15, external "
              "review): the trend-structure family (SMA stack/EMA/weekly) caps at 38, the MACD "
              "family at 18, and the volume/accumulation family at 20, so one latent condition "
              "(e.g. a broad beta rally) can't be counted 3-5 times. BUCKET AVAILABILITY "
              "(2026-07-15b, UNKNOWN != FALSE): when every EXTERNAL source (finviz/analyst/"
              "maverick) is down, that's a data outage, not negative evidence - 75% of the "
              "bucket's weight is redistributed pro-rata to the available buckets and 25% is "
              "left dead (missing evidence still costs something), all recorded in "
              "data_coverage.unavailable_buckets. max_points values below are EFFECTIVE CAPPED "
              "maxima (after subgroup caps and mutually exclusive branches), not raw rule sums - "
              "see each bucket's subgroup_caps field; raw sums are: TREND 77, MOMENTUM 40, "
              "VOLUME_PA 55 (2026-07-16: TREND's raw sum +8 now that above_avwap_earnings is REAL). "
              "EXPLICIT CONTRACT (2026-07-21, external review - 'raw' and 'achievable' were used "
              "ambiguously in adjacent sentences): raw_max_points = every independently eligible "
              "rule's points summed before any cap (TREND 77 / MOMENTUM 40 / VOLUME_PA 55 above). "
              "effective_max_points = the max_points value shown per bucket below - the max "
              "achievable AFTER subgroup caps and mutually-exclusive branches (e.g. RSI's "
              "pullback/momentum-zone tiers) are applied; this is what 'actual achievable sum' "
              "means below. bucket_pct = earned points / effective_max_points - this, not a "
              "raw-points ratio, is what feeds _qualification_multiplier() and the composite "
              "weighting. raw_max_points is for auditing cap bite only (see latent_factors' "
              "raw vs capped fields); never normalize against it. "
              "Every bucket's max_points "
              "below is the actual achievable sum of that bucket's own rule "
              "points (fixed as a pure bug fix - several buckets used to declare a stale max lower "
              "than their true achievable sum, over-crediting a fully-loaded bucket relative to "
              "the others). Weights (2026-07-15 recalibration): TREND 22.5%, MOMENTUM 20.5%, "
              "VOLUME_PA 15%, EXTERNAL 16%, SENTIMENT_MACRO 15%, MARKET_BREADTH 11% - sums to "
              "100%. VOLATILITY_EXPANSION no longer carries composite weight: it's a pure "
              "additive bonus of up to +4 pts on the final score (its old 7% weight acted as a "
              "permanent drag on every stock not currently in a squeeze).",
    "buckets": [
        {
            "name": "TREND", "weight_pct": 22.5, "max_points": 67, "min_qualify_pct": 50,
            "subgroup_caps": {"trend_structure (SMA20/50/200 + EMA + weekly)": 38},
            "rules": [
                {"name": "above_sma200", "points": 15, "status": "REAL", "description": "Price above the 200-day SMA"},
                {"name": "above_sma50", "points": 10, "status": "REAL", "description": "Price above the 50-day SMA"},
                {"name": "above_sma20", "points": 8, "status": "REAL", "description": "Price above the 20-day SMA"},
                {"name": "ema9_gt_ema21", "points": 7, "status": "REAL", "description": "9-day EMA above 21-day EMA"},
                {"name": "adx_trending_bullish", "points": 10, "status": "REAL", "description": "ADX > 25 AND +DI > -DI (2026-07-15b: ADX measures trend strength, not direction - a strong DOWNTREND also has high ADX; the bullish directional side must confirm)"},
                {"name": "donchian_20d_break", "points": 5, "status": "REAL", "description": "Price at/above 20-day Donchian channel high - computed from daily OHLCV"},
                {"name": "above_avwap_earnings", "points": 8, "status": "REAL_APPROXIMATE", "description": "Price above anchored VWAP from last earnings date. REAL as of 2026-07-16: FMP's free /stable/earnings gives a genuine past report date. Anchor bar placement (2026-07-21, external review): EXACT (bmo_exact/amc_exact) when the winning bars provider supplied real per-bar dates AND FMP confirmed a bmo/amc report-time hint; APPROXIMATE (unknown_hint_approx or the old calendar-day 5/7-ratio calendar_fallback_approx) otherwise - see engine/ticker_analyzer.py's _calc_earnings_avwap() and TickerData.earnings_avwap_anchor_mode/confidence/date for the per-signal telemetry that distinguishes which happened on any given signal (surfaced as avwap_earnings_anchor_mode/confidence/date in threshold_result.data_coverage). Status labeled REAL_APPROXIMATE rather than plain REAL because both paths are live in production depending on data availability that cycle. Also a live hard veto (rules/hard_vetoes.py's BELOW_AVWAP, #7) that was previously dead code."},
                {"name": "weekly_trend_aligned", "points": 8, "status": "REAL", "description": "TRUE weekly resample as of 2026-07-15b (5-bar trading-week closes from the 1y daily history; weekly SMA20 needs >=20 weeks, SMA50 >=50). Falls back to the old daily proxy only when history is too short - the fallback is labeled in the adapter"},
                {"name": "rs_vs_spy_1m", "points": 6, "status": "REAL", "description": "Ticker's own ~21-bar return beats SPY's - per-ticker relative strength vs the market (2026-07-15, leaders outperform before breakouts)"},
            ],
        },
        {
            "name": "MOMENTUM", "weight_pct": 20.5, "max_points": 35, "min_qualify_pct": 40,
            "subgroup_caps": {"macd_family (cross + histogram + persistence)": 18},
            "rules": [
                {"name": "rsi_pullback / rsi_momentum_zone / rsi_oversold_broken_trend", "points": "12 / 8 / 4", "status": "REAL", "description": "DUAL-PATH with trend-integrity guard (2026-07-15b): oversold RSI earns the full 12 ONLY while price holds above SMA50 (a pullback in an uptrend); oversold below a broken SMA50 is a falling knife and earns a token 4. RSI 45-70 momentum zone earns 8; >70 overbought earns 0."},
                {"name": "macd_bullish_cross", "points": 8, "status": "REAL", "description": "MACD line crossed above signal line. Points cut 12 -> 8 (2026-07-21, external review's staged-credit fix): the crossover instant is initial turning-point evidence, not full confirmation - cross+hist together on day one (8+6=14) no longer reach the 18-pt MACD-family cap on their own; the momentum_persistent tier below is now required to reach the top of the cap."},
                {"name": "macd_hist_positive", "points": 6, "status": "REAL", "description": "MACD histogram > 0"},
                {"name": "stoch_pullback / stoch_momentum_zone", "points": "5 / 3", "status": "REAL", "description": "DUAL-PATH (2026-07-15): Stochastic %K below the risk-level's threshold (5) OR in the 40-85 momentum zone (3). >85 earns 0."},
                {"name": "momentum_persistent", "points": "3 / 5", "status": "REAL", "description": "MACD histogram has stayed positive for >=5 / >=10 consecutive trailing days - rewards conviction on top of macd_hist_positive's same-day check"},
            ],
        },
        {
            "name": "VOLUME_PA", "weight_pct": 15, "max_points": 48, "min_qualify_pct": 40,
            "subgroup_caps": {
                "accumulation_family (OBV + CMF + dollar-vol + accum days)": 20,
                # Nested inside accumulation_family (2026-07-21, external
                # review round 2 - "add an internal subgroup before the
                # broad volume cap"): obv_rising + obv_divergence_accumulation
                # /obv_new_high_20d are three views of the same OBV series;
                # this caps them at 9 BEFORE the broader 20-pt cap even
                # applies, so OBV alone can't consume most of that budget.
                "obv_family (obv_rising + obv_divergence/new_high, nested inside accumulation_family)": 9,
            },
            "rules": [
                {"name": "rvol_excellent / rvol_good / rvol_moderate", "points": "10 / 6 / 3", "status": "PROXY", "description": "Relative volume tier - rough proxy from volume_ratio, not true intraday-normalized RVOL"},
                {"name": "obv_rising", "points": 5, "status": "REAL", "description": "On-balance volume trending up over the last 5 bars"},
                {"name": "cmf_positive", "points": 6, "status": "REAL", "description": "Chaikin Money Flow > 0 - computed from daily OHLCV+volume"},
                {"name": "bb_lower_touch / bb_upper_ride", "points": "8 / 5", "status": "REAL", "description": "DUAL-PATH (2026-07-15): Bollinger %B < 0.20 near lower band (8) OR %B >= 0.60 while above SMA20 - riding the upper band in an uptrend (5)"},
                {"name": "above_vwap", "points": 4, "status": "REAL", "description": "Price above VWAP. Labeled above_vwap_multisession5d in every mode (2026-07-21, external review round 2 - corrected from an earlier pass that mislabeled DAY mode as 'session' VWAP while computing the identical number as SWING/HYBRID) - the underlying value is a cumulative VWAP over the ~5 trading days of intraday bars the provider returned this cycle (Alpaca/yfinance), not a true single-session VWAP that resets at market open. That matches the review's own suggested SWING design as-is - a 5-day VWAP isn't wrong, it just answers a different question than session VWAP - so it scores identically in every mode rather than claiming a precision this pipeline doesn't have. A true single-session VWAP for DAY mode needs per-bar intraday timestamps this pipeline doesn't carry yet (deferred, not faked). Also gated on ticker_analyzer.py's own VWAP staleness flag (stale_indicators) so incomplete/no intraday data this cycle can't award credit."},
                {"name": "avwap_swing_low_bounce", "points": 6, "status": "REAL", "description": "Price within 0.5xATR of the swing-low anchored VWAP (2026-07-15b: ATR-normalized - a fixed % band means different things across volatility regimes; floored at 0.5% for quiet names, capped at 2.5% for wild ones)"},
                {"name": "near_poc_support", "points": 0, "status": "PLACEHOLDER", "description": "Price near volume-profile point of control - never fires (no volume-profile data source); excluded from max_points"},
                {"name": "obv_divergence_accumulation / obv_new_high_20d", "points": "6 / 4", "status": "REAL", "description": "ACCUMULATION (2026-07-15): OBV at a 20-bar high while price is NOT (quiet institutional accumulation, 6) or with price confirming (4)"},
                {"name": "dollar_vol_expanding", "points": 5, "status": "REAL", "description": "ACCUMULATION (2026-07-15): 20d avg dollar volume >= 1.15x the 50d avg - liquidity/institutional interest building"},
                {"name": "accumulation_days", "points": 5, "status": "REAL", "description": "ACCUMULATION (2026-07-15): >=5 of the last 10 sessions closed up on above-20d-average volume"},
            ],
        },
        {
            "name": "EXTERNAL", "weight_pct": 16, "max_points": 48, "min_qualify_pct": 40,
            "rules": [
                {"name": "maverick_bullish", "points": 12, "status": "REAL", "description": "Maverick MCP sentiment score > 0.6"},
                {"name": "finviz_technical_rating", "points": 10, "status": "REAL", "description": "Finviz technical rating contains \"Buy\" or \"Strong Buy\""},
                {"name": "analyst_consensus", "points": 5, "status": "REAL", "description": "Analyst consensus is Buy/Strong Buy/Overweight. Multi-source fallback chain as of 2026-07-15d (no Finviz Elite required): finviz -> yfinance recommendationKey -> Finnhub free monthly Buy/Hold/Sell counts. Short float likewise falls back to yfinance shortPercentOfFloat"},
                {"name": "sector_rs_1m_positive_proxy", "points": 13, "status": "PROXY", "description": "Sector-vs-SPY relative strength positive (1mo) - same real sector-ETF-proxy data as SENTIMENT_MACRO's sector_rs_1m, reused here. Weight increased 10->13. A sector-level proxy, not true industry/peer-group relative strength. Renamed from industry_rs_positive (2026-07-21, external review - 'rename it to something operationally honest'); underlying ticker_data key is unchanged. Replace with true industry/peer RS once a classification + peer-universe source exists."},
                {"name": "unusual_options_bullish", "points": 0, "status": "PLACEHOLDER", "description": "Unusual bullish options flow - never fires; excluded from max_points. The real source, github.com/erikmaday/unusual-whales-mcp, was evaluated 2026-07-16 and confirmed genuine (actual options-flow alerts) but requires a paid UW_API_KEY with no free tier - not configured. Deliberately not replaced with a weaker yfinance call/put-skew approximation."},
                {"name": "estimate_raised", "points": 6, "status": "REAL", "description": "Recent analyst estimate raise. REAL as of 2026-07-16: FMP's free /stable/analyst-estimates consensus EPS, diffed against a stored prior reading (storage/database.py's estimate_snapshots table, one row/ticker/day). Returns no-credit (None) until 30 days of snapshot history exist per ticker, then fires on a genuine measured increase (>1%)."},
                {"name": "no_recent_downgrade", "points": 2, "status": "REAL", "description": "REAL as of 2026-07-16 (external review's default-True placeholder finally replaced): FMP's free /stable/grades gives real dated analyst rating-change events - verified live catching an actual KeyBanc AAPL downgrade. True only when FMP explicitly confirms no downgrade in the last 30 days; a data outage now resolves to no-credit, never a silent True. Points cut 5 -> 2 (2026-07-21, external review): absence of a negative event is weaker evidence than an affirmative positive signal and shouldn't be weighted the same as one; a real downgrade still costs the position via the symmetric exit-side analyst_downgrade penalty."},
            ],
        },
        {
            "name": "SENTIMENT_MACRO", "weight_pct": 15, "max_points": 34, "min_qualify_pct": 30,
            "rules": [
                {"name": "news_sentiment", "points": "up to 8", "status": "REAL", "description": "Scaled from the real 0-1 news sentiment score"},
                {"name": "sector_rs_1d_positive", "points": 8, "status": "PROXY", "description": "1-day sector-vs-SPY relative strength positive - real sector-ETF proxy, see engine/market_breadth.py"},
                {"name": "sector_rs_1m_positive", "points": 0, "status": "PROXY", "description": "Zeroed 2026-07-15 (external review): identical figure to EXTERNAL's sector_rs_1m_positive_proxy (renamed 2026-07-21 from industry_rs_positive - both read market_breadth.get_sector_return's return_1m) - one signal gets one route into the score. Kept in the checklist for observability"},
                {"name": "fg_optimal", "points": 4, "status": "REAL", "description": "Fear & Greed between 35-75 (widened from 35-65 on 2026-07-15: healthy bulls sit 65-75 for weeks; >75 euphoria still earns 0)"},
                {"name": "insider_net_buying", "points": 6, "status": "REAL", "description": "Net insider buying in the last 30 days"},
                {"name": "short_float_ok", "points": 4, "status": "REAL", "description": "Short float under 20%"},
                {"name": "yield_curve_ok", "points": 4, "status": "REAL", "description": "2s10s yield spread not deeply inverted (> -0.5%)"},
            ],
        },
        {
            "name": "MARKET_BREADTH", "weight_pct": 11, "max_points": 68, "min_qualify_pct": 35,
            "rules": [
                {"name": "ad_ratio", "points": 10, "status": "PROXY", "description": "Sector-ETF advance/decline ratio > 0.55 (see engine/market_breadth.py)"},
                {"name": "pct_above_20ema", "points": 8, "status": "PROXY", "description": "% of 11 sector ETFs above their own 20-EMA > 60%"},
                {"name": "pct_above_50ema", "points": 8, "status": "PROXY", "description": "% of 11 sector ETFs above their own 50-EMA > 55%"},
                {"name": "nh_gt_nl", "points": 8, "status": "PROXY", "description": "Sector new-highs > new-lows proxy > 1.0"},
                {"name": "mcclellan_positive", "points": 8, "status": "PROXY", "description": "McClellan-style breadth oscillator > 0"},
                {"name": "ad_line_5d_rising", "points": 8, "status": "PROXY", "description": "Sector breadth participation strictly rising over 5 days"},
                {"name": "spy_ad_aligned", "points": 10, "status": "PROXY", "description": "SPY's own SMA50 trend agrees with sector-breadth direction"},
                {"name": "breadth_accelerating", "points": 8, "status": "PROXY", "description": "Today's pct_above_20ema minus the prior reading > +5pp - rewards expanding participation, not just a static snapshot"},
            ],
        },
        {
            "name": "VOLATILITY_EXPANSION", "weight_pct": 0, "max_points": 14, "min_qualify_pct": 0,
            "rules": [
                {"name": "ttm_squeeze_firing", "points": 6, "status": "REAL", "description": "Bollinger Bands (20,2std) were compressed inside Keltner Channels (SMA20 +/-1.5x ATR14) within the last 5 bars and have now released"},
                {"name": "nr7_compression / nr4_compression", "points": "6 / 3", "status": "REAL", "description": "Today's high-low range is the narrowest of the last 7 (or, if not, of the last 4) trading days - mutually exclusive (elif)"},
                {"name": "inside_day", "points": 2, "status": "REAL", "description": "Today's high/low range sits entirely inside yesterday's"},
            ],
        },
    ],
    "note": "PLACEHOLDER rules essentially never fire (no data source wired yet - see "
            "engine/ticker_data_adapter.py for the full REAL/PLACEHOLDER tagging); the "
            "remaining ones are: near_poc_support (VOLUME_PA - no volume-profile data source "
            "in this stack) and unusual_options_bullish (EXTERNAL - the real source, "
            "github.com/erikmaday/unusual-whales-mcp, requires a paid API key not configured "
            "here; deliberately not faked with a weaker proxy). 2026-07-16: above_avwap_earnings "
            "(TREND) and estimate_raised/no_recent_downgrade (EXTERNAL) went REAL via FMP's "
            "free-tier /stable endpoints - see mcp_clients/market_data.py's FMPProvider. Every "
            "bucket's max_points is "
            "the true achievable sum of its own rules (fixed - several used to declare a stale, "
            "lower max that let a fully-loaded bucket over-credit its own 100%-of-weight share). "
            "Bucket qualification is a CONTINUOUS anchor-table multiplier on %-of-max "
            "(0%->0.0, 30%->0.35, 50%->0.60, 100%->1.00, linear between anchors) with NO cliff "
            "at any point - min_qualify_pct feeds only the displayed qualified flag. (This "
            "sentence previously described an older '0 at 60% of min_pct' ramp that no longer "
            "exists - stale text flagged by two external reviews and now definitively fixed; "
            "the single source of truth is _qualification_multiplier() in "
            "rules/swing_buy_rules.py, pinned by unit tests.) "
            "VOLATILITY_EXPANSION's min_qualify_pct is 0% on purpose: "
            "it's designed to confirm a setup with bonus points, not gate one - most good "
            "trend/momentum setups won't be in a squeeze at all, and that's expected, not a "
            "failure.",
}

DYNAMIC_THRESHOLD_CATALOG = {
    "engine": "rules/dynamic_thresholds.py - adjusts the base buy-score threshold per cycle",
    "formula": "final = base + MAX(regime_stress_adj, vix_stress_adj) + calendar_adj + transition_adj "
               "+ breadth_adj (all capped at +20 total) + EV_bonus (applied AFTER the cap), then clamped to 50%-85%. "
               "2026-07-15 recalibration: regime stress now includes a bull-dominance CREDIT (a clean bull "
               "LOWERS the bar, floor -5); the EV bonus is strictly 0 when no EV was actually measured "
               "(the old '+5 when 0<=EV<1' fired on every signal while the pattern DB was empty - a "
               "cold-start deadlock); calendar Mon+3/Wed-3 removed (no empirical basis), Fri+5 and OpEx kept.",
    "components": [
        {"name": "Regime/VIX stress", "description": "MAX (not sum) of regime-based stress and VIX-based stress (VIX>27: +13, VIX>22: +8) - avoids double-counting the same market condition"},
        {"name": "Calendar (LOG-ONLY)", "description": "Friday +5, OpEx week +5, post-OpEx +5 are computed and shown in every threshold breakdown but NOT applied (2026-07-15, external review: plausible intuitions with zero empirical support in this system's own trade history should not change entry eligibility). Enable via config thresholds.calendar_enabled once proven against real outcomes. Monday +3 / Wednesday -3 removed entirely"},
        {"name": "Mode adjustment (2026-07-15)", "description": "DAY mode +3% - a same-day round trip pays the spread twice and lives inside intraday noise, so it needs a visibly better setup than a multi-day swing. SWING and HYBRID (which scores through the swing engine) take no adjustment"},
        {"name": "Transition probability", "description": "regime.transition_probability * 0.08 - a regime that looks like it's about to flip raises the bar"},
        {"name": "Breadth adjustment (2026-07-15)", "description": (
            "Tiered additive modifier from sector-ETF breadth proxy (engine/market_breadth.py). "
            "Replaces the old single-indicator hard block (McClellan<-70 OR A/D<0.30) — "
            "weak breadth raises the buy threshold instead of silencing the whole scan. "
            "Tiers: excellent (McClellan≥30 & A/D≥0.65): -3% (tailwind); "
            "good (McClellan≥0 & A/D≥0.50): 0%; "
            "weak (either slightly negative): +5%; "
            "very_weak/panic: capped at +8% (2026-07-15, external review - breadth already has a "
            "full scoring route via the MARKET_BREADTH bucket, so its threshold authority is "
            "limited to modest risk control; the true-panic hard block lives in "
            "rules/market_filters.py's multi-signal crisis gate). "
            "A/D of exactly 0.0 is flagged as possibly a data artifact (ad_ratio_suspect) "
            "and clipped so a stale pre-market bar cannot push into the panic tier alone."
        )},
        {"name": "EV bonus", "description": "Applied after the +20% cap, and ONLY when an EV was actually measured from enough similar closed trades: EV>3% -> -5 (easier), EV>2% -> -3, EV<0% -> +5 (harder). No pattern-DB history -> strictly 0 (the pre-2026-07-15 version charged +5 on every signal while the DB was empty - a cold-start deadlock)"},
        {"name": "Risk-level base thresholds", "description": "CONSERVATIVE 68 / MODERATE 60 / AGGRESSIVE 55 / TURBO 50 (config.yaml risk.<level>.buy_score_threshold_pct, recalibrated 2026-07-15 - TURBO was previously identical to AGGRESSIVE). Final clamped to 50-85%. TURBO structural gate (2026-07-21, external review): TURBO's low 50% base can be reached by moderately positive evidence spread across a few correlated buckets even on a structurally damaged name, so TURBO carries one extra, TURBO-only eligibility condition on top of the composite score/EV decision - TREND bucket must be >=40% of its own effective max AND at least one of price>SMA50 / price>earnings AVWAP / 1mo RS>SPY must hold. Checked AFTER the score/probabilistic decision so it can't be bypassed by either path; failing it flips passed to False and is recorded in threshold_result.turbo_structural_gate. Not a general bucket-qualification cliff - only applies when risk_level=='TURBO'."},
    ],
}

DATA_SOURCE_RESILIENCE_NOTE = (
    "2026-07-15: every non-yfinance data source (finviz, maverick, stock-scanner) now has a "
    "per-source circuit breaker (3 consecutive failures -> source skipped for 5-15 min instead of "
    "burning a 45s timeout per ticker) and a per-ticker TTL cache (finviz/scanner 6h - ratings and "
    "insider data churn daily at most; maverick 10 min). Maverick availability is re-checked every "
    "5 min instead of once at process start (the old one-time check meant a Maverick started after "
    "the scheduler was treated as down forever). These exist because all three sources went dark on "
    "2026-07-14 under the screener's 38-70-tickers-per-cycle load, which zeroed the EXTERNAL and "
    "SENTIMENT buckets on every signal and roughly doubled cycle time."
)

HARD_VETOES_CATALOG = {
    "engine": "rules/hard_vetoes.py - evaluated BEFORE scoring. Any single veto fires -> ticker skipped entirely, no bucket scoring runs.",
    "vetoes": [
        {"code": "EARNINGS_RISK", "description": "Composite earnings-risk score > 80/100. Days-to-earnings sub-score dominates and is the only piece that actually contributes today: earnings TODAY=80pts, tomorrow=70, in 2 days=60, <=4 days=40, <=7 days=20. The options-expected-move and historical-earnings-move sub-scores are wired into the formula but both read PLACEHOLDER fields (options_expected_move_pct/historical_earnings_move_avg_pct, hardcoded 0.0 - no data source for either exists in this stack yet), so in practice this veto is currently a pure days-to-earnings check."},
        {"code": "SPREAD_WIDE", "description": "Graded, mode-aware spread check (rules/spread_quality.py) - only the outermost tier hard-vetoes: >0.50% of price (day mode) / >1.00% (swing mode). Replaced the old flat 0.15%-for-everything cliff, which penalized low-priced stocks and ignored that spreads widen temporarily around FOMC/CPI/earnings/the open. Spreads between the 'good' and 'veto' tiers don't reject the setup - they apply a graded score penalty instead (see rules/swing_buy_rules.py). 2026-07-14: added a data-quality plausibility guard - a bid/ask implying an implausible spread (>10% of price, or either side more than 50% off the live price) is now treated as an unreliable quote (neutral, not a veto) rather than a real spread, since yfinance's free bid/ask fields are frequently 0/stale outside a live NBBO feed and were producing 15-53% 'spreads' on names that don't structurally trade that wide."},
        {"code": "STALE_DATA_CIRCUIT_BREAKER", "description": "Data Provenance Circuit Breaker (added 2026-07-14): counts how many of the 5 core indicators (RSI, MACD, TREND, VWAP, BREADTH) silently fell back to a default this cycle because the real calc failed or its input data was missing - not the same as data_quality/missing_sources (whole-source dropout). If the count reaches config.yaml's data_quality.stale_indicator_veto_threshold (default 3 of 5), the ticker is vetoed outright rather than scored on a majority-default indicator set. Very common in the first few minutes after/before the open - VWAP needs intraday bars that haven't accumulated yet - usually self-resolves as the session progresses, not necessarily a data bug."},
        {"code": "LOW_VOLUME", "description": "Average volume below 1M (swing) / 2M (day)"},
        {"code": "PRICE_RANGE", "description": "Price outside $10-$1000"},
        {"code": "BREADTH_PANIC (REMOVED 2026-07-15)", "description": "Was: McClellan-style oscillator < -70. Removed — single-indicator breadth is too noisy as a per-ticker hard block. Now flows through the MARKET_BREADTH scoring bucket and dynamic threshold breadth_adj (+5/+10/+15% tiered). Hard block reserved for multi-signal crisis in rules/market_filters.py."},
        {"code": "AD_COLLAPSE (REMOVED 2026-07-15)", "description": "Was: Sector-ETF A/D ratio < 0.30. Removed alongside BREADTH_PANIC — see above. A/D of exactly 0.00 is now flagged as ad_ratio_suspect (data artifact) rather than silencing the scan."},
        {"code": "REG_NEWS", "description": "Negative regulatory news classified with sentiment < 0.20 - PLACEHOLDER, never fires (news classification not wired)"},
        {"code": "BELOW_AVWAP", "description": "Price below anchored VWAP from last earnings. REAL and LIVE as of 2026-07-16 - was previously dead code (avwap_earnings was always 0.0); now sourced from FMP's real last-earnings-report date, see engine/ticker_analyzer.py's _calc_earnings_avwap(). Anchor precision is REAL_APPROXIMATE (2026-07-21) - see the above_avwap_earnings rule entry for exact vs. approximate anchor modes."},
        {"code": "STALE_QUOTE", "description": "Quote older than 30 min (swing) / 2 min (day). REAL as of 2026-07-21 (external review - 'freshly fetched does not always mean fresh market data'): age is now measured from the winning quote provider's own market timestamp (Alpaca/Finnhub/Tiingo/TwelveData/financequery), not from when this codebase's HTTP call ran. Only checked when a provider actually supplied a timestamp this cycle (quote_age_is_measured) - otherwise this veto stays silent, same as before, rather than guessing."},
        {"code": "KILL_SWITCH", "description": "config.yaml's risk.kill_switch_triggered is on"},
        {"code": "DAILY_LOSS", "description": "config.yaml's risk.daily_loss_limit_triggered is on"},
        {"code": "PROFIT_LOCK", "description": "config.yaml's risk.daily_profit_lock_triggered is on"},
        {"code": "COOLDOWN", "description": "Ticker is in a post-stop-loss re-entry cooldown (set by confirm_fill.py's cmd_sell)"},
        {"code": "DEAD_ZONE / TOO_LATE", "description": "Day-trade mode only: blocks 11:30am-1:30pm ET and after 3:30pm ET"},
        {"code": "BAD_DATA", "description": "Data completeness below 40% (too many missing sources)"},
        {"code": "ALREADY_OPEN", "description": "A position is already open for this ticker (not technically a veto - the ticker just isn't re-scored as a new entry; sell_rules.py/Loop B govern it instead)"},
    ],
}


MARKET_GATE_CATALOG = {
    "engine": "Two market-WIDE gates, evaluated ONCE per cycle before any ticker is even fetched - "
              "one step earlier than the screener/hard-vetoes above, which run per-ticker. Both must "
              "pass for the cycle to proceed to scoring at all.",
    "layers": [
        {
            "name": "1. Coarse gate", "function": "engine/market_context.py's evaluate_market_gate()",
            "description": "Simple pass/fail checks, first failure wins, whole CYCLE skipped (not just one ticker): "
                           "Fear & Greed within [market_filters.fear_greed.no_buy_below, no_buy_above] "
                           "(config default 20-85), VIX under market_filters.vix.no_trade_above (default 28), "
                           "no active macro blackout (market_filters.macro_event_blackout - CPI/FOMC/NFP hours-before "
                           "windows), and the account kill switch off. Missing/unreachable data is treated as "
                           "PASSING - a briefly-down free/MCP source never itself halts trading.",
        },
        {
            "name": "2. Scored gate", "function": "rules/market_filters.py's evaluate()",
            "description": "Starts at 100 and subtracts: VIX >max -40 (or -20 if within 85% of max), F&G outside "
                           "[fg_min,fg_max] -40, macro blackout -30, tiered breadth penalty (excellent 0 / good 0 / "
                           "weak -15 / very_weak -25 / panic -40, from the SAME sector-ETF McClellan+A/D proxy the "
                           "MARKET_BREADTH scoring bucket and dynamic-threshold breadth_adj use - a suspect A/D of "
                           "exactly 0.00 is clipped to a neutral 0.10-0.90 band first so a data artifact can't drive "
                           "the penalty on its own). Needs a final score >=40 to proceed to the screener/scoring "
                           "stages; a CRISIS regime (engine/regime_engine.py's crisis_active) or a genuine "
                           "multi-signal breadth crisis (ALL FOUR of McClellan<-70 AND A/D<0.30 AND VIX>35 AND SPY "
                           "below its 200DMA agreeing simultaneously) hard-blocks the cycle outright at score 0, "
                           "regardless of the 40-point line. Any SINGLE one of those four signals alone is "
                           "deliberately NOT enough - see the 2026-07-15 breadth redesign note on the "
                           "REMOVED BREADTH_PANIC/AD_COLLAPSE hard vetoes above for why single-indicator breadth "
                           "blocks were retired in favor of this multi-signal design.",
        },
    ],
}

TRADING_MODES_CATALOG = {
    "engine": "config.yaml's trading.mode (DAY/SWING/HYBRID) changes real behavior at both ENTRY and "
              "through a position's life - not just a cosmetic label or scan-cadence change. Built "
              "2026-07-22 to close a gap where DAY/HYBRID positions used to get identical scoring/stops/"
              "sizing to SWING except for a flat +3% threshold nudge.",
    "entry_differences": [
        {"aspect": "Scan cadence", "DAY": "5 min", "SWING": "15 min", "HYBRID": "5 min (same as DAY)"},
        {"aspect": "Hard-veto volume floor", "DAY": "2,000,000 avg vol", "SWING": "1,000,000", "HYBRID": "1,000,000 (falls through to SWING's default)"},
        {"aspect": "Hard-veto quote staleness", "DAY": "2 min", "SWING": "30 min", "HYBRID": "30 min (unchanged)"},
        {"aspect": "Dead-zone / too-late time vetoes", "DAY": "yes (11:30am-1:30pm, after 3:30pm ET)", "SWING": "no", "HYBRID": "no"},
        {"aspect": "Spread hard-veto ceiling", "DAY": "0.50%", "SWING": "1.00%", "HYBRID": "1.00%"},
        {"aspect": "Bucket weights", "DAY": "weights.swing_buy_day/swing_buy_etf_day - VOLUME_PA/MOMENTUM/MARKET_BREADTH up, TREND/EXTERNAL down (see config.yaml)", "SWING": "weights.swing_buy/swing_buy_etf", "HYBRID": "same as SWING - scores through the full swing engine on purpose, judged on multi-day evidence before ever being classified a day leg"},
        {"aspect": "Base threshold", "DAY": "risk.<profile>.buy_score_threshold_day_pct (base +5, e.g. TURBO 50->55)", "SWING": "risk.<profile>.buy_score_threshold_pct", "HYBRID": "same as SWING"},
        {"aspect": "Dynamic-threshold mode adjustment", "DAY": "+3% (layered ON TOP of the higher DAY base above - a structurally-better setup requirement vs. a same-day spread/noise cost, two different things being priced)", "SWING": "+0%", "HYBRID": "+0%"},
        {"aspect": "Quality-gate min avg volume (screener)", "DAY": "2,000,000 floor", "SWING": "config value", "HYBRID": "2,000,000 (screener-side mode==\"day\" check)"},
    ],
    "hybrid_post_decision_classification": (
        "A HYBRID buy signal that clears its bar is tagged DAY by scheduler.py's _classify_hybrid_leg() "
        "only if BOTH: it clears a stricter bar (final threshold +3%, mirroring DAY's own penalty) AND it "
        "shows real intraday character (volume >=1.5x average, or a same-day move >=2%). Otherwise it's "
        "tagged SWING. This classification, stored in positions.trade_mode, then drives real behavior for "
        "the rest of that position's life: position_sizing.day_size_multiplier (default 0.5) applies to a "
        "DAY-classified leg; the risk-per-share seed reads stop_loss_day_pct instead of stop_loss_swing_pct "
        "(cascading into both the R-multiple take-profit target and the ATR stop machine's ceiling); and the "
        "position becomes eligible for the forced EOD flatten below. A SWING-classified HYBRID leg is "
        "unaffected by any of this."
    ),
    "eod_flatten": (
        "trading.day_eod_flatten_enabled (default true) forces every open DAY-tagged position closed by "
        "day_eod_flatten_time_et (default 15:55 ET, 5 min before the close) - engine/position_management.py's "
        "run_loop_b() checks this every cycle and, once past the cutoff, returns a priority-1 URGENT exit_full "
        "action - the same priority tier and execution path as the kill switch / daily-loss-limit circuit "
        "breakers. A SWING or SWING-classified-HYBRID position is never checked against this cutoff and can "
        "carry overnight as before."
    ),
    "mode_position_cap": (
        "trading.max_day_positions (default 5) caps concurrently-open DAY positions specifically, separate "
        "from the global trading.max_positions (10) that counts DAY+SWING+HYBRID together. Checked BEFORE "
        "the existing global max-positions/rotation logic in both paper_trader.py and live_trader.py. "
        "Deliberately does NOT rotate - a DAY leg hitting its own cap just skips the buy and waits for the "
        "next cycle or an EOD flatten, rather than being allowed to force out an unrelated SWING holding."
    ),
    "ev_pattern_pooling": (
        "engine/ev_engine.py's EV/probability-of-win lookup (see PROBABILISTIC_DECISION below) pools DAY and "
        "SWING patterns SEPARATELY (pattern_database rows are written with the resolved DAY/SWING label, "
        "fixed 2026-07-22 after a bug where every row was hardcoded \"SWING\" regardless of actual mode - see "
        "pre_selection_criteria_and_trading_modes.md Section 4 for the full incident writeup). A HYBRID "
        "account's lookups pool with whichever of DAY/SWING that leg actually resolved to - there is no "
        "separate \"HYBRID\" pattern bucket, since a HYBRID leg is always eventually one or the other."
    ),
    "unchanged_across_modes": (
        "Exit rule TRIGGERS (rules/sell_rules.py's hard exits, rules/exit_scorer.py's 6-bucket soft Exit "
        "Score) have no mode branching - only the DISTANCE those triggers fire at changes for DAY (tighter "
        "stop ceiling, tighter take-profit, via the risk-per-share seed above) plus the EOD flatten sitting "
        "above them in priority. The scoring engine's rule LOGIC and correlated-evidence caps are identical "
        "math regardless of mode - only the six buckets' relative WEIGHTS and the composite's base threshold "
        "change for DAY. risk.max_trades_per_day is still one shared daily budget across all three modes "
        "combined - no DAY-specific daily-loss cooldown exists yet."
    ),
}

ACCOUNT_RISK_CATALOG = {
    "engine": "rules/risk_rules.py - per-trade / per-day ACCOUNT-level checks, distinct from "
              "market_filters.py (market-wide) and buy/sell_rules.py (per-ticker signal rules). Most of "
              "these are also exposed as hard vetoes above (KILL_SWITCH/DAILY_LOSS/PROFIT_LOCK) so a "
              "breach blocks scoring entirely, not just order placement.",
    "checks": [
        {"name": "kill_switch", "description": "config.yaml's risk.kill_switch_triggered - manual or auto-tripped (see trip_kill_switch_if_needed below), halts all trading until manually cleared + restarted"},
        {"name": "max_trades_per_day", "description": "config.yaml's risk.max_trades_per_day (default 10) - shared budget across DAY+SWING+HYBRID combined"},
        {"name": "max_daily_loss", "description": "Realized P&L today must stay above -risk.max_daily_loss_usd (default $500). Auto-trips the kill switch (see below) rather than just blocking new entries once breached"},
        {"name": "max_positions", "description": "config.yaml's trading.max_positions (default 10) - open position count ceiling, separate from the DAY-specific trading.max_day_positions cap (see Trading Modes above)"},
        {"name": "max_position_size", "description": "Any single position's dollar amount must stay under risk.max_position_size_usd (default $500) - the hard ceiling position_sizing.py's suggested dollar amount is clamped to"},
        {"name": "sufficient_buying_power", "description": "Candidate dollar amount must not exceed the account's available buying power (real accounts only)"},
    ],
    "kill_switch_auto_trip": (
        "trip_kill_switch_if_needed() runs every cycle: if realized P&L today breaches -max_daily_loss_usd "
        "and the kill switch isn't already on, it flips risk.kill_switch_triggered=true, PERSISTS that to "
        "config.yaml on disk, and logs a CRITICAL entry. By design this function only ever flips the switch "
        "ON - clearing it requires a manual config edit + restart, so a bad day can't quietly self-heal "
        "without a human looking at it first."
    ),
}

PORTFOLIO_RISK_CATALOG = {
    "engine": "engine/portfolio_risk.py - evaluates a NEW candidate against the REST of the currently-open "
              "book across five independent exposure dimensions, each producing its own size_multiplier "
              "(0.0-1.0); the final multiplier is the MINIMUM across all five (worst dimension wins, not an "
              "average) and feeds engine/position_sizing.py as one factor in its chain. This governs SIZE, "
              "not eligibility, unless portfolio_risk.hard_block_on_severe_breach is enabled (default false) "
              "- a severe breach (multiplier hits 0.0) then blocks the trade outright instead of just sizing "
              "it to zero.",
    "dimensions": [
        {"name": "Sector exposure", "cap": "max_sector_exposure_pct (default 35%)", "description": "% of total open-position dollars in the candidate's sector, pre- and post-trade; scales size down as the post-trade % approaches the cap"},
        {"name": "Theme exposure", "cap": "max_theme_exposure_pct (default 40%)", "description": "Same idea, but for hand-curated theme baskets (config.yaml's portfolio_risk.theme_map, e.g. AI/SEMICONDUCTORS/MEGA_CAP_TECH/EV_AUTO) that cut across GICS sectors - a stock can belong to multiple themes; the WORST theme's exposure governs"},
        {"name": "Pairwise correlation cluster", "cap": "high_correlation_threshold 0.75, max_high_correlation_cluster 3", "description": "60-day pairwise price correlation (Pearson) between the candidate and every open position. >=3 existing positions already correlated >=0.75 with the candidate -> size multiplier 0.0 (they'd behave like one trade); 1-2 -> 0.6 (diversification buffer)"},
        {"name": "Aggregate portfolio beta", "cap": "max_portfolio_beta (default 1.6)", "description": "Dollar-weighted average beta across the book including the candidate at full size - scales size down as the post-trade weighted beta approaches the cap"},
        {"name": "Simultaneous high-volatility positions", "cap": "high_vol_atr_pct_threshold 5.0% of price, max_simultaneous_high_vol_positions 4", "description": "Counts open positions whose ATR is already >=5% of price. A high-vol candidate hitting the cap -> 0.0; one below the cap -> 0.5 (reduced size, near the limit)"},
    ],
    "note": "All five checks are skipped (size_multiplier=1.0) when portfolio_risk.enabled is false. In WATCH "
            "mode, the SIMULATED (paper) book is what gets risk-managed rather than the real book it was "
            "cloned from, to avoid double-counting the same exposure twice.",
}

EXECUTION_QUALITY_CATALOG = {
    "engine": "rules/execution_quality.py - a 0-100 score from FOUR components, feeding TWO separate "
              "downstream effects: a small additive adjustment folded into the buy SCORE itself (bounded "
              "+/-execution_quality.score_adjustment_bounds, default max +3/-8), and an independent "
              "size_multiplier consumed by engine/position_sizing.py. Does NOT replace rules/hard_vetoes.py's "
              "SPREAD_WIDE veto or swing_buy_rules.py's own spread_penalty - both stay as they were; this is "
              "additive NEW information (dollar volume / slippage / consistency) spread alone never captured.",
    "components": [
        {"name": "Spread", "weight_pct": 20, "status": "REAL", "description": "Reuses rules/spread_quality.py's tiered bid/ask evaluation (excellent..veto -> 100..0)"},
        {"name": "Dollar volume", "weight_pct": 35, "status": "REAL", "description": "avg_volume x price, banded: >=$50M/day->100, >=$15M->80, >=$5M->55, >=$1M->25, else 0 - the same spread means very different risk on a $50M/day name vs. a $500K/day name"},
        {"name": "Slippage estimate", "weight_pct": 25, "status": "PROXY", "description": "MODEL, not real fill data (this codebase never places orders from Python - nothing to calibrate against yet): half the spread (crossing it) plus a market-impact term scaled by the candidate trade's dollar size relative to the stock's own average dollar volume. Treat as directional, not exact"},
        {"name": "Liquidity consistency", "weight_pct": 20, "status": "PROXY", "description": "How far today's volume_ratio sits from a \"normal\" 0.6x-1.8x band (100pts inside it, 60pts in a wider 0.3x-3.0x band, 25pts outside) - a proxy since ticker_data doesn't carry a multi-day volume series for a true rolling-consistency calculation"},
    ],
    "tiers": "EXCELLENT >=85, GOOD >=65, ACCEPTABLE >=45, POOR >=20, VERY_POOR <20",
    "size_multiplier": "Linear map from total score to execution_quality.size_multiplier_bounds (default 0.5-1.0) - poor execution quality is a real capital-at-risk concern independent of setup conviction, so it gets its own lever in position sizing on top of the small score adjustment.",
}

POSITION_SIZING_CATALOG = {
    "engine": "engine/position_sizing.py - closed a gap where every qualifying BUY got the exact same flat "
              "dollar amount (trading.trade_size_usd) regardless of setup strength. Multiplies SEVEN "
              "independent factors together, then clamps to [position_sizing.min_size_pct, max_size_pct] "
              "(default 20%-100%) of the base allocation, capped at risk.max_position_size_usd. Never "
              "places an order - only computes a SUGGESTED size for the human (or MCP-driven) execution "
              "step to act on.",
    "pipeline": "Buy Score -> Expected Value confidence -> Volatility -> Regime -> Portfolio Risk -> Execution Quality -> Mode -> Position Size",
    "factors": [
        {"name": "Score tier (base %)", "description": "position_sizing.score_tiers: >=85->100%, >=75->70%, >=65->40%, else 25% - sets the base before any multiplier is applied"},
        {"name": "EV confidence multiplier", "description": "From engine/ev_engine.py's confidence label (see Probabilistic Decision below): insufficient x0.50, low x0.70, moderate x0.90, high x1.00 - a fresh/thin pattern DB shrinks size even on a high-scoring setup"},
        {"name": "Volatility (ATR%) multiplier", "description": "position_sizing.volatility_atr_pct_bands: ATR<=2% of price x1.00, <=4% x0.85, <=6% x0.65, else x0.45"},
        {"name": "Regime/transition multiplier", "description": "engine/regime_engine.py's transition_size_scalar() - a regime that looks like it's about to flip shrinks size, same transition_probability signal the dynamic threshold uses"},
        {"name": "Portfolio risk multiplier", "description": "engine/portfolio_risk.py's size_multiplier (see Portfolio Risk above) - sector/theme/correlation/beta/high-vol exposure caps"},
        {"name": "Execution quality multiplier", "description": "rules/execution_quality.py's size_multiplier (see Execution Quality above) - a SEPARATE, independent lever from its small buy-score adjustment"},
        {"name": "DAY-mode multiplier", "description": "position_sizing.day_size_multiplier (default 0.5) applied only when the resolved trade mode is DAY - higher-frequency trades risk less per trade (see Trading Modes above)"},
    ],
}

PROBABILISTIC_DECISION_CATALOG = {
    "engine": "rules/probabilistic_decision.py + engine/ev_engine.py - replaces the flat score-vs-threshold "
              "cliff (a 68% score and a 95% score used to be treated identically once both cleared the same "
              "bar) with an actual probability/EV-driven should_buy call, whenever there's enough pattern-"
              "database history to trust it. Does NOT touch hard vetoes, market gates, or the data-quality "
              "circuit breaker - those all still run before this, unchanged.",
    "modes": [
        {
            "name": "probabilistic", "condition": "Pattern DB has enough similar closed trades for this exact setup_type/regime/mode combo (learning/pattern_database.py's MIN_RECENCY_COUNT_BY_FREQUENCY, typically 15+)",
            "decision": "should_buy = (EV > probabilistic_decision.min_ev_pct) AND (P(win) >= probabilistic_decision.min_win_probability) - both config-driven (defaults: min_ev_pct 0.0, min_win_probability 0.50)",
        },
        {
            "name": "score_fallback", "condition": "Not enough pattern-DB history yet for this setup (common early in the system's life, or for a rare setup/regime combo even later)",
            "decision": "should_buy = the pre-existing final_score_pct >= final_threshold_pct decision, UNCHANGED behavior - but always labeled score_fallback in the UI/prompt so it's never mistaken for a probability-backed call",
        },
    ],
    "ev_engine_outputs": (
        "engine/ev_engine.py computes p_win with a Wilson confidence interval (so a 12-match estimate and a "
        "200-match estimate aren't trusted equally), plus expected_return_pct, P(>target% gain), "
        "P(stop-loss-range loss), expected_drawdown_pct, and expected_hold_hours from the outcome "
        "distribution. HONESTY NOTE (carried from the module's own docstring): p_stop_loss / "
        "expected_drawdown_pct are proxies computed from the horizon outcome (entry vs. price N days later), "
        "not a real intraday path - most pattern rows are simulated fixed-hold-days closes with no stop ever "
        "actually checked along the way, so there is no genuine 'did a stop fire' signal to read yet."
    ),
    "confidence_labels": "insufficient (<15 matches), low (<30), moderate (<75), high (>=75) - drives both the should_buy gate's trustworthiness and position_sizing.py's EV confidence multiplier",
    "comparison_field": "threshold_would_have_passed is always computed and surfaced even in probabilistic mode (comparison-only, never gates the decision) - lets the prompt/UI show when the old threshold method and the new probabilistic method would have disagreed.",
}

REGIME_ENGINE_CATALOG = {
    "engine": "engine/regime_engine.py - the CANONICAL, single shared source of market regime for every "
              "downstream module (dynamic_thresholds, swing_buy_rules, market_filters, position_sizing, "
              "pattern_features all read current_state() rather than recomputing independently). Calculated "
              "once per cycle from five weighted signals into three regime scores that are then normalized "
              "to percentages.",
    "signals": [
        {"name": "SPY vs SMA200", "points": 30, "description": ">0.5% above -> bull, >0.5% below -> bear, else -> choppy (20pts)"},
        {"name": "SPY vs SMA50", "points": 20, "description": ">0.3% above -> bull, >0.3% below -> bear, else -> choppy (25pts)"},
        {"name": "VIX level", "points": 25, "description": "<15 -> bull, >25 -> bear, else -> choppy (30pts)"},
        {"name": "Fear & Greed", "points": 20, "description": ">60 -> bull, <30 -> bear, else -> choppy (15pts)"},
        {"name": "Sector-ETF A/D ratio", "points": 25, "description": ">0.60 -> bull, <0.35 -> bear, else -> choppy (15pts)"},
    ],
    "outputs": "bull_pct/bear_pct/choppy_pct (normalized shares of total evidence weight), transition_probability (independent of dominant regime - can be high in any regime, from how much bull_pct has moved cycle-over-cycle plus a breadth-divergence check), crisis_active (a separate, stricter multi-signal flag - see Market Gate above), dominant_regime (BULL/BEAR/CHOPPY/CRISIS), confidence_gap and confidence_level (VERY_HIGH..VERY_LOW, from how far the dominant regime's % leads the second-highest).",
    "consumers": "dynamic_thresholds.py's regime-stress adjustment and transition_probability x0.08 term, position_sizing.py's transition_size_scalar(), swing_buy_rules.py's TURBO structural gate context, market_filters.py's crisis_active hard block.",
}

ROTATION_CATALOG = {
    "engine": "engine/rotation.py - when the book is full (trading.max_positions) and a NEW candidate fires "
              "a buy signal, decides whether an existing holding should be sold to make room. Deliberately "
              "does NOT compare the candidate's entry score to holdings' (stale) entry scores - a buy score "
              "measures setup quality at entry, which is consumed the moment you enter, so that would be "
              "apples-to-oranges. The rotation VICTIM is chosen entirely by the EXIT side's own opinion "
              "(engine/position_health.py's health score), never by how shiny the new candidate looks.",
    "guardrails": [
        {"name": "rotation.enabled", "default": False, "description": "master switch - off by default"},
        {"name": "min_candidate_score", "default": 80.0, "description": "only a top-tier setup may displace anything held"},
        {"name": "max_victim_health_score", "default": 55, "description": "victim's position_health_score must already be at deep MONITOR/REDUCE tier or worse - a STRONG_HOLD/HOLD position is never sacrificed no matter how strong the candidate. Positions Loop B hasn't health-scored yet (NULL) are never eligible"},
        {"name": "min_hold_days", "default": 3, "description": "a position gets time to develop before it can be rotation-eligible (anti-churn)"},
        {"name": "max_rotations_per_week", "default": 2, "description": "budget per book (paper/live counted separately), persisted so restarts don't reset it"},
    ],
    "victim_selection": "Among all guardrail-eligible holdings, the LOWEST health score loses. Every existing execution guard (buying power, kill switch, breaker) still applies to both legs of the rotation.",
}

STOP_MACHINE_CATALOG = {
    "engine": "engine/stop_state_machine.py - 6-state ATR-based stop, only ever moves in the "
              "trade's favor (never widened). MODE-AWARE as of 2026-07-22 (Trinath's stop-machine "
              "review): every ATR multiplier and R-based staging threshold is read from "
              "config.yaml's stop_machine.DAY / stop_machine.SWING sections instead of being "
              "shared identically between both modes - DAY trails faster/tighter (earlier "
              "breakeven, closer profit-protect trail, earlier trend-trail activation) since a "
              "DAY position is force-flattened same-day regardless of where its stop sits "
              "(day_eod_flatten_enabled), so it doesn't need swing-sized room. A config missing "
              "the stop_machine section falls back to the pre-2026-07-22 numbers (same "
              "1.2/1.5/2.0 multipliers and 0.5R/1R/2R staging for both modes), so this degrades "
              "gracefully rather than breaking.",
    "states": [
        {"name": "INITIAL_RISK", "description": "entry - (ATR x multiplier), multiplier tiered by entry-signal score (weak/standard/strong) and mode. Widened by atr_spike.multiplier_bonus when ATR% of price clears atr_spike.atr_pct_threshold (a real volatility shock, not normal chop) - pairs with position_sizing.volatility_atr_pct_bands, which already shrinks size in the same regime. Always clamped to the mode's risk.<level>.stop_loss_day_pct/stop_loss_swing_pct ceiling - this is the one hard cap on how wide the stop can ever be. 2026-07-20 fix: a previous max()-against-raw-price floor silently overrode the tiered ATR distance with a ~0.75-1% stop regardless of score/volatility, causing premature stop-outs - removed, not reintroduced by the mode-awareness or spike-widening work."},
        {"name": "TRADE_CONFIRMING", "description": "Price above entry AND above the earnings-anchored VWAP - stop moves to entry - 1R (or AVWAP support minus 0.5xATR if that's tighter)"},
        {"name": "BREAKEVEN", "description": "Profit >= mode's breakeven_r (DAY: earlier, e.g. 0.3R; SWING: 0.5R) - stop moves to entry + breakeven_lock_r x risk-per-share, a small buffer above true breakeven"},
        {"name": "PROFIT_PROTECT", "description": "Profit >= mode's profit_protect_r (DAY: 0.8R; SWING: 1.0R) - locks in profit_protect_lock_r x R, trailed no further back than current price minus profit_protect_trail_atr_mult x ATR (DAY trails closer - 0.75x ATR vs SWING's 1.5x, per the review's 'day should lock more of the profit' guidance)"},
        {"name": "TREND_FOLLOWING", "description": "Profit >= mode's trend_trail_r (DAY: 1.5R; SWING: 2.0R) - trails trend_trail_atr_mult x ATR (DAY: 0.75x, tighter; SWING: 1.5x, wider to avoid normal multi-day noise shake-outs) below the position's high watermark, floored at swing-low support minus 0.25xATR when available"},
        {"name": "THESIS_BROKEN", "description": "Exit Score (rules/exit_scorer.py) >= 90 - immediate exit at ~market, overrides every other stage regardless of profit/loss state"},
    ],
    "note": "Live per-mode values (multipliers, R-thresholds, trail multiples, ATR-spike "
            "threshold/bonus, and the risk-level caps) are shown below, read straight from "
            "config.yaml - these ARE the numbers in effect right now, not just documentation.",
}

SELL_RULES_CATALOG = {
    "hard_exits": {
        "engine": "rules/sell_rules.py - single trigger wins, checked BEFORE the Exit Score below. "
                  "Config-driven (config.yaml's sell_rules.rules) - these are risk-management, "
                  "profit-taking, or event-avoidance decisions where one signal firing IS correct, "
                  "not something that should wait on a vote from the rest of the Exit Score.",
        "rules": [
            {"name": "stop_loss / trailing_stop", "status": "REAL", "description": "Reads current_stop_price from the mode-aware ATR-based 6-state stop machine (engine/stop_state_machine.py, see the Stop Machine panel below) once Loop B has run for this position; falls back to the flat config pct only on the very first cycle after entry, and that fallback pct is itself clamped to the same mode-specific ATR risk cap the stop machine enforces (2026-07-22 fix - the fallback used to be able to briefly exceed the ATR ceiling)"},
            {"name": "take_profit", "status": "REAL", "description": "R-multiple target (entry + risk_per_share * r_multiple, ATR-based) once risk_per_share is known; falls back to the flat config pct on the first cycle"},
            {"name": "earnings_approaching", "status": "REAL", "description": "days_to_earnings <= config's days_before (default 2)"},
            {"name": "vix_spike", "status": "REAL", "description": "VIX >= config's threshold (default 28)"},
        ],
    },
    "exit_score": {
        "engine": "rules/exit_scorer.py - unified 6-bucket weighted Exit Score (0-100), mirrors the "
                  "buy engine's design on purpose (deployment-review finding: every soft exit signal "
                  "used to have equal authority, so RSI alone could force an exit even with trend/"
                  "breadth/volume all still bullish). Runs in Loop B (engine/position_management.py) "
                  "for every open position, every cycle - NOT gated behind hard exits; it's evaluated "
                  "regardless, feeding priority 3 of the exit hierarchy (see position_management.py's "
                  "module docstring: risk control > thesis-broken proxy > this score's action tier > "
                  "profit management).",
        "action_tiers": [
            {"range": "0-25", "action": "HOLD", "partial_exit": "0%"},
            {"range": "26-45", "action": "MONITOR", "partial_exit": "0%"},
            {"range": "46-65", "action": "TIGHTEN_STOP", "partial_exit": "0% (stop machine tightens instead)"},
            {"range": "66-80", "action": "REDUCE_POSITION", "partial_exit": "50%"},
            {"range": "81-100", "action": "EXIT", "partial_exit": "100%"},
        ],
        "buckets": [
            {
                "name": "TREND_DETERIORATION", "weight_pct": 25, "max_points": 44,
                "rules": [
                    {"name": "below_sma20", "points": 6, "status": "REAL", "description": "Price below the 20-day SMA"},
                    {"name": "below_sma50", "points": 10, "status": "REAL", "description": "Price below the 50-day SMA"},
                    {"name": "ema9_lt_ema21", "points": 6, "status": "REAL", "description": "9-day EMA below 21-day EMA"},
                    {"name": "weekly_trend_broken", "points": 8, "status": "REAL", "description": "Consumes the exact same weekly_above_sma20/weekly_above_sma50 fields as the buy side's weekly_trend_aligned (engine/ticker_data_adapter.py) - TRUE weekly resample (5-bar trading-week closes) as of 2026-07-15b, one shared computation for entry and exit so the two can't disagree on what 'weekly trend' means. Falls back to the old daily-proxy approximation only when there isn't enough weekly history yet (<20 weeks for SMA20, <50 for SMA50) - same fallback, same label, as the buy side. Corrected 2026-07-21 (external review): this catalog entry still said PROXY/'approximated from daily data' after the 2026-07-15b true-weekly-resample fix - it was a stale catalog transcription, not a stale rule; the live rule was already unified."},
                    {"name": "adx_weakening", "points": 6, "status": "REAL", "description": "ADX has dropped >=2 points since last cycle's reading - a true delta, not just a low level"},
                    {"name": "below_avwap_earnings", "points": 8, "status": "REAL_APPROXIMATE", "description": "REAL as of 2026-07-16 - same avwap_earnings field, same REAL_APPROXIMATE anchor-precision caveat, as the buy side's above_avwap_earnings (FMP's real last-earnings-report date, see engine/ticker_analyzer.py's _calc_earnings_avwap()). Not duplicate authority with rules/hard_vetoes.py's BELOW_AVWAP veto - that veto only blocks NEW buys; this scores EXISTING open positions in Loop B."},
                ],
            },
            {
                "name": "MOMENTUM_WEAKNESS", "weight_pct": 20, "max_points": 35,
                "rules": [
                    {"name": "macd_bearish_crossover", "points": 12, "status": "REAL", "description": "MACD line crossed below signal line"},
                    {"name": "macd_hist_negative", "points": 8, "status": "REAL", "description": "MACD histogram < 0"},
                    {"name": "stoch_rollover", "points": 8, "status": "REAL", "description": "Stochastic %K was >=80 last cycle and has since fallen - a real rollover, not just a high level"},
                    {"name": "rsi_overbought", "points": 7, "status": "PROXY", "description": "RSI >= 70 - a proxy for bearish price/RSI divergence, which would need a multi-bar pivot comparison this codebase doesn't do yet"},
                ],
            },
            {
                "name": "VOLUME_DISTRIBUTION", "weight_pct": 20, "max_points": 32,
                "rules": [
                    {"name": "obv_falling", "points": 10, "status": "REAL", "description": "On-balance volume trending down"},
                    {"name": "cmf_negative", "points": 8, "status": "REAL", "description": "Chaikin Money Flow < 0"},
                    {"name": "distribution_day", "points": 8, "status": "REAL", "description": "A down day (change_pct < 0) on >=1.5x average volume - the classic institutional-selling tell"},
                    {"name": "avwap_swing_low_broken", "points": 6, "status": "PROXY", "description": "Price has broken below the anchored VWAP from the last swing low, which previously acted as support - a proxy for a true AVWAP-rejection candlestick pattern"},
                ],
            },
            {
                "name": "MARKET_CONTEXT", "weight_pct": 15, "max_points": 24,
                "rules": [
                    {"name": "breadth_collapsing", "points": 10, "status": "PROXY", "description": "Sector-ETF A/D ratio < 0.35 or McClellan-style oscillator < -40"},
                    {"name": "regime_deteriorated", "points": 8, "status": "REAL", "description": "Regime has moved to BEAR/CRISIS since this position was entered (compares position.entry_regime to the current regime)"},
                    {"name": "vix_spike", "points": 0, "status": "INFORMATIONAL", "description": "VIX >= 28 - INFORMATIONAL ONLY as of 2026-07-15c (duplicate-authority fix): the hard-exit vix_spike rule in rules/sell_rules.py already owns this condition; scoring it here too would double-count the same regime change. Kept in the rules list for observability, contributes 0 pts, excluded from max_points."},
                    {"name": "sector_losing_leadership", "points": 6, "status": "PROXY", "description": "Sector-vs-SPY relative strength negative on both 1-day and 1-month windows"},
                ],
            },
            {
                "name": "FUNDAMENTAL_RISK", "weight_pct": 10, "max_points": 26,
                "rules": [
                    {"name": "earnings_in_2d", "points": 0, "status": "INFORMATIONAL", "description": "Earnings within 2 days - INFORMATIONAL ONLY as of 2026-07-15c: rules/sell_rules.py's earnings_approaching hard exit already owns this condition. 0 pts, excluded from max_points."},
                    {"name": "earnings_approaching_3to4d", "points": 6, "status": "REAL", "description": "Earnings 3-4 days out - genuinely earlier warning than the hard exit provides (which only fires at <=2 days), not a duplicate"},
                    {"name": "insider_selling_heavy", "points": 8, "status": "REAL", "description": ">=10,000 shares sold by insiders in the last 30 days"},
                    {"name": "news_sentiment_crash", "points": 7, "status": "REAL", "description": "News sentiment score <= 0.25"},
                    {"name": "analyst_downgrade", "points": 5, "status": "REAL", "description": "REAL as of 2026-07-16 - same FMP /stable/grades signal as the buy side's no_recent_downgrade, opposite polarity (a HELD position getting downgraded is exit evidence)."},
                    {"name": "negative_guidance / regulatory_action", "points": 0, "status": "PLACEHOLDER", "description": "No guidance-cut or regulatory-event data source - never fires, same gap as rules/hard_vetoes.py's REG_NEWS veto"},
                ],
            },
            {
                "name": "POSITION_HEALTH", "weight_pct": 10, "max_points": 10,
                "rules": [
                    {"name": "inverse_health", "points": "0-7", "status": "REAL", "description": "(100 - Position Health score) rescaled - engine/position_health.py's own 7-component score (P&L trend, EV, RS trend, volume, AVWAP, breadth, time decay), inverted so weak health raises exit pressure"},
                    {"name": "mae_anomalous / mae_elevated", "points": "0-2", "status": "REAL", "description": "Current drawdown vs. the historical MAE percentile of winning trades in the same setup/regime (engine/mae_mfe_engine.py) - needs >=10 historical winners before it says anything"},
                    {"name": "time_stop", "points": "0-1", "status": "REAL", "description": "No-progress or max-hold-time check (engine/position_management.py._check_time_stop) has triggered"},
                ],
            },
        ],
    },
    "note": "Two tiers only, not three: hard exits (config-driven, single-trigger) and the unified "
            "Exit Score (hardcoded 6 buckets, weighted, graduated). The old separate soft scoring "
            "that used to live in rules/sell_rules.py itself has been removed entirely - it was a "
            "second, disagreeing opinion running alongside Loop B's richer version of the same idea. "
            "See engine/position_management.py's module docstring for the full priority hierarchy "
            "these two tiers sit inside (risk control and a thesis-broken proxy still sit ABOVE the "
            "Exit Score, so an account-level kill switch or a fully-broken thesis can't be outvoted "
            "by a merely-moderate score).",
}


# 2026-07-14: added after Trinath asked to see "all the filtering criteria
# before scoring" on the Strategy page - these are the SCREENER's own
# candidate-selection filters (engine/screener.py), which run BEFORE a
# ticker is even added to the per-cycle watchlist, i.e. one step earlier
# than the hard vetoes above (which run on every ticker, watchlist or
# screener-sourced, right before scoring). Descriptions only - actual live
# threshold VALUES come from config.yaml at request time (see server.py's
# /api/strategy, same "descriptions here, numbers from live config" split
# HARD_VETOES_CATALOG above/SELL_RULES_CATALOG already use).
SCREENER_FILTERS_CATALOG = {
    "engine": "engine/screener.py's run_screener() - only runs when config.yaml's screener.enabled is true. "
              "Three stages, in order, each capable of dropping a raw candidate before it ever reaches a scan slot. "
              "2026-07-15: candidate cap is now RISK-LEVEL-scaled on top of the regime scaling "
              "(CONSERVATIVE x0.6 / MODERATE x0.8 / AGGRESSIVE x1.0 / TURBO x1.25) so pre-selection "
              "behaves like the risk profile it feeds, and DAY mode enforces the same 2M avg-volume "
              "floor in the quality gate that the hard veto applies at scoring time:",
    "stages": [
        {
            "name": "1. Identity / history filter",
            "function": "_pre_filter()",
            "description": "Drops a raw candidate if it's already on the manual watchlist, already an open position, in a post-exit re-entry cooldown, on an active stale/fallback-data streak within the last screener.learning.unhealthy_recheck_cooldown_minutes (config: screener.learning.exclude_unhealthy_tickers/unhealthy_min_consecutive/unhealthy_recheck_cooldown_minutes - the cooldown exists so a ticker that went stale once isn't locked out forever; it gets one more look once the cooldown elapses), or has a proven track record of almost never qualifying once scored (config: screener.learning.exclude_low_quality_tickers/min_track_record/max_qualify_rate_to_exclude/min_stale_block_rate_to_exclude). Self-healing, not a permanent blocklist - a ticker's stats reset once it stops being discovered.",
        },
        {
            "name": "2. Quality gate (price / volume / spread)",
            "function": "_apply_quality_gate()",
            "description": "Added 2026-07-14 after most screener-sourced signals were reaching a scan slot only to be immediately hard-vetoed on price/volume/spread anyway. One lightweight yfinance_get_ticker_info call per surviving candidate, checking the SAME fields+thresholds rules/hard_vetoes.py enforces at scoring time (config: screener.quality_gate.enabled/min_price/max_price/min_avg_volume - spread uses rules/spread_quality.py's mode-aware veto ceiling, same as the live hard veto). Fails open on an MCP error - a fetch hiccup never costs a real candidate its slot.",
        },
        {
            "name": "3. Quota allocation + candidate cap",
            "function": "_allocate_by_quota()",
            "description": "Surviving candidates are grouped by source (rs_gainers/volume_surge/gap_candidates/pre_market_movers/sector_leaders/universe_sweep/alpha_movers/fmp_movers/fq_movers), each with a fixed per-cycle quota (e.g. rs_gainers 4, universe_sweep 4, the rest 2 each) so no single source can crowd out the others - unused quota rolls over to the highest Discovery-Score candidates from any source. Ranked by Discovery Score (see below) and capped at config.yaml's screener.max_candidates - 0 means uncapped (config: screener.max_candidates, screener.dynamic_by_regime scales a positive cap up in BULL / down in BEAR-CRISIS regimes, ignored when uncapped, further scaled by risk level: CONSERVATIVE x0.6 / MODERATE x0.8 / AGGRESSIVE x1.0 / TURBO x1.25). A sector-diversity cap then trims the final shortlist so no single sector supplies more than 30% of it (minimum 3 slots always allowed per sector regardless of the pct cap, so a legitimately sector-driven rally isn't artificially thinned to nothing). 3 exploration slots are reserved for structurally-valid candidates the engine has seen LEAST recently, so the shortlist doesn't just keep re-scanning the same handful of leaders every cycle.",
        },
    ],
    "discovery_score": {
        "description": "Re-ranks every surviving candidate 0-100, computed in engine/screener.py, regardless of which source found it.",
        "components": [
            {"name": "Relative strength vs SPY", "weight_pct": 40, "description": "Blend of 20d/50d/100d return vs SPY over the same windows"},
            {"name": "Trend alignment", "weight_pct": 25, "description": "Price > SMA20 > SMA50 > SMA200 (full stack aligned bullish)"},
            {"name": "Volatility compression", "weight_pct": 20, "description": "TTM squeeze / NR7 / NR4 / inside-day - same compression signals as the buy side's VOLATILITY_EXPANSION bonus bucket, checked here pre-scoring as a discovery signal"},
            {"name": "Today's %change", "weight_pct": 15, "description": "Capped contribution so a single huge-gap-up day can't dominate the ranking on its own"},
        ],
        "persistence_outcome_bonus": "Additive, capped at +/-10pts, only once a ticker has >=5 scored cycles of history (2026-07-14 self-learning pass): 70% weight on real qualify rate (did it actually qualify when scored, not just get discovered again), 30% weight on how strongly it scored even on cycles it didn't qualify. A ticker chronically blocked by stale data (>=50% of its cycles) gets an active -15 penalty rather than a neutral zero - repeatedly failing to produce usable data is itself a signal, not neutral information.",
        "lite_pass": "Screener-sourced candidates (not the hand-curated watchlist) get a cheaper first pass - bars/quote/indicators only, skipping maverick/finviz/scanner/news (the EXTERNAL bucket's real evidence, expensive to fetch at screener volume). A lite candidate scoring within 15 points of its risk profile's NOMINAL base threshold (the plain risk.<level>.buy_score_threshold_pct, before any dynamic-threshold inflation - 2026-07-15 fix, comparing against the fully-inflated live threshold here was starving CONSERVATIVE/MODERATE of any promotions) earns a full re-fetch and rescore with every bucket's real data.",
    },
}


def get_strategy_catalog() -> dict:
    return {
        "buy_rules": BUY_RULES_CATALOG,
        "dynamic_thresholds": DYNAMIC_THRESHOLD_CATALOG,
        "hard_vetoes": HARD_VETOES_CATALOG,
        "sell_rules": SELL_RULES_CATALOG,
        "stop_machine": STOP_MACHINE_CATALOG,
        "screener_filters": SCREENER_FILTERS_CATALOG,
        "data_source_resilience": DATA_SOURCE_RESILIENCE_NOTE,
        # 2026-07-23: added while closing out a full audit of every rule-
        # producing module in the codebase against this catalog (Trinath's
        # "update the strategy page to include every rule ever used in the
        # whole framework" ask) - these eight existed and were fully live in
        # production but had never been transcribed into the Strategy tab:
        "market_gate": MARKET_GATE_CATALOG,
        "trading_modes": TRADING_MODES_CATALOG,
        "account_risk": ACCOUNT_RISK_CATALOG,
        "portfolio_risk": PORTFOLIO_RISK_CATALOG,
        "execution_quality": EXECUTION_QUALITY_CATALOG,
        "position_sizing": POSITION_SIZING_CATALOG,
        "probabilistic_decision": PROBABILISTIC_DECISION_CATALOG,
        "regime_engine": REGIME_ENGINE_CATALOG,
        "rotation": ROTATION_CATALOG,
    }
