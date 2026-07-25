"""7-bucket swing scoring engine (6 weighted decision buckets + 1 additive
volatility bonus). Each bucket has a weight, max points, and a min_pct bar.

QUALIFICATION IS A CONTINUOUS SOFT MULTIPLIER, NOT A HARD GATE (definitive
statement, 2026-07-15 - an external review correctly flagged that this
docstring still described the old "contribution = 0 below the bar" behavior
while the code had moved to the smooth curve): every bucket contributes
(points/max_points) * weight * qual_mult, where qual_mult comes from
_qualification_multiplier()'s anchor table (0% of max -> 0.0, 30% -> 0.35,
50% -> 0.60, 100% -> 1.00, linearly interpolated). There is NO score cliff
anywhere; min_pct feeds only the `qualified` boolean used for display.

ticker_data / market_data are plain dicts from engine/ticker_data_adapter.py.
ADX, CMF, Donchian, AVWAP-swing-low, industry RS (sector-vs-SPY proxy),
earnings AVWAP, no_recent_downgrade, and estimate_raised are all REAL as of
2026-07-16 - see that file for exactly which. Two inputs remain genuine
placeholders: volume-profile POC (no source in this stack gives multi-week
volume-at-price data) and unusual options flow (the real source,
github.com/erikmaday/unusual-whales-mcp, requires a paid API key not
configured here - deliberately not faked with a weaker proxy). See
ticker_data_adapter.py for exactly which. That means two rules here will
never fire yet, not that the scoring logic itself is wrong.

squeeze/NR7/NR4/inside_day (used by the VOLATILITY_EXPANSION bucket) are REAL,
not placeholders - computed from the same daily OHLCV bars as everything else
by engine/ticker_analyzer.py's _calc_volatility_compression.

DIAGNOSTICS NOTE (added after a review flagged the old "TREND 0/71 pts
(needed 50%)" line as ambiguous/misleading): every BucketScore now carries a
full `checklist` - EVERY rule this bucket checks, fired or not, not just the
ones that scored points - via the _BucketBuilder helper below. That's what
lets engine/packet_builder.py show a real "why did this fail" breakdown
(✔/❌ per rule) instead of just a bucket-level pass/fail. See from_score_result()
for the corrected raw/normalized/weight breakdown string, and note on
"why 71, not 55": b1_max=71.0 IS the correct number - 55 was a stale
constant this codebase already identified and fixed (see the comment at
b1_max below) that let a fully-loaded TREND bucket score >100% of its own
weight. Reintroducing 55 as a "normalized max" would resurrect that exact bug.
"""
import datetime
from dataclasses import dataclass, field


@dataclass
class BucketScore:
    name: str
    weight: float       # 0-1, all weights sum to 1
    points: float       # earned
    max_points: float
    min_pct: float      # minimum to qualify (as % of max_points)
    qualified: bool      # points/max_points >= min_pct - REPORTING ONLY now
                          # (which buckets cleared their own bar, shown in the
                          # UI/breakdown); scoring itself uses qual_mult below.
    rules_fired: list = field(default_factory=list)
    qual_mult: float = 1.0  # 0-1 soft-qualification multiplier used in the
                             # final score - see _qualification_multiplier()
    checklist: list = field(default_factory=list)  # EVERY rule checked in this
                             # bucket - {"name","passed","points"} - fired AND
                             # not-fired, for real "why did this fail" diagnostics.


def _qualification_multiplier(pct_of_max: float, min_pct: float) -> float:
    """Fully continuous multiplier — no binary qualification cliff anywhere.

    Maps pct_of_max (0.0–1.0) through a smooth lookup table so every level of
    bucket completion contributes something proportional.  min_pct is retained
    solely for the `qualified` boolean on BucketScore (display/reporting: which
    buckets cleared their bar) and does NOT affect this calculation.

    Anchor table (linear interpolation between points):
      100% → 1.00    90% → 0.95    80% → 0.88    70% → 0.80
       60% → 0.70    50% → 0.60    40% → 0.50    30% → 0.35
        0% → 0.00

    Key design properties:
    • No zero-floor cliff — a bucket at 20% still earns 0.233×, not 0.
    • Above min_pct contributes proportionally (not a sudden jump to 1.0).
    • VOLATILITY_EXPANSION (min_pct=0, always-bonus bucket) gets the same
      proportional treatment as every other bucket; 0 pts → 0.0 multiplier,
      which means it contributes 0 and doesn't distort the total.

    Replaces the previous linear ramp (0 at min_pct×0.6 → 1.0 at min_pct)
    which had a hidden cliff at 60% of min_pct and gave full credit the
    instant min_pct was crossed. (2026-07-15)
    """
    _ANCHORS = [
        (1.00, 1.00), (0.90, 0.95), (0.80, 0.88), (0.70, 0.80),
        (0.60, 0.70), (0.50, 0.60), (0.40, 0.50), (0.30, 0.35), (0.00, 0.00),
    ]
    pct = max(0.0, min(1.0, pct_of_max))
    for i in range(len(_ANCHORS) - 1):
        hi_pct, hi_mult = _ANCHORS[i]
        lo_pct, lo_mult = _ANCHORS[i + 1]
        if pct >= lo_pct:
            span = hi_pct - lo_pct
            t = (pct - lo_pct) / span if span else 1.0
            return lo_mult + t * (hi_mult - lo_mult)
    return 0.0


def _family_breakdown(members: list, cap: float) -> list:
    """Per-rule raw/capped-share/clipped breakdown for a correlated-evidence
    family (2026-07-21, external review - "log raw points, capped points,
    and cap-clipped points for every rule, especially trend/MACD/volume-OBV,
    so the caps' real-world bite can be measured before any further
    tightening"). `members` is the list of (rule_label, raw_points) for
    rules that actually FIRED this call (build it the same way the family's
    existing _*_pts sum already does - see each cap site below). Clipping is
    attributed proportionally across fired members when the family total
    exceeds `cap` - this is diagnostic-only telemetry; it does NOT change
    the scoring math, which still applies one aggregate subtraction to the
    bucket's total at the call site, exactly as before this pass."""
    raw_total = sum(pts for _, pts in members)
    ratio = (min(raw_total, cap) / raw_total) if raw_total else 1.0
    return [
        {"name": name, "raw": pts, "capped_share": round(pts * ratio, 2),
         "clipped": round(pts * (1 - ratio), 2)}
        for name, pts in members
    ]


class _BucketBuilder:
    """Accumulates a bucket's points AND a full checklist of every rule
    checked (fired or not) - single source of truth so the checklist can
    never drift from the actual scoring math (they're computed by the exact
    same .check() call). Point values/conditions below are UNCHANGED from
    before this pass; this only adds observability around the same math."""

    def __init__(self, name: str):
        self.name = name
        self.pts = 0.0
        self.rules_fired = []
        self.checklist = []

    def check(self, name: str, condition, points: float) -> bool:
        fired = bool(condition)
        if fired:
            self.pts += points
            self.rules_fired.append(name)
        self.checklist.append({"name": name, "passed": fired, "points": points})
        return fired

    def bucket_score(self, weight: float, max_points: float, min_pct: float) -> BucketScore:
        pct = (self.pts / max_points) if max_points else 0.0
        return BucketScore(
            self.name, weight, self.pts, max_points, min_pct, pct >= min_pct, self.rules_fired,
            qual_mult=_qualification_multiplier(pct, min_pct), checklist=self.checklist,
        )


# Config-driven bucket weights/qualification thresholds - Priority 3 from the
# deployment review ("config-driven weights instead of hardcoded literals").
# These constants are now ONLY the fallback defaults, used when config.yaml
# has no `weights.<profile>` section (or is missing a specific key) so any
# existing config.yaml keeps behaving identically. The live values are read
# from config every call via _bucket_weight()/_bucket_min_pct() below - this
# is what gives learning/bayesian_updater.py's propose_update() a REAL
# current_weight to target instead of a hardcoded literal nothing could ever
# apply (see that module and engine/learning_loop.py's docstring).
# 2026-07-15 (zero-trades audit): VOLATILITY_EXPANSION's weight is now 0.0
# and its 7% was redistributed across the six real decision buckets. The old
# wiring made it a weighted bucket like any other, which meant every stock
# NOT currently in a squeeze (i.e. most good setups, by this bucket's own
# design) had its maximum possible composite capped at 93% - a permanent 7%
# drag that contradicted the bucket's stated "confirm, don't gate" intent.
# It now contributes as a pure ADDITIVE BONUS on top of the 6-bucket
# composite (up to +VOL_EXP_BONUS_MAX_PTS points) - see the FINAL SCORE
# section below.
VOL_EXP_BONUS_MAX_PTS = 4.0

_DEFAULT_BUCKET_WEIGHTS = {
    "swing_buy": {
        "TREND": 0.225, "MOMENTUM": 0.205, "VOLUME_PA": 0.15, "EXTERNAL": 0.16,
        "SENTIMENT_MACRO": 0.15, "MARKET_BREADTH": 0.11, "VOLATILITY_EXPANSION": 0.0,
    },
    # ETF profile fallback defaults - see config.yaml's weights.swing_buy_etf
    # comment for the rationale (less company-specific EXTERNAL/SENTIMENT_MACRO,
    # more TREND/MOMENTUM/MARKET_BREADTH).
    "swing_buy_etf": {
        "TREND": 0.29, "MOMENTUM": 0.215, "VOLUME_PA": 0.15, "EXTERNAL": 0.065,
        "SENTIMENT_MACRO": 0.085, "MARKET_BREADTH": 0.195, "VOLATILITY_EXPANSION": 0.0,
    },
    # DAY profile fallback defaults (2026-07-22, full DAY/SWING/HYBRID
    # separation) - see config.yaml's weights.swing_buy_day comment for the
    # full rationale. Only selected when mode=="day" - see _weights_key()
    # below. These are Python-side fallbacks (used if config.yaml is
    # missing the key), same convention as swing_buy/swing_buy_etf above.
    "swing_buy_day": {
        "TREND": 0.12, "MOMENTUM": 0.25, "VOLUME_PA": 0.27, "EXTERNAL": 0.08,
        "SENTIMENT_MACRO": 0.10, "MARKET_BREADTH": 0.18, "VOLATILITY_EXPANSION": 0.0,
    },
    "swing_buy_etf_day": {
        "TREND": 0.15, "MOMENTUM": 0.26, "VOLUME_PA": 0.27, "EXTERNAL": 0.04,
        "SENTIMENT_MACRO": 0.06, "MARKET_BREADTH": 0.22, "VOLATILITY_EXPANSION": 0.0,
    },
}
_DEFAULT_BUCKET_MIN_PCT = {
    "swing_buy": {
        "TREND": 0.50, "MOMENTUM": 0.40, "VOLUME_PA": 0.40, "EXTERNAL": 0.40,
        "SENTIMENT_MACRO": 0.30, "MARKET_BREADTH": 0.35, "VOLATILITY_EXPANSION": 0.0,
    },
    "swing_buy_etf": {
        "TREND": 0.50, "MOMENTUM": 0.40, "VOLUME_PA": 0.40, "EXTERNAL": 0.15,
        "SENTIMENT_MACRO": 0.20, "MARKET_BREADTH": 0.35, "VOLATILITY_EXPANSION": 0.0,
    },
    # DAY min_pct deliberately identical to swing/etf - see
    # config.yaml's weights.swing_buy_day comment.
    "swing_buy_day": {
        "TREND": 0.50, "MOMENTUM": 0.40, "VOLUME_PA": 0.40, "EXTERNAL": 0.40,
        "SENTIMENT_MACRO": 0.30, "MARKET_BREADTH": 0.35, "VOLATILITY_EXPANSION": 0.0,
    },
    "swing_buy_etf_day": {
        "TREND": 0.50, "MOMENTUM": 0.40, "VOLUME_PA": 0.40, "EXTERNAL": 0.15,
        "SENTIMENT_MACRO": 0.20, "MARKET_BREADTH": 0.35, "VOLATILITY_EXPANSION": 0.0,
    },
}


def _bucket_weight(config: dict, weights_key: str, name: str) -> float:
    weights_cfg = ((config or {}).get("weights", {}) or {}).get(weights_key, {}).get("bucket_weights", {}) or {}
    return float(weights_cfg.get(name, _DEFAULT_BUCKET_WEIGHTS[weights_key][name]))


def _bucket_min_pct(config: dict, weights_key: str, name: str) -> float:
    min_pct_cfg = ((config or {}).get("weights", {}) or {}).get(weights_key, {}).get("bucket_min_pct", {}) or {}
    return float(min_pct_cfg.get(name, _DEFAULT_BUCKET_MIN_PCT[weights_key][name]))


def _detect_asset_class(ticker_data: dict, ticker: str, config: dict) -> str:
    """Returns "ETF" or "STOCK". Primary signal: ticker_data['quote_type'] -
    REAL, straight from yfinance's own info.quoteType (see
    engine/ticker_analyzer.py's _parse_yfinance), not inferred/guessed.
    config.yaml's asset_profiles.etf_tickers is a manual override list for
    when quote_type is missing/stale (e.g. a ticker that hasn't had a
    successful yfinance fetch yet)."""
    overrides = {t.upper() for t in ((config or {}).get("asset_profiles", {}) or {}).get("etf_tickers", []) or []}
    if ticker and ticker.upper() in overrides:
        return "ETF"
    if (ticker_data.get("quote_type") or "").upper() == "ETF":
        return "ETF"
    return "STOCK"


@dataclass
class SwingScoreResult:
    final_score_pct: float      # 0-100
    buckets: list
    rules_fired: list
    threshold: float            # from dynamic_thresholds
    passed: bool
    breakdown: str
    ev_result: dict = None       # real pattern-DB EV lookup - see get() below.
                                 # None if db/ticker weren't passed in, or if
                                 # the pattern DB didn't have enough matches
                                 # yet (ev_result["ev"] is None in that case
                                 # too - see engine/ev_engine.py). Consumed by
                                 # engine/position_sizing.py so the sizing
                                 # engine doesn't have to redo this lookup.
    execution_quality: object = None  # rules/execution_quality.py's ExecutionQualityResult,
                                       # or None if that module raised - consumed by
                                       # engine/position_sizing.py as another size multiplier.
    asset_class: str = "STOCK"   # "STOCK" or "ETF" - see _detect_asset_class()
    threshold_result: dict = None  # rules/dynamic_thresholds.py's calc_threshold() FULL dict (base/stress/
                                    # cal/tp adjustments, confidence, breakdown string) - `threshold` above is
                                    # just its final_threshold float. Persisted so analytics/decision_replay.py
                                    # can show the full "why 67%, not 55%" math for a past signal, not just
                                    # the final number.
    probabilistic_decision: dict = None  # rules/probabilistic_decision.py's decide() output (2026-07-15) -
                                          # the REAL basis for `passed` below whenever the pattern database has
                                          # enough similar closed trades ("probabilistic" mode); falls back to
                                          # the pre-existing score-vs-threshold cliff ("score_fallback" mode)
                                          # when it doesn't. See that module's docstring for the full rationale.


def score(ticker_data: dict, market_data: dict, regime, config: dict, mode: str = "swing",
          db=None, ticker: str = None) -> SwingScoreResult:
    """
    ticker_data: from engine/ticker_data_adapter.py's ticker_to_dict()
    market_data: from engine/ticker_data_adapter.py's market_to_dict()
    regime: RegimeState from engine/regime_engine.py
    mode: "swing", "hybrid", or "day". "day" (2026-07-22, full DAY/SWING/
    HYBRID separation) now selects: the spread-quality penalty tiers (see
    rules/spread_quality.py), a DAY-specific bucket-weight profile
    (weights_key above), and a DAY-specific base threshold
    (buy_score_threshold_day_pct). "hybrid" is treated identically to
    "swing" for all of scoring - see pre_selection_criteria_and_trading_modes.md
    Section 3 for why (HYBRID's DAY/SWING split happens AFTER scoring, via
    scheduler.py's _classify_hybrid_leg, and only affects exits/sizing/EOD
    handling for the resulting position - never entry scoring). Spreads wide
    enough to hard-veto never
    reach this function at all (rules/hard_vetoes.py catches those first).
    db, ticker: OPTIONAL. If both are given, this looks up a REAL Expected
    Value estimate from the pattern database (engine/ev_engine.py) using the
    buckets/rules this exact call just computed, and feeds it into the
    dynamic-threshold EV bonus below. Before this pass, ev_pct always silently
    defaulted to 0 here - get_ev_for_signal() existed but was never actually
    called from anywhere, so the EV bonus in rules/dynamic_thresholds.py was
    always taking its neutral "0<=ev_pct<1.0" branch (a flat +5%-harder
    threshold) regardless of a ticker's real historical edge. If db/ticker are
    omitted (e.g. older callers, tests), behavior is unchanged - ev_pct stays 0.
    """
    risk_cfg = config["risk"][config["risk_level"]]

    # Asset-class profile (STOCK vs ETF) - selects which weights.* config
    # section to read. See _detect_asset_class() docstring.
    asset_class = _detect_asset_class(ticker_data, ticker, config)
    # DAY-mode reweighting (2026-07-22, full DAY/SWING/HYBRID separation -
    # see config.yaml's weights.swing_buy_day comment and
    # pre_selection_criteria_and_trading_modes.md Section 3): pure DAY-mode
    # candidates score against a different bucket-weight profile that shifts
    # weight from TREND/EXTERNAL toward VOLUME_PA/MOMENTUM/MARKET_BREADTH.
    # HYBRID deliberately does NOT take this branch - it continues scoring
    # through swing_buy/swing_buy_etf exactly as before (mode=="hybrid" !=
    # "day"), matching the documented "HYBRID scores with swing evidence,
    # classifies legs afterward" design; only a config.yaml trading.mode of
    # literal DAY opts into day-specific scoring.
    is_day_mode = (mode or "swing").lower() == "day"
    weights_key = (
        ("swing_buy_etf_day" if asset_class == "ETF" else "swing_buy_day") if is_day_mode
        else ("swing_buy_etf" if asset_class == "ETF" else "swing_buy")
    )

    # Effective bucket weights = Priority-3 config-driven base weights for
    # the selected profile, optionally regime-adjusted (Priority 6,
    # engine/regime_weight_adaptation.py - disabled by default, gated on
    # live trade-history count; returns the base weights unchanged until
    # both conditions are met). Computed ONCE per score() call rather than
    # per-bucket.
    from engine.regime_weight_adaptation import get_effective_bucket_weights
    _effective_weights = get_effective_bucket_weights(config, regime, db, engine=weights_key)

    def _bw(name: str) -> float:
        return _effective_weights.get(name, _DEFAULT_BUCKET_WEIGHTS[weights_key][name])

    def _mp(name: str) -> float:
        return _bucket_min_pct(config, weights_key, name)

    # -- BUCKET 1: TREND --
    price = ticker_data.get("price", 0)
    sma20 = ticker_data.get("sma_20", 0)
    sma50 = ticker_data.get("sma_50", 0)
    sma200 = ticker_data.get("sma_200", 0)
    ema9 = ticker_data.get("ema_9", 0)
    ema21 = ticker_data.get("ema_21", 0)
    adx = ticker_data.get("adx", 0)
    weekly_above_sma20 = ticker_data.get("weekly_above_sma20", False)
    weekly_above_sma50 = ticker_data.get("weekly_above_sma50", False)
    avwap_earnings = ticker_data.get("avwap_earnings", 0)
    donchian_20d_high = ticker_data.get("donchian_20d_high", 0)

    b1 = _BucketBuilder("TREND")
    b1.check("above_sma200", sma200 and price > sma200, 15)
    b1.check("above_sma50", sma50 and price > sma50, 10)
    b1.check("above_sma20", sma20 and price > sma20, 8)
    b1.check("ema9_gt_ema21", ema9 and ema21 and ema9 > ema21, 7)
    # ADX measures trend STRENGTH, not direction (2026-07-15, external
    # review) - a stock in a strong DOWNTREND also has ADX > 25. Require
    # the bullish directional side (+DI > -DI) to confirm.
    plus_di = ticker_data.get("plus_di", 0)
    minus_di = ticker_data.get("minus_di", 0)
    b1.check("adx_trending_bullish", adx > 25 and plus_di > minus_di, 10)
    b1.check("donchian_20d_break", donchian_20d_high and price >= donchian_20d_high, 5)
    # above_avwap_earnings: REAL as of 2026-07-16 - avwap_earnings is now a
    # genuine anchored VWAP from FMP's real last-earnings-report date (see
    # engine/ticker_analyzer.py's _calc_earnings_avwap()), restored to its
    # original 8-pt value now that the rule can actually fire.
    b1.check("above_avwap_earnings", avwap_earnings and price > avwap_earnings, 8)
    b1.check("weekly_trend_aligned", weekly_above_sma20 and weekly_above_sma50, 8)
    # rs_vs_spy_1m: REAL per-ticker relative strength vs SPY over ~1 month
    # (ticker's own 21-bar return minus SPY's, computed from data already
    # fetched - see ticker_analyzer.py's return_1m_pct and market_breadth's
    # get_spy_return_1m). Leaders outperform the index BEFORE breakouts -
    # one of the highest-signal "emerging leader" indicators. (2026-07-15)
    rs_vs_spy_1m = ticker_data.get("rs_vs_spy_1m", 0.0)
    b1.check(f"rs_vs_spy_1m_{rs_vs_spy_1m:+.1f}%", rs_vs_spy_1m > 0, 6)

    # CORRELATED-EVIDENCE CAP (2026-07-15, external review): the five
    # "trend structure" rules (above SMA20/50/200, EMA9>EMA21, weekly
    # alignment - 48 raw pts) all measure the same latent condition. Any
    # stock in a broad market rally fires most of them at once, so one
    # underlying fact was being counted five times. Capped at 38 so full
    # structure alignment still dominates the bucket but can't crowd out
    # the genuinely independent signals (ADX strength, Donchian breakout,
    # RS vs SPY).
    _structure_members = [
        (name, pts) for name, pts in (("above_sma200", 15), ("above_sma50", 10), ("above_sma20", 8),
                                      ("ema9_gt_ema21", 7), ("weekly_trend_aligned", 8))
        if name in b1.rules_fired
    ]
    _structure_pts = sum(pts for _, pts in _structure_members)
    if _structure_pts > 38:
        b1.pts -= (_structure_pts - 38)
        b1.rules_fired.append(f"trend_structure_capped_-{_structure_pts - 38:.0f}")

    # b1_max = capped structure (38) + adx 10 + donchian 5 + rs_vs_spy 6 +
    # above_avwap_earnings 8 = 67. (2026-07-16: above_avwap_earnings went
    # REAL, so it's back in the achievable sum - see above.)
    b1_max = 67.0
    b1_score = b1.bucket_score(_bw("TREND"), b1_max, _mp("TREND"))

    # -- BUCKET 2: MOMENTUM --
    rsi = ticker_data.get("rsi", 50)
    macd = ticker_data.get("macd", 0)
    macd_signal = ticker_data.get("macd_signal", 0)
    macd_hist = ticker_data.get("macd_hist", 0)
    stoch_k = ticker_data.get("stoch_k", 50)

    rsi_thresh = risk_cfg.get("rsi_oversold_threshold", 40)
    stoch_thresh = risk_cfg.get("stochastic_threshold", 30)

    b2 = _BucketBuilder("MOMENTUM")
    # 2026-07-15 (zero-trades audit): the old bucket ONLY rewarded oversold
    # readings (rsi < thresh: 12pts, stoch < thresh: 5pts) - pure dip-buying
    # rules that a strong bull-market leader structurally can NEVER satisfy
    # (leaders hold RSI 55-70 for weeks). 17 of this bucket's 40 points were
    # unreachable for exactly the stocks a bull market rewards, which is a
    # big part of why the best signal ever recorded scored only 58.6%.
    # Now DUAL-PATH (mutually exclusive tiers, like the NR7/NR4 pattern):
    #   - pullback path: oversold reading (full points - a dip in an uptrend
    #     is still the highest-EV swing entry)
    #   - strength path: healthy momentum zone (partial points - RSI 45-70 /
    #     stoch 40-85 is where sustained uptrends live; >70/>85 stays 0,
    #     chasing an overbought move is not rewarded)
    # Trend-integrity condition on the pullback path (2026-07-15, external
    # review): "in a broken trend, oversold is often a falling knife, not a
    # pullback." Full pullback points require price still above SMA50; an
    # oversold reading below a broken 50-day earns only a token 4 - it's a
    # mean-reversion gamble, not a pullback-in-uptrend.
    trend_intact = bool(sma50 and price > sma50)
    if rsi < rsi_thresh and trend_intact:
        rsi_pts, rsi_label = 12, f"rsi_pullback_{rsi:.0f}"
    elif rsi < rsi_thresh:
        rsi_pts, rsi_label = 4, f"rsi_oversold_broken_trend_{rsi:.0f}"
    elif rsi <= 70:
        rsi_pts, rsi_label = 8, f"rsi_momentum_zone_{rsi:.0f}"
    else:
        rsi_pts, rsi_label = 0, f"rsi_overbought_{rsi:.0f}"
    b2.check(rsi_label, rsi_pts > 0, rsi_pts)
    # STAGED CREDIT (2026-07-21, external review - "a 12-point cross can
    # still dominate the bucket immediately, especially under TURBO's 50%
    # base threshold... do not allow a bullish cross plus a positive
    # histogram to receive nearly full MACD-family credit on the first bar.
    # Make persistence the route to the larger share of the cap."): cross
    # cut 12 -> 8 (initial turning-point evidence, not the dominant signal)
    # so cross+hist on day one is 8+6=14/18 (78% of the family cap), not
    # 18/18 (100%, the old value) - a fresh crossover with no track record
    # yet no longer reads as "fully confirmed MACD" on its own. Reaching the
    # full 18-pt cap now requires the momentum_persistent tier below too
    # (8+6+5 at 10+ days), so persistence - not the crossover instant - is
    # what earns the top of the cap.
    b2.check("macd_bullish_cross", macd and macd_signal and macd > macd_signal, 8)
    b2.check("macd_hist_positive", macd_hist and macd_hist > 0, 6)
    if stoch_k < stoch_thresh:
        st_pts, st_label = 5, f"stoch_pullback_{stoch_k:.0f}"
    elif stoch_k <= 85:
        st_pts, st_label = 3, f"stoch_momentum_zone_{stoch_k:.0f}"
    else:
        st_pts, st_label = 0, f"stoch_overbought_{stoch_k:.0f}"
    b2.check(st_label, st_pts > 0, st_pts)

    # Momentum persistence - REAL (see engine/ticker_analyzer.py's
    # _consecutive_positive_days). "MACD positive for 12 days" is meaningfully
    # more convicted than "positive for 1 day", even though macd_hist_positive
    # above treats them identically - this rewards the streak on top of that.
    # Mutually-exclusive tiers (10+ days / 5-9 days / <5 days), one checklist
    # entry either way so the diagnostic always shows the actual streak length.
    macd_positive_days = ticker_data.get("macd_positive_days", 0)
    if macd_positive_days >= 10:
        mpd_pts = 5
    elif macd_positive_days >= 5:
        mpd_pts = 3
    else:
        mpd_pts = 0
    b2.check(f"momentum_persistent_{macd_positive_days}d", mpd_pts > 0, mpd_pts)

    # CORRELATED-EVIDENCE CAP (2026-07-15, external review): MACD cross,
    # MACD histogram positive, and MACD-histogram persistence (19 raw pts as
    # of the 2026-07-21 staged-credit cut above, was 23) are three views of
    # the same MACD state. Capped at 18 so a fully confirmed, PERSISTENT
    # MACD still leads the bucket without triple-counting - see the staged
    # credit comment above for why day-one credit no longer reaches the cap
    # on its own.
    _macd_members = [
        (name, pts) for name, pts in (("macd_bullish_cross", 8), ("macd_hist_positive", 6))
        if name in b2.rules_fired
    ]
    if mpd_pts > 0:
        _macd_members.append((f"momentum_persistent_{macd_positive_days}d", mpd_pts))
    _macd_pts = sum(pts for _, pts in _macd_members)
    if _macd_pts > 18:
        b2.pts -= (_macd_pts - 18)
        b2.rules_fired.append(f"macd_family_capped_-{_macd_pts - 18:.0f}")

    # b2_max = rsi 12 + stoch 5 + capped MACD family 18 = 35.
    # (squeeze/NR7/NR4/inside_day moved to VOLATILITY_EXPANSION.)
    b2_max = 35.0
    b2_score = b2.bucket_score(_bw("MOMENTUM"), b2_max, _mp("MOMENTUM"))

    # -- BUCKET 3: VOLUME & PRICE ACTION --
    rvol_quality = ticker_data.get("rvol_quality_score", 50)
    obv_rising = ticker_data.get("obv_rising", False)
    cmf = ticker_data.get("cmf", 0)
    bb_pct = ticker_data.get("bb_pct", 0.5)
    vwap = ticker_data.get("vwap", 0)
    avwap_swing_low = ticker_data.get("avwap_swing_low", 0)
    poc_price = ticker_data.get("poc_price", 0)

    b3 = _BucketBuilder("VOLUME_PA")
    if rvol_quality >= 80:
        rvol_pts, rvol_label = 10, f"rvol_excellent_{rvol_quality:.0f}"
    elif rvol_quality >= 60:
        rvol_pts, rvol_label = 6, f"rvol_good_{rvol_quality:.0f}"
    elif rvol_quality >= 40:
        rvol_pts, rvol_label = 3, f"rvol_moderate_{rvol_quality:.0f}"
    else:
        rvol_pts, rvol_label = 0, f"rvol_weak_{rvol_quality:.0f}"
    b3.check(rvol_label, rvol_pts > 0, rvol_pts)
    b3.check("obv_rising", obv_rising, 5)
    b3.check(f"cmf_positive_{cmf:.2f}", cmf > 0, 6)
    # 2026-07-15 (zero-trades audit): dual-path Bollinger position, same
    # rationale as MOMENTUM's dual-path RSI - the old rule ONLY paid for a
    # lower-band touch (bb_pct < 0.20), which a trending bull leader riding
    # the upper band structurally never gives you.
    if bb_pct < 0.20:
        bb_pts, bb_label = 8, f"bb_lower_touch_{bb_pct:.2f}"
    elif bb_pct >= 0.60 and sma20 and price > sma20:
        bb_pts, bb_label = 5, f"bb_upper_ride_{bb_pct:.2f}"
    else:
        bb_pts, bb_label = 0, f"bb_mid_band_{bb_pct:.2f}"
    b3.check(bb_label, bb_pts > 0, bb_pts)
    # above_vwap timeframe semantics (2026-07-21, external review; corrected
    # 2026-07-21 round 2 - the first pass labeled DAY mode "above_vwap_session"
    # even though it computes the exact same number as SWING/HYBRID, which
    # is itself a mislabel the review caught: "for DAY mode, do not label or
    # score it as session VWAP until the pipeline carries intraday
    # timestamps and resets accumulation at the session boundary... either
    # set it to informational/no-score, or explicitly call it
    # above_vwap_multisession5d there too"): td.vwap (see
    # ticker_analyzer.py's _calc_indicators) is a CUMULATIVE VWAP over the
    # full intraday-bars window this cycle's provider returned - ~5 trading
    # days of 5-min bars for both the Alpaca and yfinance paths
    # (AlpacaProvider.get_intraday_bars / yfinance_mcp's "period":"5d"), NOT
    # a single session that resets at market open, REGARDLESS of mode. That
    # already matches this review's own suggested SWING design ("use a
    # rolling multi-session VWAP") - a 5-day VWAP isn't wrong, it just
    # answers a different question than session VWAP, so it's kept scoring
    # in every mode, labeled identically and honestly as
    # above_vwap_multisession5d everywhere rather than claiming a
    # session-VWAP precision this pipeline doesn't have for ANY mode yet. A
    # TRUE single-session VWAP for DAY mode would need per-bar intraday
    # timestamps threaded through _calc_indicators the same way
    # _calc_earnings_avwap's date-index fix did for daily bars - a separate,
    # larger lift, deferred (not silently faked).
    vwap_fresh = "VWAP" not in (ticker_data.get("stale_indicators") or [])
    b3.check("above_vwap_multisession5d", bool(vwap and price > vwap and vwap_fresh), 4)
    # ATR-normalized band (2026-07-15, external review): a fixed % band is
    # not equally meaningful across volatility regimes - 1.5% is huge for a
    # utility and noise for a small-cap. "Near AVWAP" now means within
    # 0.5×ATR, floored at 0.5% (quiet names) and capped at 2.5% (wild ones).
    _avwap_band = 0.0
    atr_val = ticker_data.get("atr", 0)
    if price:
        _avwap_band = min(0.025 * price, max(0.005 * price, 0.5 * (atr_val or 0)))
    # Distance instrumentation (2026-07-15e, review round 4): the fired
    # label carries the actual normalized distance (ATR units) so the
    # deferred 0.25-0.75xATR grid calibration can be run later from logged
    # signals alone, without re-instrumenting the code.
    _avwap_dist_atr = (abs(price - avwap_swing_low) / atr_val
                       if (avwap_swing_low and price and atr_val) else 99.0)
    b3.check(f"avwap_swing_low_bounce_{_avwap_dist_atr:.2f}atr",
             avwap_swing_low and price and abs(price - avwap_swing_low) <= _avwap_band, 6)
    # near_poc_support: PLACEHOLDER - poc_price is always 0.0 (no volume-
    # profile data source wired; see ticker_data_adapter.py). A rule that can
    # never fire must not inflate b3_max (same treatment as TREND's
    # above_avwap_earnings). Restore 4 pts when a real POC source exists.
    b3.check("near_poc_support", poc_price and price and abs(price - poc_price) / price < 0.005, 0)

    # ---- ACCUMULATION signals (2026-07-15, all REAL, computed from the same
    # daily OHLCV bars as everything else - see ticker_analyzer.py). These
    # detect institutional accumulation BEFORE the breakout, which is the
    # single highest-value addition from the zero-trades audit: the old
    # bucket measured today's snapshot; these measure the 2-10 week
    # transition from "ignored" to "accumulated". ----
    obv_new_high_20d = ticker_data.get("obv_new_high_20d", False)
    obv_divergence = ticker_data.get("obv_divergence", False)
    dollar_vol_ratio = ticker_data.get("dollar_vol_ratio_20_50", 1.0)
    accumulation_days = ticker_data.get("accumulation_days_10", 0)
    # OBV making a 20d high while price hasn't = quiet accumulation
    # (divergence, strongest form: 6). OBV 20d high WITH price high = healthy
    # confirmation (4). Mutually exclusive tiers.
    if obv_divergence:
        obv_hi_pts, obv_hi_label = 6, "obv_divergence_accumulation"
    elif obv_new_high_20d:
        obv_hi_pts, obv_hi_label = 4, "obv_new_high_20d"
    else:
        obv_hi_pts, obv_hi_label = 0, "obv_no_new_high"
    b3.check(obv_hi_label, obv_hi_pts > 0, obv_hi_pts)
    # 20d avg dollar volume running >=15% above the 50d avg = liquidity
    # improvement / institutional interest building.
    b3.check(f"dollar_vol_expanding_{dollar_vol_ratio:.2f}x", dollar_vol_ratio >= 1.15, 5)
    # >=5 of the last 10 sessions closed up on above-average volume =
    # consecutive accumulation days, more informative than one volume spike.
    b3.check(f"accumulation_days_{accumulation_days}/10", accumulation_days >= 5, 5)

    # OBV-SPECIFIC SUBGROUP CAP (2026-07-21, external review round 2 -
    # "obv_rising, obv_divergence_accumulation, and obv_new_high_20d [are]
    # still closely related... add an internal subgroup before the broad
    # volume cap"): these three all read the same underlying OBV series
    # (obv_hi_label is one of the latter two, mutually exclusive with each
    # other but not with obv_rising) - nested INSIDE the broader 20-pt
    # accumulation-family cap below so OBV alone can't consume most of that
    # budget before CMF/dollar-volume/accumulation-days get a look in.
    _obv_rising_pts = 5 if "obv_rising" in b3.rules_fired else 0
    _obv_members = [(name, pts) for name, pts in
                     (("obv_rising", _obv_rising_pts), (obv_hi_label, obv_hi_pts)) if pts > 0]
    _OBV_FAMILY_CAP = 9
    _obv_family_raw = sum(pts for _, pts in _obv_members)
    if _obv_family_raw > _OBV_FAMILY_CAP:
        b3.pts -= (_obv_family_raw - _OBV_FAMILY_CAP)
        b3.rules_fired.append(f"obv_family_capped_-{_obv_family_raw - _OBV_FAMILY_CAP:.0f}")
    _obv_breakdown = _family_breakdown(_obv_members, _OBV_FAMILY_CAP)

    # CORRELATED-EVIDENCE CAP (2026-07-15, external review): obv_rising,
    # OBV new-high/divergence, CMF, dollar-volume expansion, and
    # accumulation days (27 raw pts) all read "money flowing in" from the
    # same volume tape. Capped at 20 so strong accumulation still carries
    # the bucket without quintuple-counting one condition. Uses the
    # OBV-CAPPED per-rule shares from _obv_breakdown above (not OBV's raw
    # points) as the OBV contribution to this outer sum, so a name that
    # already tripped the inner OBV cap isn't then double-counted at its
    # pre-cap value here too.
    _accum_members = [(e["name"], e["capped_share"]) for e in _obv_breakdown]
    if any(r.startswith("cmf_positive") for r in b3.rules_fired):
        _accum_members.append((f"cmf_positive_{cmf:.2f}", 6))
    if any(r.startswith("dollar_vol_expanding") for r in b3.rules_fired):
        _accum_members.append((f"dollar_vol_expanding_{dollar_vol_ratio:.2f}x", 5))
    if any(r.startswith("accumulation_days") for r in b3.rules_fired):
        _accum_members.append((f"accumulation_days_{accumulation_days}/10", 5))
    _accum_pts = sum(pts for _, pts in _accum_members)
    if _accum_pts > 20:
        b3.pts -= (_accum_pts - 20)
        b3.rules_fired.append(f"accumulation_family_capped_-{_accum_pts - 20:.0f}")

    # b3_max = rvol 10 + bb 8 + vwap 4 + avwap 6 + poc 0 + capped
    # accumulation family 20 = 48. (2026-07-21: nested OBV-family cap at 9
    # doesn't change this - 20 is still reachable via OBV-capped-9 + cmf 6 +
    # dollar_vol 5 + accum_days 5 = 25, still clipped to 20 by the outer cap.)
    b3_max = 48.0
    b3_score = b3.bucket_score(_bw("VOLUME_PA"), b3_max, _mp("VOLUME_PA"))

    # -- BUCKET 4: EXTERNAL SIGNALS -- (company-specific; carries less weight
    # for ETFs - see _DEFAULT_BUCKET_WEIGHTS["swing_buy_etf"] above)
    maverick_bullish = ticker_data.get("maverick_bullish", False)
    finviz_rating = ticker_data.get("finviz_technical_rating", "") or ""
    analyst_consensus = ticker_data.get("analyst_consensus", "")
    industry_rs_positive = ticker_data.get("industry_rs_positive", False)
    unusual_options = ticker_data.get("unusual_options_bullish", None)
    estimate_raised = ticker_data.get("analyst_estimate_raised", False)
    no_recent_downgrade = ticker_data.get("no_recent_downgrade", True)

    # technical_rating (2026-07-22, Trinath: "source finviz's data elsewhere" -
    # see mcp_clients/stock_scanner.py's module docstring): finviz's own
    # rating is a SYNTHESIZED heuristic (derived from SMA/RSI this engine
    # already scores directly in the TREND/MOMENTUM buckets - see
    # finviz_mcp.py's _derive_technical_rating() docstring), so a genuine,
    # independent third-party opinion is worth more when one is available.
    # tradingview_rating (stock-scanner MCP's tradingview_technicals, a real
    # TradingView gauge) is preferred; finviz's derived rating is the
    # fallback when tradingview didn't respond this cycle. Label reflects
    # whichever source actually supplied it, so a logged signal never claims
    # a stronger/different source than the one that actually fired - same
    # honesty convention as sector_rs_1m_positive_proxy's rename below.
    tradingview_rating = ticker_data.get("tradingview_rating", "N/A") or "N/A"
    if tradingview_rating not in ("", "N/A"):
        technical_source, technical_label_rating = "tradingview", tradingview_rating
    else:
        technical_source, technical_label_rating = "finviz", (finviz_rating or "none")

    b4 = _BucketBuilder("EXTERNAL")
    b4.check("maverick_bullish", maverick_bullish, 12)
    b4.check(f"technical_rating_{technical_source}_{technical_label_rating}",
             "buy" in technical_label_rating.lower() or "strong buy" in technical_label_rating.lower(), 10)
    # analyst_consensus 8pts -> 5pts and industry_rs_positive 10pts -> 13pts:
    # reweighted per deployment-review Priority 8 - analyst ratings are a
    # lagging, third-party opinion, while industry_rs_positive is now a REAL
    # calculated signal (sector-vs-SPY relative strength, same data as
    # SENTIMENT_MACRO's sector_rs_1m - see ticker_data_adapter.py), not the
    # placeholder it used to be. (Stale "still 61" note removed 2026-07-21 -
    # b4_max has moved since this comment was written; see the real
    # computation a few lines below, currently 48.)
    b4.check(f"analyst_{analyst_consensus or 'none'}",
             analyst_consensus in ["Buy", "Strong Buy", "Overweight"], 5)
    # Checklist label renamed industry_rs_positive -> sector_rs_1m_positive_proxy
    # (2026-07-21, external review - "industry_rs_positive is still described
    # as sector ETF versus SPY, not true industry or peer-group strength.
    # Rename it to something operationally honest"). The underlying
    # ticker_data KEY stays "industry_rs_positive" (ticker_data_adapter.py's
    # data contract, unchanged) - only the label shown in every logged
    # signal/checklist/catalog entry changes, so it stops reading as a
    # stronger claim than the data actually supports. Replace with true
    # industry/peer relative strength once a classification + peer-universe
    # source exists; until then this 13-pt rule is a sector-ETF proxy, not
    # literal industry RS - see rules_catalog.py's matching rename.
    b4.check("sector_rs_1m_positive_proxy", industry_rs_positive, 13)
    # unusual_options_bullish (2026-07-22, Trinath: "remove any capped API if
    # possible and see if it can be sourced elsewhere"): github.com/
    # erikmaday/unusual-whales-mcp (the originally-evaluated source) still
    # needs a paid UW_API_KEY with no free tier, so that specific source
    # stays out. stock-scanner MCP's options_unusual_activity tool is a
    # different, already-connected, real options-flow source - see
    # ticker_analyzer.py's _parse_scanner() for the (unverified-shape,
    # best-effort) wiring. Weighted at 6 (same tier as estimate_raised) -
    # deliberately modest given the response shape isn't yet confirmed
    # against a live call; revisit once production data shows how often it
    # actually fires. unusual_options is None (not False) when the tool
    # didn't respond this cycle, so a data outage still can't manufacture
    # bullish evidence - same convention as no_recent_downgrade below.
    b4.check("unusual_options_bullish", unusual_options is True, 6)
    # estimate_raised: REAL as of 2026-07-16 - FMP's /stable/analyst-estimates
    # consensus EPS, diffed against a stored prior reading (see
    # storage/database.py's estimate_snapshots table / ticker_analyzer.py).
    # Returns None (no credit, same as False here) until 30 days of snapshot
    # history exist for a given ticker - expect this to sit at 0 pts for
    # every ticker for its first month after deploy, then start firing for
    # real once genuine before/after comparisons exist.
    b4.check("estimate_raised", estimate_raised, 6)
    # no_recent_downgrade: REAL as of 2026-07-16 - FMP's /stable/grades gives
    # actual dated rating-change events (verified live: caught a real
    # KeyBanc AAPL downgrade). ticker_data_adapter.py now sets this True only
    # when FMP explicitly confirms no downgrade in the last 30 days - a data
    # outage (FMP down/unconfigured) resolves to False (no credit), never to
    # the old unconditional default-True the 2026-07-15 review flagged.
    # POINTS CUT 5 -> 2 (2026-07-21, external review): "no downgrade in 30
    # days" is mostly absence-of-a-negative-event, not affirmative bullish
    # evidence - crediting it at the same weight as a genuine positive signal
    # (maverick_bullish, industry_rs_positive) risked recreating the old
    # over-crediting problem the 2026-07-15 default-True fix was meant to
    # solve, just with real data behind it this time. Kept as a small
    # positive (not zero/neutral-only) since a confirmed clean 30-day check
    # IS mildly informative; revisit upward only once outcome data shows it
    # earns more. An actual downgrade still costs the position via the
    # symmetric exit-side rules/exit_scorer.py analyst_downgrade penalty,
    # which is untouched by this cut.
    b4.check("no_recent_downgrade", no_recent_downgrade, 2)

    # b4_max = maverick 12 + technical_rating 10 + analyst 5 + industry_rs 13 +
    # estimate_raised 6 + no_recent_downgrade 2 + unusual_options 6 = 54.
    # (2026-07-22: +6 for unusual_options_bullish going from a 0-pt
    # placeholder to a real, weighted rule - see above.)
    b4_max = 54.0
    b4_score = b4.bucket_score(_bw("EXTERNAL"), b4_max, _mp("EXTERNAL"))

    # -- BUCKET 5: SENTIMENT & MACRO -- (insider_net_buying/short_float are
    # stock-specific and carry less weight for ETFs)
    news_multiplier = ticker_data.get("news_multiplier", 1.0)
    sector_rs_1d = ticker_data.get("sector_rs_1d", 0)
    sector_rs_1m = ticker_data.get("sector_rs_1m", 0)
    fg_score = market_data.get("fg_score", 50)
    insider_net_buying = ticker_data.get("insider_net_buying", False)
    short_float = ticker_data.get("short_float_pct", 0)
    yield_spread = market_data.get("yield_spread_2s10s", 0)

    b5 = _BucketBuilder("SENTIMENT_MACRO")
    news_pts = min(8.0, max(0.0, (news_multiplier - 0.7) / (2.5 - 0.7) * 8))
    b5.check(f"news_sentiment_{news_multiplier:.2f}x", news_pts > 0, news_pts)
    b5.check(f"sector_rs_1d_positive_{sector_rs_1d:.1f}%", sector_rs_1d > 0, 8)
    # sector_rs_1m: 0 pts as of 2026-07-15 (external review) - this is the
    # EXACT same sector-vs-SPY figure EXTERNAL's industry_rs_positive
    # already scores at 13 pts (both read market_breadth.get_sector_return's
    # return_1m). One signal, one route into the score. Kept in the
    # checklist for observability; sector_rs_1d stays (different window).
    b5.check(f"sector_rs_1m_positive_{sector_rs_1m:.1f}%", sector_rs_1m > 0, 0)
    # Range widened 35-65 -> 35-75 (2026-07-15): in a healthy bull market
    # F&G routinely sits 65-75 for weeks; treating that as non-optimal
    # penalized exactly the conditions that produce the best swing entries.
    # >75 (true euphoria) still earns nothing.
    b5.check(f"fg_optimal_{fg_score:.0f}", 35 <= fg_score <= 75, 4)
    b5.check("insider_net_buying", insider_net_buying, 6)
    b5.check(f"short_float_ok_{short_float:.0f}%", short_float < 20, 4)
    b5.check("yield_curve_ok", yield_spread > -0.5, 4)

    # b5_max = actual achievable sum (8 news + 8 sector_rs_1d + 0 sector_rs_1m
    # + 4 fg + 6 insider + 4 short + 4 yield = 34). (2026-07-15: sector_rs_1m
    # zeroed - deduplicated with EXTERNAL's industry_rs_positive, see above.)
    b5_max = 34.0
    b5_score = b5.bucket_score(_bw("SENTIMENT_MACRO"), b5_max, _mp("SENTIMENT_MACRO"))

    # -- BUCKET 6: MARKET BREADTH -- (fully meaningful for ETFs too - carries
    # MORE weight in the ETF profile, see config.yaml)
    ad_ratio = market_data.get("ad_ratio", 0.5)
    pct_above_20ema = market_data.get("pct_above_20ema", 50)
    pct_above_50ema = market_data.get("pct_above_50ema", 50)
    nh_nl_ratio = market_data.get("nh_nl_ratio", 1.0)
    mcclellan = market_data.get("mcclellan", 0)
    ad_slope_positive = market_data.get("ad_slope_5d_positive", True)
    spy_ad_aligned = market_data.get("spy_ad_aligned", True)
    breadth_acceleration = market_data.get("breadth_acceleration", 0.0)

    b6 = _BucketBuilder("MARKET_BREADTH")
    # Threshold lowered from 0.55 → 0.50: "majority advancing" is the logical
    # dividing line. 0.55 was too strict — a market where >50% of sectors are
    # rising is genuinely positive breadth, not neutral. Aligns with the
    # _breadth_tier() "good" threshold (ad_ratio >= 0.50) added in the breadth
    # redesign. Effect: positive McClellan + ad_ratio=0.53 now earns 10+8+8=26/68
    # = 38.2% → passes 35% min_pct → full qual_mult → ~3.8 composite pts.
    b6.check(f"ad_ratio_{ad_ratio:.2f}", ad_ratio > 0.50, 10)
    b6.check(f"pct_above_20ema_{pct_above_20ema:.0f}%", pct_above_20ema > 60, 8)
    b6.check(f"pct_above_50ema_{pct_above_50ema:.0f}%", pct_above_50ema > 55, 8)
    b6.check(f"nh_gt_nl_{nh_nl_ratio:.1f}", nh_nl_ratio > 1.0, 8)
    b6.check(f"mcclellan_positive_{mcclellan:.0f}", mcclellan > 0, 8)
    b6.check("ad_line_5d_rising", ad_slope_positive, 8)
    b6.check("spy_ad_aligned", spy_ad_aligned, 10)
    # breadth_acceleration - REAL (engine/market_breadth.py): today's
    # pct_above_20ema minus yesterday's. "55% -> 72%" (expanding
    # participation) is stronger evidence than a static 72% snapshot alone -
    # this rewards the DELTA on top of pct_above_20ema's static level above.
    b6.check(f"breadth_accelerating_+{breadth_acceleration:.1f}pp", breadth_acceleration > 5.0, 8)

    # b6_max = actual sum (10+8+8+8+8+8+10+8=68), corrected from the old 60
    # now that breadth_acceleration has been added.
    b6_max = 68.0
    b6_score = b6.bucket_score(_bw("MARKET_BREADTH"), b6_max, _mp("MARKET_BREADTH"))

    # -- BUCKET 7: VOLATILITY EXPANSION --
    # Added to capture volatility CONTRACTING before it expands - genuinely
    # different information from trend/momentum, which measure the direction
    # and strength of a move already in progress, not whether the market is
    # coiled to make one. See engine/ticker_analyzer.py's
    # _calc_volatility_compression for how these are computed (all REAL, same
    # daily OHLCV bars as everything else, zero extra MCP calls).
    #
    # min_qualify_pct = 0% is deliberate, not an oversight: this bucket is
    # meant to CONFIRM a setup, not GATE one. Most good trend/momentum setups
    # won't be in a squeeze at all, and that's fine - a bucket at 0 points
    # still "qualifies" (0/14 >= 0%) and simply contributes 0 to the score,
    # rather than being flagged as an unqualified/failed bucket the way a
    # weak TREND or MOMENTUM score would be.
    squeeze_active = ticker_data.get("squeeze_active", False)
    is_nr7 = ticker_data.get("is_nr7", False)
    is_nr4 = ticker_data.get("is_nr4", False)
    is_inside_day = ticker_data.get("is_inside_day", False)

    b7 = _BucketBuilder("VOLATILITY_EXPANSION")
    b7.check("ttm_squeeze_firing", squeeze_active, 6)
    if is_nr7:
        nr_pts, nr_label = 6, "nr7_compression"
    elif is_nr4:
        nr_pts, nr_label = 3, "nr4_compression"
    else:
        nr_pts, nr_label = 0, "no_range_compression"
    b7.check(nr_label, nr_pts > 0, nr_pts)
    b7.check("inside_day", is_inside_day, 2)

    b7_max = 14.0
    b7_score = b7.bucket_score(_bw("VOLATILITY_EXPANSION"), b7_max, 0.0)

    # -- FINAL SCORE CALCULATION --
    all_buckets = [b1_score, b2_score, b3_score, b4_score, b5_score, b6_score, b7_score]

    # Soft qualification (see _qualification_multiplier above): a bucket's
    # contribution is (points/max_points) * weight * qual_mult, summed across
    # ALL buckets, not just ones that clear their bar. qual_mult ramps 0->1
    # between 60% and 100% of a bucket's own min_pct, so there's no cliff
    # where one point of noise near the boundary flips a bucket's
    # contribution between "full" and "zero" the way the old
    # `if b.qualified` filter did. `qualified` (the boolean) is kept purely
    # for reporting - which buckets cleared their bar - not for scoring.
    # 2026-07-15: VOLATILITY_EXPANSION is excluded from the weighted sum
    # (its weight is 0.0 now) and applied as a pure additive bonus below -
    # see the note at VOL_EXP_BONUS_MAX_PTS. The six decision buckets'
    # weights sum to 1.0 on their own, so a stock that isn't in a squeeze
    # can now genuinely reach 100%.
    #
    # BUCKET AVAILABILITY (2026-07-15, external review's UNKNOWN != FALSE
    # principle): when EVERY source feeding the EXTERNAL bucket is down
    # (finviz + analyst + maverick all absent - a data outage, not negative
    # evidence), scoring it 0/40 punished every candidate ~16 composite pts
    # for something that says nothing about the stock. Instead: 75% of the
    # unavailable bucket's weight is redistributed pro-rata across the
    # available buckets, and 25% is deliberately left DEAD - missing
    # evidence still costs something (wider uncertainty demands a bit more
    # from the evidence that exists), it just doesn't count as bearish.
    # Recorded in the breakdown + data_coverage so every such score is
    # auditable.
    external_available = bool(ticker_data.get("external_data_available", True))
    # PARTIAL unavailability (2026-07-22, Trinath: "finviz and 2 FMP endpoints
    # cannot be used... fix all the issues"): external_data_available above
    # only trips when EVERY EXTERNAL source is down, which was rare even
    # with finviz's breaker open and FMP's grades/estimates endpoints
    # HTTP-402 dead for a full day - one surviving fallback (e.g. yfinance's
    # recommendationKey) kept the whole bucket looking "available", so those
    # 2-3 genuinely-dark rules were silently scored as if their absence were
    # bearish evidence rather than a data gap. external_unavailable_points
    # (engine/ticker_data_adapter.py) tracks the REAL point-weight of
    # specifically-confirmed-unavailable rules (tri-state None fields only,
    # never a measured False) out of EXTERNAL's own max - this is that
    # fraction, 1.0 when the whole bucket is dark (unchanged from before)
    # down to 0.0 when everything responded.
    external_bucket_max = float(ticker_data.get("external_bucket_max_points", 54) or 54)
    external_unavail_pts = float(ticker_data.get("external_unavailable_points", 0) or 0)
    external_unavail_fraction = (
        1.0 if not external_available
        else (min(1.0, external_unavail_pts / external_bucket_max) if external_bucket_max else 0.0)
    )
    # MARKET_BREADTH and SENTIMENT_MACRO unavailability (2026-07-24, Stage 1
    # backtest zero-trades follow-up): these two never had ANY "unavailable"
    # treatment - engine/backtest_engine.py's historical replay feeds them
    # fixed NEUTRAL placeholders every single day (no point-in-time-safe
    # source exists there for real breadth/news/insider/short-float
    # history), but nothing flagged that, so the neutral defaults were
    # silently scored as measured evidence - the exact problem EXTERNAL's
    # redistribution above already solves for a different bucket. All-or-
    # nothing (no per-rule tri-state bookkeeping like
    # external_unavailable_points exists for these two yet - a finer
    # partial-fraction version can be built later the same way EXTERNAL's
    # was, if it proves worth the added plumbing).
    #
    # MARKET_BREADTH reuses market_data["breadth_stale"] - already set
    # correctly in BOTH live (engine/market_breadth.py's calculate(),
    # is_fallback=True on a real outage) and the Stage 1 backtest
    # (engine/backtest_engine.py's build_market_data_asof, hardcoded True).
    # This flag existed before this change but was only ever read by
    # rules/hard_vetoes.py's STALE_DATA_CIRCUIT_BREAKER veto, never by
    # scoring - reading it here is additive, not a new data dependency.
    market_breadth_available = not bool(market_data.get("breadth_stale", False))
    market_breadth_unavail_fraction = 0.0 if market_breadth_available else 1.0
    # SENTIMENT_MACRO is a genuinely NEW flag, read with default=True - every
    # existing live caller that never sets "sentiment_macro_data_available"
    # gets exactly the old behavior (fraction 0.0, zero change). Only
    # engine/backtest_engine.py sets this False today.
    sentiment_macro_available = bool(ticker_data.get("sentiment_macro_data_available", True))
    sentiment_macro_unavail_fraction = 0.0 if sentiment_macro_available else 1.0

    decision_buckets = [b for b in all_buckets if b.name != "VOLATILITY_EXPANSION"]
    _unavail_fraction_by_bucket = {
        "EXTERNAL": external_unavail_fraction,
        "SENTIMENT_MACRO": sentiment_macro_unavail_fraction,
        "MARKET_BREADTH": market_breadth_unavail_fraction,
    }
    # Backward-compat membership list (challenger-shadow weight math and the
    # human-readable unavail_note below still key off bucket NAME, not the
    # fraction) - a bucket appears here on ANY confirmed-unavailable share,
    # partial or total; the actual redistribution math uses the fraction,
    # not this list, so a partial gap still gets proportionally less relief
    # than a total one despite both appearing here.
    unavailable = [name for name, frac in _unavail_fraction_by_bucket.items() if frac > 0]
    # Per-bucket unavailable WEIGHT (2026-07-24: now three buckets can carry
    # a nonzero fraction, not just EXTERNAL - see above).
    unavail_weight = {
        b.name: b.weight * _unavail_fraction_by_bucket.get(b.name, 0.0)
        for b in decision_buckets
    }
    w_unavail = sum(unavail_weight.values())
    w_avail = sum(b.weight for b in decision_buckets) - w_unavail
    scale_uncapped = 1.0 + (0.75 * w_unavail / w_avail) if (w_unavail and w_avail) else 1.0
    # GUARDRAIL (2026-07-21, external review - "cap the final effective
    # weight of any bucket. Otherwise, simultaneous EXTERNAL and another
    # outage could unintentionally make TREND or MOMENTUM dominate the
    # entire score"): scale is applied uniformly to every available bucket,
    # so capping scale itself caps every bucket's post-redistribution weight
    # at the same multiple of its configured weight - "no active bucket may
    # exceed 1.25x its configured weight after redistribution." Originally
    # only EXTERNAL could carry unavailable weight (w_unavail at most ~16%
    # of the stock profile), so this rarely bound in live production - a
    # forward guard, not a live behavior change, until 2026-07-24 added
    # SENTIMENT_MACRO/MARKET_BREADTH to the same treatment above. In the
    # Stage 1 backtest specifically, all three ARE simultaneously
    # unavailable every cycle (w_unavail ~42% of the stock profile), so this
    # guardrail now genuinely binds there (scale caps at 1.25x instead of
    # the ~1.54x uncapped pro-rata would otherwise give) - exactly the
    # scenario this comment originally flagged as a forward guard. Any
    # amount the cap prevents from being redistributed joins the deliberate
    # 25% dead weight below (still not scored, still not treated as
    # bearish) - recorded in data_coverage so it's auditable.
    scale = min(scale_uncapped, 1.25)
    # Per-bucket effective weights (2026-07-21, external review round 2 -
    # "record effective weights for every decision"): the scalar `scale`
    # above tells you THAT redistribution happened and by how much overall,
    # but not what it did to each individual bucket's actual share of the
    # composite. This is that breakdown - configured weight vs. effective
    # (post-redistribution) weight per bucket, so "did EXTERNAL's outage
    # push TREND or MOMENTUM's real influence past its configured 22.5%/
    # 20.5%" is answerable directly from a logged signal instead of having
    # to recompute it from `scale` by hand.
    effective_weights = {
        b.name: {
            "configured_weight": round(b.weight, 4),
            "effective_weight": round((b.weight - unavail_weight.get(b.name, 0.0)) * scale, 4),
        }
        for b in decision_buckets
    }

    # Effective bucket_pct for the composite (2026-07-22 fix - found via
    # simulation while validating the promotion-catch-22 change above): a
    # naive (b.points / b.max_points) here double-penalizes a PARTIALLY dark
    # EXTERNAL bucket. The confirmed-unavailable rules already contribute 0
    # to b.points (correct - they're simply not counted), but b.max_points
    # still counts them in the denominator as "achievable points this
    # candidate failed to earn" - so a ticker missing 3 data points that
    # would likely have scored well gets marked down TWICE: once by the
    # deflated points/max ratio, and again by this section's own weight cut.
    # Simulation proved this: a ticker with finviz/estimate/downgrade
    # UNAVAILABLE scored LOWER than the identical ticker with those same 3
    # rules explicitly MEASURED NEGATIVE - the opposite of "UNKNOWN !=
    # FALSE". Fix: exclude the confirmed-dark points from the denominator
    # too, so the ratio reflects performance on only the rules that were
    # actually measurable (100% if every measurable rule passed, not 66.7%
    # penalized for 3 rules that were never evaluated at all). Only applies
    # to EXTERNAL (the only bucket with a PARTIAL, i.e. 0 < fraction < 1,
    # unavailable state today - SENTIMENT_MACRO/MARKET_BREADTH above are
    # all-or-nothing, so they never need this adjustment) and only when the
    # fraction is <1.0 (full outage keeps its existing, separately-tested
    # 75/25 treatment - the whole bucket's own points are irrelevant there
    # since its entire weight share is zeroed regardless of what leftover
    # values happen to be in ticker_data).
    def _effective_bucket_pct(b):
        if b.name == "EXTERNAL" and 0 < external_unavail_fraction < 1.0:
            measured_max = max(b.max_points - external_unavail_pts, 1e-9)
            return min(1.0, b.points / measured_max)
        return (b.points / b.max_points) if b.max_points else 0.0

    weighted_sum = sum(
        _effective_bucket_pct(b) * ((b.weight - unavail_weight.get(b.name, 0.0)) * scale) * b.qual_mult
        for b in decision_buckets
    )
    # NOTE (2026-07-22, resolved): temporary debug logging lived here while
    # tracking down why production packets showed "Score: 0.0%" for the
    # large majority of tickers despite visibly nonzero bucket breakdowns.
    # Root cause found and fixed in scheduler.py, NOT here - weighted_sum
    # itself was fine the whole time (confirmed via this section's own debug
    # log against real production data: e.g. FIX genuinely computed
    # weighted_sum=0.3068). The bug was that scheduler.py's veto handling
    # built buy_result via from_veto() (which hardcodes score=0/pct_score=0)
    # and, for research-scorable vetoes, only patched buy_result.rules_passed
    # with the real research score's rules - never buy_result.score/
    # pct_score themselves - so the packet's headline always showed the
    # hardcoded 0 while its OWN bucket table (built from a different, correct
    # score_result object) showed the real score underneath. See
    # scheduler.py's _evaluate_ticker(), the RESEARCH-MODE SCORING block.
    # Baseline (no-redistribution, no measured-max adjustment) sum, kept for
    # outage-impact telemetry: "did the redistribution itself change the
    # decision band?" (review round 4 - watch for outages becoming a hidden
    # buy-regime). Deliberately uses the NAIVE ratio (not _effective_bucket_pct)
    # since this baseline's whole purpose is representing what the score
    # would have been with NONE of today's outage handling applied.
    _baseline_sum = sum(
        (b.points / b.max_points) * b.weight * b.qual_mult
        for b in decision_buckets
    )
    final_pct = weighted_sum * 100
    if b7_score.max_points:
        vol_exp_bonus = (b7_score.points / b7_score.max_points) * VOL_EXP_BONUS_MAX_PTS
        if vol_exp_bonus > 0:
            final_pct = min(100.0, final_pct + vol_exp_bonus)

    all_rules = [r for b in all_buckets for r in b.rules_fired]

    # Spread-quality penalty - graded, non-veto tiers only (the "veto" tier
    # never reaches this function; rules/hard_vetoes.py rejects it before
    # scoring runs at all). See rules/spread_quality.py for the full tiered
    # scale and rationale.
    from rules.spread_quality import evaluate as evaluate_spread
    spread_result = evaluate_spread(ticker_data, mode=mode)
    if spread_result.score_penalty_pct:
        final_pct = max(0.0, final_pct - spread_result.score_penalty_pct)
        all_rules.append(f"spread_penalty_{spread_result.tier}_-{spread_result.score_penalty_pct:.0f}pts")

    # Execution Quality Score (rules/execution_quality.py) - a SEPARATE,
    # smaller, additive adjustment on top of the spread_penalty above. Spread
    # is already scored; this adds the genuinely NEW information (dollar
    # volume, slippage estimate, liquidity consistency) the deployment
    # review flagged as missing ("make execution quality part of the
    # decision rather than a simple veto"). Uses trading.trade_size_usd as
    # the planned-trade-size input for the slippage estimate, since the
    # real per-ticker suggested size (engine/position_sizing.py) is only
    # computed AFTER should_buy is known.
    execution_quality_result = None
    try:
        from rules.execution_quality import evaluate as evaluate_execution_quality
        planned_amount = config.get("trading", {}).get("trade_size_usd", 100)
        execution_quality_result = evaluate_execution_quality(ticker_data, planned_amount, config, mode=mode)
        if execution_quality_result.score_adjustment_pct:
            final_pct = max(0.0, min(100.0, final_pct + execution_quality_result.score_adjustment_pct))
            all_rules.append(
                f"execution_quality_{execution_quality_result.tier.lower()}_"
                f"{execution_quality_result.score_adjustment_pct:+.1f}pts"
            )
    except Exception:
        execution_quality_result = None

    from rules.dynamic_thresholds import calculate as calc_threshold
    day_of_week = datetime.datetime.now().weekday()

    # Real EV lookup - see the db/ticker docstring note above. Built from
    # fields already extracted above in this same function (rsi, bb_pct,
    # stoch_k, adx, cmf, sector_rs_1d/1m, squeeze_active, unusual_options,
    # bucket points), so this costs one pattern-DB query, zero extra MCP calls.
    ev_pct = 0.0
    ev_result = None
    if db is not None and ticker:
        try:
            from engine.ev_engine import get_ev_for_signal
            signal_features = {
                "vix_raw": market_data.get("vix", 18),
                "fg_score": market_data.get("fg_score", 50),
                "change_pct": ticker_data.get("change_pct", 0.0),
                "volume_ratio": ticker_data.get("volume_ratio", 1.0),
                "rsi14": rsi,
                "bb_pct": bb_pct,
                "stochastic_k": stoch_k,
                "final_score": final_pct,
                "fg_rating": market_data.get("fg_rating", "unknown"),
                "macd_crossover": ticker_data.get("macd_crossover_direction", "unknown"),
                "finviz_rating": finviz_rating or "unknown",
                "analyst_consensus": analyst_consensus or "unknown",
                "insider_direction": ticker_data.get("insider_net_direction", "unknown"),
                "sector": ticker_data.get("sector", "unknown"),
                "setup_type": all_rules[0] if all_rules else "unspecified",
                "day_of_week": datetime.datetime.utcnow().strftime("%A"),
                "session": "regular",
                "bucket1_score": (b1_score.points / b1_score.max_points * 100) if b1_score.max_points else 0.0,
                "bucket2_score": (b2_score.points / b2_score.max_points * 100) if b2_score.max_points else 0.0,
                "bucket3_score": (b3_score.points / b3_score.max_points * 100) if b3_score.max_points else 0.0,
                "bucket4_score": (b4_score.points / b4_score.max_points * 100) if b4_score.max_points else 0.0,
                "bucket5_score": (b5_score.points / b5_score.max_points * 100) if b5_score.max_points else 0.0,
                "bucket6_score": (b6_score.points / b6_score.max_points * 100) if b6_score.max_points else 0.0,
                "regime": regime.dominant_regime if regime else "unknown",
                "bull_pct": regime.bull_pct if regime else 0.0,
                "bear_pct": regime.bear_pct if regime else 0.0,
                "choppy_pct": regime.choppy_pct if regime else 0.0,
                "transition_prob": regime.transition_probability if regime else 0.0,
                "vix_percentile_1y": market_data.get("vix_percentile_1y", 0.0),
                "vix_percentile_3m": market_data.get("vix_percentile_3m", 0.0),
                "gap_pct": ticker_data.get("gap_pct", 0.0),
                "premarket_gap": ticker_data.get("premarket_gap", 0.0),
                "premarket_rvol": ticker_data.get("premarket_rvol", 0.0),
                "adx": adx,
                "cmf": cmf,
                "sector_rs_1d": sector_rs_1d,
                "sector_rs_1m": sector_rs_1m,
                "squeeze_active": squeeze_active,
                "unusual_options": unusual_options,
                "opex_status": market_data.get("opex_status", "normal"),
            }
            ev_result = get_ev_for_signal(
                # 2026-07-22 (EV mode-keying fix): pattern_database rows are
                # only ever recorded under "DAY" or "SWING" (see scheduler.py's
                # record_entry call - a HYBRID leg gets classified into one of
                # those two by _classify_hybrid_leg before it's ever stored,
                # and non-hybrid legs use trading_mode.upper() directly, which
                # is only ever "DAY" or "SWING" in practice). Passing the raw
                # `mode.upper()` here used to send "HYBRID" through for any
                # HYBRID-configured account - a pattern-DB mode value that is
                # NEVER written, so every HYBRID EV lookup silently matched
                # zero rows forever (confidence pinned "insufficient" no
                # matter how much trade history accumulated). Using the same
                # is_day_mode boolean the bucket-weights/threshold above
                # already use keeps this consistent: HYBRID pools with SWING
                # at lookup time (matching its SWING-equivalent scoring
                # treatment pre-entry), DAY gets its own pool.
                db, signal_features, ticker, mode=("DAY" if is_day_mode else "SWING"),
                regime_filter=regime.dominant_regime if regime else None,
                target_gain_pct=(config.get("probabilistic_decision", {}) or {}).get("target_gain_pct", 5.0),
                # DAY-mode EV lookup uses the day stop distance (2026-07-22)
                # so pattern-database matching reflects the tighter stop a
                # DAY position will actually be held against - see
                # engine/stop_state_machine.py's mode-aware max_stop_pct.
                stop_loss_pct=(risk_cfg.get("stop_loss_day_pct", risk_cfg.get("stop_loss_swing_pct", 5.0) / 2)
                               if is_day_mode else risk_cfg.get("stop_loss_swing_pct", 5.0)),
            )
            if ev_result.get("ev") is not None:
                ev_pct = ev_result["ev"]
        except Exception:
            # Pattern DB lookup failing must never block a live buy/sell
            # decision - fall back to the pre-existing neutral behavior.
            ev_pct = 0.0
            ev_result = None

    # DAY-mode base threshold (2026-07-22, full DAY/SWING/HYBRID separation)
    # - see config.yaml's buy_score_threshold_day_pct comment. Falls back to
    # swing's base + 5 if a profile is missing the day-specific key (config
    # drift safety net, same pattern as _DEFAULT_BUCKET_WEIGHTS above) so a
    # partially-edited config.yaml degrades to "close enough", not a
    # KeyError. HYBRID does NOT take this branch (is_day_mode is only True
    # for mode=="day") - it keeps reading buy_score_threshold_pct exactly as
    # before, consistent with weights_key above.
    _base_threshold = (
        risk_cfg.get("buy_score_threshold_day_pct", risk_cfg["buy_score_threshold_pct"] + 5)
        if is_day_mode else risk_cfg["buy_score_threshold_pct"]
    )
    threshold_result = calc_threshold(
        base_threshold=_base_threshold,
        regime=regime,
        vix=market_data.get("vix", 18),
        day_of_week=day_of_week,
        opex_status=market_data.get("opex_status", "normal"),
        ev_pct=ev_pct,
        breadth_data=market_data,  # passes mcclellan/ad_ratio/ad_ratio_suspect through
        # ev_measured: True only when the pattern DB actually returned a real
        # EV from enough similar trades - prevents the cold-start +5 penalty
        # that blocked every signal while the DB was empty (2026-07-15).
        ev_measured=bool(ev_result is not None and ev_result.get("ev") is not None),
        mode=mode,  # DAY pays +3% on the bar; SWING/HYBRID don't (2026-07-15)
        # Calendar adjustments are log-only until proven against real trade
        # outcomes (external review, 2026-07-15) - see dynamic_thresholds.py.
        calendar_enabled=bool((config.get("thresholds", {}) or {}).get("calendar_enabled", False)),
        # quote_freshness_unknown (2026-07-21, external review round 2): no
        # provider supplied a real market timestamp this cycle - see
        # ticker_analyzer.py's quote_age_is_measured. Only docks confidence
        # in DAY mode (calc_threshold's own docstring) - never blocks the
        # trade, just makes an unmeasured DAY-mode quote read as lower
        # confidence than a verified one, instead of identical to it.
        quote_freshness_unknown=not bool(ticker_data.get("quote_age_is_measured", False)),
    )

    # data_coverage (external review, 2026-07-15): a buy decision should be
    # explainable as "N% real data / M% degraded" - not just a score. Rides
    # inside threshold_result so it's persisted in the signals table's
    # threshold_breakdown JSON with zero schema changes.
    _quote_age_measured = bool(ticker_data.get("quote_age_is_measured", False))
    _quote_age_min = ticker_data.get("quote_age_minutes", 0)
    _quote_max_age = 2 if mode == "day" else 30
    quote_freshness_status = (
        "UNKNOWN" if not _quote_age_measured
        else ("MEASURED_STALE" if _quote_age_min > _quote_max_age else "MEASURED_FRESH")
    )
    threshold_result["data_coverage"] = {
        "data_completeness_pct": ticker_data.get("data_completeness_pct", 100.0),
        "stale_indicators": list(ticker_data.get("stale_indicators", [])),
        # quote_freshness_status (2026-07-21, external review round 2) -
        # UNKNOWN (no provider timestamp this cycle - see
        # rules/hard_vetoes.py's STALE_QUOTE veto, which stays silent in
        # this case rather than guessing) vs MEASURED_FRESH/MEASURED_STALE
        # (a real provider timestamp was available and compared against the
        # same 2min/30min DAY/SWING threshold STALE_QUOTE uses). UNKNOWN in
        # DAY mode also docks threshold confidence - see calc_threshold's
        # quote_freshness_unknown param above.
        "quote_freshness_status": quote_freshness_status,
        "quote_age_minutes": _quote_age_min,
        # e.g. {"quote": "alpaca", "daily_bars": "alpaca", "news": "finnhub"};
        # missing keys mean the yfinance fallback served that capability.
        "providers": dict(ticker_data.get("data_sources", {}) or {}),
        # avwap_earnings has both score (TREND) and new-entry-veto authority
        # (2026-07-21, external review) - True means the anchor bar was
        # located via the calendar-day approximation or an unconfirmed
        # bmo/amc guess, not a confirmed real-date match. See
        # ticker_analyzer.py's _calc_earnings_avwap.
        "avwap_earnings_anchor_approximate": bool(
            ticker_data.get("avwap_earnings_anchor_approximate", True)),
        # Full anchor telemetry (2026-07-21, external review round 2) - see
        # ticker_analyzer.py's TickerData.earnings_avwap_anchor_mode field
        # comment for the exact mode values and what each means.
        "avwap_earnings_anchor_mode": ticker_data.get("avwap_earnings_anchor_mode", "unset"),
        "avwap_earnings_anchor_confidence": ticker_data.get("avwap_earnings_anchor_confidence", "none"),
        "avwap_earnings_anchor_date": ticker_data.get("avwap_earnings_anchor_date", ""),
        # estimate_raised warm-up state (2026-07-21, external review) - see
        # storage/database.py's check_and_record_estimate_snapshot()/
        # ticker_data_adapter.py for the full field list.
        "estimate_raised_detail": dict(ticker_data.get("estimate_raised_detail", {}) or {}),
        # Buckets scored as UNAVAILABLE (weight redistributed, not zeroed) -
        # see the BUCKET AVAILABILITY note in the final-score section. Kept
        # as a list (not just the fraction) for backward-compatible UI/
        # learning-pipeline consumers that already filter on this field;
        # 2026-07-22: EXTERNAL appears here on ANY confirmed-unavailable
        # points (external_unavail_fraction > 0), not just a total outage -
        # partial gaps (e.g. only finviz down) are visible via
        # external_unavail_fraction below instead of being invisible until
        # the whole bucket goes dark. 2026-07-24: SENTIMENT_MACRO/
        # MARKET_BREADTH can now appear here too (all-or-nothing - see their
        # fraction fields below).
        "unavailable_buckets": unavailable,
        "external_unavail_fraction": round(external_unavail_fraction, 4),
        "sentiment_macro_unavail_fraction": round(sentiment_macro_unavail_fraction, 4),
        "market_breadth_unavail_fraction": round(market_breadth_unavail_fraction, 4),
        "weight_redistribution_scale": round(scale, 4),
        # Guardrail telemetry (2026-07-21, external review) - see the
        # scale/scale_uncapped comment above. redistribution_scale_capped is
        # True only if the 1.25x guardrail actually clipped something this
        # cycle; uncapped is what pure 75%-pro-rata would have used.
        "weight_redistribution_scale_uncapped": round(scale_uncapped, 4),
        "redistribution_scale_capped": bool(scale_uncapped > scale + 1e-9),
        # Per-bucket configured vs. effective weight (2026-07-21, external
        # review round 2) - see effective_weights' definition above.
        "effective_weights": effective_weights,
        # Explicit state label for EXTERNAL (2026-07-15c, external review;
        # widened 2026-07-22 for partial outages): "outage" (fully dark,
        # weight redistributed), "degraded" (SOME confirmed-unavailable
        # rules but not all - e.g. only finviz or only FMP down), or
        # available_negative/available_positive (fully up, evidence itself
        # is bearish/absent vs. genuinely bullish). Downstream learning
        # filters on "outage"/"degraded"; the other two are clean
        # observations.
        "external_state": (
            "outage" if external_unavail_fraction >= 0.999
            else ("degraded" if external_unavail_fraction > 0
                  else ("available_positive" if b4_score.points > 0 else "available_negative"))
        ),
        # State labels for the two newly-eligible buckets (2026-07-24) - no
        # "degraded" tier for these (all-or-nothing, no partial fraction is
        # possible yet - see the fraction computation above).
        "sentiment_macro_state": (
            "outage" if sentiment_macro_unavail_fraction >= 0.999
            else ("available_positive" if b5_score.points > 0 else "available_negative")
        ),
        "market_breadth_state": (
            "outage" if market_breadth_unavail_fraction >= 0.999
            else ("available_positive" if b6_score.points > 0 else "available_negative")
        ),
        # % of expected EXTERNAL/SENTIMENT feed sources that delivered this
        # cycle + the active risk profile (2026-07-15f, review round 5's
        # "show coverage and which fallback served the decision").
        "external_coverage_pct": round(100.0 * sum(
            1 for v in {
                "maverick": ticker_data.get("maverick_data_present", False),
                "finviz": (ticker_data.get("finviz_technical_rating") or "N/A") not in ("", "N/A"),
                "analyst": (ticker_data.get("analyst_consensus") or "") not in ("", "N/A"),
                "news": ticker_data.get("news_multiplier", 1.6) != 1.6,
            }.values() if v) / 4.0, 0),
        "risk_level": config.get("risk_level", "?"),
    }
    # Latent-factor ledger (2026-07-15, external review): raw vs capped
    # points per correlated-evidence family, logged on every signal so the
    # caps' real-world bite can be measured before any further tightening.
    # "per_rule" (2026-07-21, external review round 2 - "log raw points,
    # capped points, and cap-clipped points for EVERY rule, especially
    # trend, MACD, and volume/OBV subgroups"): the family-level raw/capped
    # above only showed whether the cap fired at all; per_rule breaks that
    # same clip down per fired rule (proportional attribution - see
    # _family_breakdown()'s docstring) so it's possible to tell WHICH rule
    # inside a capped family is getting suppressed most often, not just that
    # the family as a whole hit its ceiling.
    threshold_result["latent_factors"] = {
        "trend_structure": {"raw": _structure_pts, "capped": min(_structure_pts, 38),
                             "per_rule": _family_breakdown(_structure_members, 38)},
        "macd_family": {"raw": _macd_pts, "capped": min(_macd_pts, 18),
                         "per_rule": _family_breakdown(_macd_members, 18)},
        # obv_family (2026-07-21, external review round 2): the NESTED cap
        # inside volume_accumulation below - obv_rising +
        # obv_divergence_accumulation/obv_new_high_20d, capped at
        # _OBV_FAMILY_CAP=9 before the broader 20-pt cap even applies.
        "obv_family": {"raw": _obv_family_raw, "capped": min(_obv_family_raw, _OBV_FAMILY_CAP),
                        "per_rule": _obv_breakdown},
        # volume_accumulation's "raw"/"per_rule" below already reflect the
        # OBV cap having been applied first (see _accum_members' OBV entries
        # above, built from _obv_breakdown's capped_share, not raw points) -
        # this is intentionally NOT the same as "sum of everything's raw
        # points with no inner cap" so it can't double-count what
        # obv_family already clipped.
        "volume_accumulation": {"raw": _accum_pts, "capped": min(_accum_pts, 20),
                                 "per_rule": _family_breakdown(_accum_members, 20)},
        "placeholder_rules_awarding_points": [],  # all default-true/unreachable placeholders are 0 pts as of 2026-07-15
        "external_sources_seen": {
            "maverick": bool(ticker_data.get("maverick_bullish") is True or ticker_data.get("maverick_sentiment_present", False)),
            "finviz": bool((ticker_data.get("finviz_technical_rating") or "") not in ("", "N/A")),
            "analyst": bool((ticker_data.get("analyst_consensus") or "") not in ("", "N/A")),
            "news": bool(ticker_data.get("news_multiplier", 1.6) != 1.6),
        },
        # sector_rs_proxy_co_fire (2026-07-21, external review round 2 -
        # "sector_rs_1m_positive_proxy at 13/48 EXTERNAL points is still the
        # largest external rule... tag and measure it as a cross-bucket
        # latent factor... quantify how often it co-fires with
        # sector_rs_1d_positive, MARKET_BREADTH strength, SPY trend
        # alignment/regime state, rs_vs_spy_1m. If it is nearly always
        # present when those are positive, it may be overweighted despite
        # no exact duplicate rule."): pure measurement, does NOT change the
        # 13-pt weight - logged on every signal so co-fire frequency can be
        # computed from stored signals later, per the review's "measure
        # before reducing" guidance.
        "sector_rs_proxy_co_fire": {
            "fired": bool(industry_rs_positive),
            "co_fired_with": {
                "sector_rs_1d_positive": bool(sector_rs_1d > 0),
                "market_breadth_strong": bool(
                    (b6_score.points / b6_score.max_points) >= 0.5 if b6_score.max_points else False),
                "rs_vs_spy_1m_positive": bool(rs_vs_spy_1m > 0),
                "bullish_regime": bool(regime and getattr(regime, "dominant_regime", "") == "BULL"),
            },
        },
    }

    threshold_passed = final_pct >= threshold_result["final_threshold"]

    # Outage decision-impact telemetry (2026-07-15e): would this signal have
    # landed on the other side of the bar WITHOUT the availability
    # redistribution? All post-bucket adjustments (vol bonus, spread/
    # execution penalties) are additive, so the delta is exact.
    if unavailable:
        _baseline_final = max(0.0, min(100.0, final_pct - (weighted_sum - _baseline_sum) * 100))
        threshold_result["data_coverage"]["outage_changed_decision"] = bool(
            (_baseline_final >= threshold_result["final_threshold"]) != threshold_passed
        )
        threshold_result["data_coverage"]["baseline_score_without_redistribution"] = round(_baseline_final, 1)

    # Challenger shadow scoring (2026-07-15e, review round 4's "simple
    # challenger harness"): if config.yaml defines
    # weights.<profile>_challenger.bucket_weights, the SAME bucket results
    # are re-weighted under the challenger profile and logged - never acted
    # on. Zero extra data fetches or bucket computation; identical caps and
    # qual_mult; same additive adjustments. Gives side-by-side evidence for
    # a future promotion decision without touching live behavior.
    try:
        ch_weights = (((config or {}).get("weights", {}) or {})
                      .get(f"{weights_key}_challenger", {}) or {}).get("bucket_weights")
        if ch_weights:
            # Challenger gets its own redistribution scale (its weight split
            # across unavailable buckets differs from the champion's).
            # 2026-07-22: mirrors the champion's fractional treatment above
            # (a partial EXTERNAL gap gets proportional relief, not all-or-
            # nothing exclusion) so the challenger and champion never
            # disagree merely because one used the old binary model and the
            # other didn't.
            _ch_w = {b.name: float(ch_weights.get(b.name, b.weight)) for b in decision_buckets}
            # 2026-07-24: mirrors the champion's 3-bucket unavailability
            # treatment above (EXTERNAL/SENTIMENT_MACRO/MARKET_BREADTH), not
            # just EXTERNAL, so the challenger and champion never disagree
            # merely because one used the old 1-bucket model and the other
            # didn't.
            _ch_unavail_weight = {
                n: w * _unavail_fraction_by_bucket.get(n, 0.0)
                for n, w in _ch_w.items()
            }
            _ch_unavail = sum(_ch_unavail_weight.values())
            _ch_avail = sum(_ch_w.values()) - _ch_unavail
            # Same 1.25x guardrail as the champion's `scale` above
            # (2026-07-21, external review) - a challenger profile that
            # concentrates more weight into fewer buckets shouldn't be able
            # to bypass the cap the champion is held to.
            ch_scale = min(
                1.0 + (0.75 * _ch_unavail / _ch_avail) if (_ch_unavail and _ch_avail) else 1.0,
                1.25,
            )
            ch_sum = sum(
                _effective_bucket_pct(b) * ((_ch_w[b.name] - _ch_unavail_weight.get(b.name, 0.0)) * ch_scale) * b.qual_mult
                for b in decision_buckets
            )
            _adjustments_delta = final_pct - weighted_sum * 100  # vol bonus + penalties, additive
            ch_final = max(0.0, min(100.0, ch_sum * 100 + _adjustments_delta))
            threshold_result["challenger"] = {
                "profile": f"{weights_key}_challenger",
                "final_score_pct": round(ch_final, 1),
                "would_buy": bool(ch_final >= threshold_result["final_threshold"]),
                "champion_score_pct": round(final_pct, 1),
                "agrees_with_champion": bool(
                    (ch_final >= threshold_result["final_threshold"]) == threshold_passed),
            }
    except Exception:
        pass  # shadow scoring must never affect the live decision

    # THE decision (2026-07-15, replacing the score-vs-threshold cliff per
    # Trinath's explicit "highest priority" ask): whenever the pattern
    # database has enough historically similar closed trades, `passed` is
    # now driven by the REAL probability of success / expected value from
    # engine/ev_engine.py, not just "did the score clear the bar" - a 68%
    # score and a 95% score stop being treated identically. Falls back to
    # the pre-existing threshold_passed boolean, unchanged, when there
    # isn't enough pattern-DB history yet for this setup - see
    # rules/probabilistic_decision.py's docstring for the full rationale
    # and exactly what "enough" means.
    from rules.probabilistic_decision import decide as decide_probabilistically
    prob_decision = decide_probabilistically(
        ev_result, threshold_passed, final_pct, threshold_result["final_threshold"], config,
    )
    passed = prob_decision["should_buy"]

    # TURBO structural-health eligibility gate (2026-07-21, external review -
    # "TURBO's 50-point composite threshold can be reached by moderately
    # positive evidence across a few correlated technical buckets... require
    # at least one structural condition in addition to the composite score").
    # This is deliberately NOT a bucket-level qualification cliff (every
    # bucket still uses the continuous qual_mult curve everywhere else) -
    # it's a single, TURBO-only, additional ELIGIBILITY condition layered on
    # top of whatever decision method (score threshold or probabilistic EV)
    # already said should_buy=True, checked AFTER `passed` above so it
    # can't be bypassed by either decision path. A bullish market regime
    # should lower the bar for healthy pullbacks, not for structurally
    # impaired names - this stops TURBO from buying a falling knife just
    # because several correlated buckets were mildly positive at once.
    turbo_gate_failed = False
    turbo_gate_reason = ""
    is_turbo = str(config.get("risk_level", "")).upper() == "TURBO"
    trend_pct_of_max = (b1_score.points / b1_score.max_points) if b1_score.max_points else 0.0
    trend_ok = trend_pct_of_max >= 0.40
    # UNKNOWN != FALSE discipline applied to this gate's telemetry
    # (2026-07-21, external review round 2 - "if earnings AVWAP is unknown
    # because its data source is unavailable, do not let 'unknown' count as
    # false in the OR condition... apply the same discipline to the
    # TURBO structural-gate telemetry"). The OR logic itself already
    # handles this correctly - an unavailable avwap_earnings (0.0 default)
    # just drops out of the OR rather than counting as a confirmed bearish
    # reading, same as the scoring buckets treat missing data. What was
    # missing was VISIBILITY: `avwap_condition=False` alone can't tell a log
    # reader whether that's because price genuinely closed below a REAL
    # earnings AVWAP, or because there's no earnings AVWAP to check this
    # cycle at all - these are recorded as separate fields below so that
    # distinction survives into the audit trail, not just this function's
    # internal boolean logic.
    avwap_earnings_available = bool(avwap_earnings)
    avwap_earnings_approximate = bool(ticker_data.get("avwap_earnings_anchor_approximate", True))
    sma50_condition = bool(sma50 and price > sma50)
    avwap_condition = bool(avwap_earnings_available and price > avwap_earnings)
    rs_condition = bool(rs_vs_spy_1m > 0)
    structural_ok = sma50_condition or avwap_condition or rs_condition
    gate_conditions = {
        "price_above_sma50": sma50_condition,
        "price_above_earnings_avwap": avwap_condition,
        "earnings_avwap_available": avwap_earnings_available,
        "earnings_avwap_anchor_approximate": avwap_earnings_approximate if avwap_earnings_available else None,
        "rs_vs_spy_1m_positive": rs_condition,
    }
    if is_turbo and passed:
        if not (trend_ok and structural_ok):
            turbo_gate_failed = True
            passed = False
            turbo_gate_reason = (
                f"TURBO structural gate BLOCKED the buy: TREND {trend_pct_of_max*100:.0f}% of its "
                f"own max ({'OK' if trend_ok else 'need >=40%'}), structural condition "
                f"(price>SMA50 / price>earnings AVWAP / 1mo RS>SPY) "
                f"{'held' if structural_ok else 'NOT held'}"
                + ("" if avwap_earnings_available else " (earnings AVWAP unavailable this cycle - "
                                                          "excluded from the OR, not counted against it)")
                + "."
            )
    threshold_result["turbo_structural_gate"] = {
        "applicable": is_turbo,
        "failed": turbo_gate_failed,
        "reason": turbo_gate_reason,
        "trend_pct_of_max": round(trend_pct_of_max, 4),
        "trend_ok": trend_ok,
        "structural_ok": structural_ok,
        "conditions": gate_conditions,
    }

    unqualified = [b.name for b in all_buckets if not b.qualified]
    # 2026-07-24: generalized from a single EXTERNAL-only sentence to loop
    # over however many of the three buckets are unavailable this call (in
    # the Stage 1 backtest that's normally all three at once).
    unavail_note = (
        " DATA: " + "; ".join(
            f"{name} {_unavail_fraction_by_bucket[name]*100:.0f}% unavailable "
            f"({'all sources down' if _unavail_fraction_by_bucket[name] >= 0.999 else 'partial - some sources down'})"
            for name in unavailable
        ) + " - 75% of each unavailable share's weight redistributed, 25% left dead."
        if unavailable else "")
    spread_note = f" Spread: {spread_result.reason}." if spread_result.score_penalty_pct else ""
    asset_note = f" Asset class: {asset_class} (weights profile: {weights_key})." if asset_class == "ETF" else ""
    turbo_note = f" {turbo_gate_reason}" if turbo_gate_reason else ""
    breakdown = (
        f"Score: {final_pct:.1f}% vs threshold {threshold_result['final_threshold']:.0f}% "
        f"({'PASS' if threshold_passed else 'FAIL'} on the score method). "
        f"Unqualified buckets: {unqualified or 'none'}.{unavail_note} "
        f"Threshold: {threshold_result['breakdown']}.{spread_note}{asset_note}{turbo_note} "
        f"Decision ({prob_decision['mode']}): {prob_decision['reason']}"
    )

    return SwingScoreResult(
        final_score_pct=final_pct,
        buckets=all_buckets,
        rules_fired=all_rules,
        threshold=threshold_result["final_threshold"],
        passed=passed,
        breakdown=breakdown,
        ev_result=ev_result,
        execution_quality=execution_quality_result,
        asset_class=asset_class,
        threshold_result=threshold_result,
        probabilistic_decision=prob_decision,
    )


# ---------------------------------------------------------------------------
# Compatibility adapters: scheduler.py/db.log_signal/packet_builder.py were
# all written against rules/buy_rules.py's BuyResult shape (should_buy,
# score/max_score/pct_score, rules_passed/rules_failed, top_signals with
# .name/.weight/.detail). Rather than rewrite those three files, these
# adapters wrap a SwingScoreResult (or a fired VetoResult, or "already
# holding this ticker") into that same shape.
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dataclass, field as _field


@_dataclass
class _RuleLike:
    name: str
    weight: int = 0
    detail: str = ""


@_dataclass
class BuyResultCompat:
    should_buy: bool
    score: float
    max_score: float
    pct_score: float
    rules_passed: list = _field(default_factory=list)
    rules_failed: list = _field(default_factory=list)
    top_signals: list = _field(default_factory=list)


def _bucket_diagnostic_detail(b) -> str:
    """Corrected diagnostic string (see this module's DIAGNOSTICS NOTE) -
    raw points/raw max, % of the bucket's OWN max (not some other bucket's
    max), qualification bar, the soft-qualification multiplier actually
    applied, this bucket's weight in the composite score, and how many of
    the composite score's points this bucket actually contributed. No "55"
    here - b.max_points (71 for TREND) IS the real max; see this module's
    top-of-file note for why."""
    pct_of_max = (b.points / b.max_points * 100) if b.max_points else 0.0
    contribution = (b.points / b.max_points) * b.weight * b.qual_mult * 100 if b.max_points else 0.0
    max_contribution = b.weight * 100
    return (
        f"Raw {b.points:.0f}/{b.max_points:.0f} pts ({pct_of_max:.0f}% of this bucket's own max) | "
        f"needed {b.min_pct*100:.0f}% to fully qualify | qualification multiplier {b.qual_mult:.2f} | "
        f"bucket weight {b.weight*100:.0f}% of composite | "
        f"contributed {contribution:.1f} of {max_contribution:.1f} possible composite pts"
    )


def from_score_result(result: SwingScoreResult) -> BuyResultCompat:
    passed_rules = [_RuleLike(name=r) for r in result.rules_fired]
    failed_buckets = [
        _RuleLike(name=b.name, detail=f"{_bucket_diagnostic_detail(b)} - {result.breakdown}")
        for b in result.buckets if not b.qualified
    ]
    return BuyResultCompat(
        should_buy=result.passed,
        score=result.final_score_pct,
        max_score=100,
        pct_score=result.final_score_pct,
        rules_passed=passed_rules,
        rules_failed=failed_buckets,
        top_signals=passed_rules[:3],
    )


def from_veto(veto) -> BuyResultCompat:
    return BuyResultCompat(
        should_buy=False, score=0, max_score=100, pct_score=0,
        rules_passed=[], top_signals=[],
        rules_failed=[_RuleLike(name=veto.veto_code, detail=veto.reason)],
    )


def already_open() -> BuyResultCompat:
    return BuyResultCompat(
        should_buy=False, score=0, max_score=100, pct_score=0,
        rules_passed=[], top_signals=[],
        rules_failed=[_RuleLike(name="ALREADY_OPEN", detail="Already holding this ticker - not evaluated as a new entry")],
    )
