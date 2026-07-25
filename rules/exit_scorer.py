"""Unified 6-bucket Exit Score (0-100) for open positions - mirrors the
buy-side design in rules/swing_buy_rules.py on purpose (deployment-review
finding: "our buy engine is already institutional quality, your sell engine
is still closer to a traditional retail rule engine" - specifically, every
soft/momentum-style exit signal used to have EQUAL authority, so a single
RSI reading could force an exit even with trend/breadth/volume all still
bullish).

This REPLACES three previously-separate, sometimes-disagreeing scores with
one:
  1. rules/sell_rules.py used to carry its own separate soft "Exit Score"
     (rsi_overbought/macd_bearish_crossover/etc, its own weights) - removed
     from that file; sell_rules.py now only owns the HARD, single-trigger
     exits (stop/target/earnings/VIX - see that file's docstring for why
     those correctly stay single-trigger).
  2. engine/position_health.py used to take THIS module's score as an input
     component ("lower exit score = healthier") - that was circular once
     Position Health became one of THIS module's own input buckets. Fixed:
     position_health.py is now self-contained and computed first; its result
     feeds into this module's POSITION_HEALTH bucket, one direction only.
  3. engine/position_management.py's old _evaluate_priority() had separate
     override branches keyed to mae_eval.status and health.score alongside
     the exit score - those are now folded INTO this module's
     POSITION_HEALTH bucket instead of competing with it. See that file for
     the resulting single, explicit priority hierarchy: risk control (kill
     switch/daily loss) > thesis-broken proxy (this score >=90, matching
     stop_state_machine.py's THESIS_BROKEN stage) > this score's action tier
     > stop advancement / hold.

Buckets (mirrors rules/swing_buy_rules.py's BucketScore style/math -
contribution = (points/max_points) * weight, summed * 100, no soft-
qualification gate needed on the exit side since there's no "bucket didn't
qualify" concept here, every bucket always counts):
  TREND_DETERIORATION   25%  - has the trend actually broken?
  MOMENTUM_WEAKNESS     20%  - is momentum rolling over, not just "high"?
  VOLUME_DISTRIBUTION   20%  - is this distribution (institutional selling)?
  MARKET_CONTEXT        15%  - did the MARKET change, not just the stock?
  FUNDAMENTAL_RISK      10%  - event/news risk ahead
  POSITION_HEALTH       10%  - this trade's own EV/RS/time-decay/MAE behavior

ticker_data/market_data are plain dicts from engine/ticker_data_adapter.py,
same convention as rules/hard_vetoes.py and rules/swing_buy_rules.py. `health`
comes from engine/position_health.calculate() (call it BEFORE this), and
mae_eval/time_stop come from engine/mae_mfe_engine.evaluate_mae_percentile()
and engine/position_management._check_time_stop() respectively - all three
are inputs, computed by the caller (engine/position_management.py's
run_loop_b), not by this module, to keep this module a pure function over
its inputs like rules/swing_buy_rules.py's score().

HONESTY NOTE, same convention as everywhere else in this codebase: several
rules below are PROXY (a real, calculated approximation, e.g.
"rsi_overbought" standing in for true bearish price/RSI divergence, which
would need a multi-bar pivot search this codebase doesn't do yet) rather than
the literally-named textbook signal. below_avwap_earnings and
analyst_downgrade went REAL 2026-07-16 (same FMP-backed fields as the buy
side's above_avwap_earnings/no_recent_downgrade). negative_guidance and
regulatory_action remain genuine PLACEHOLDERS - no guidance-cut or
regulatory-event data source exists anywhere in this codebase (same gap
rules/hard_vetoes.py's REG_NEWS veto has).
"""
from dataclasses import dataclass, field


@dataclass
class ExitBucketScore:
    name: str
    weight: float       # 0-1, all weights sum to 1
    points: float
    max_points: float
    rules_fired: list = field(default_factory=list)


# Config-driven bucket weights - Priority 3 from the deployment review, same
# pattern as rules/swing_buy_rules.py's _bucket_weight(): fallback defaults
# used when config.yaml has no `weights.exit_score` section, live value read
# from config every call otherwise. Gives a real, live weight for future
# Bayesian/regime-adaptive updates to target on the exit side too.
_DEFAULT_EXIT_BUCKET_WEIGHTS = {
    "TREND_DETERIORATION": 0.25, "MOMENTUM_WEAKNESS": 0.20, "VOLUME_DISTRIBUTION": 0.20,
    "MARKET_CONTEXT": 0.15, "FUNDAMENTAL_RISK": 0.10, "POSITION_HEALTH": 0.10,
}


def _exit_bucket_weight(cfg: dict, name: str, effective_weights: dict = None) -> float:
    if effective_weights:
        return float(effective_weights.get(name, _DEFAULT_EXIT_BUCKET_WEIGHTS[name]))
    weights_cfg = ((cfg or {}).get("weights", {}) or {}).get("exit_score", {}).get("bucket_weights", {}) or {}
    return float(weights_cfg.get(name, _DEFAULT_EXIT_BUCKET_WEIGHTS[name]))


@dataclass
class ExitScoreResult:
    total_score: float          # 0-100
    buckets: list
    reasons: list                # top contributing rule tags, for display
    action: str                  # HOLD | MONITOR | TIGHTEN_STOP | REDUCE_POSITION | EXIT
    partial_exit_pct: float      # 0.0-1.0, fraction of the position to sell now


# Action-tier ladder, keyed directly off total_score - replaces the old
# ad-hoc mix of exit_score/health/mae_eval branches in
# engine/position_management.py's _evaluate_priority() with a single ladder.
# Upper bound is inclusive of each tier's ceiling (0-25 Hold, 26-45 Monitor,
# 46-65 Tighten Stop, 66-80 Reduce, 81-100 Exit).
_ACTION_TIERS = [
    (25.0, "HOLD", 0.0),
    (45.0, "MONITOR", 0.0),
    (65.0, "TIGHTEN_STOP", 0.0),
    (80.0, "REDUCE_POSITION", 0.50),
    (100.01, "EXIT", 1.0),
]


def _action_for_score(score: float) -> tuple:
    for ceiling, action, pct in _ACTION_TIERS:
        if score <= ceiling:
            return action, pct
    return "EXIT", 1.0


def calculate(position: dict, ticker_data: dict, market_data: dict, regime,
              health, mae_eval: dict, time_stop, cfg: dict = None, db=None) -> ExitScoreResult:
    price = ticker_data.get("price", position.get("entry_price", 0))

    # Regime-adjusted weights (Priority 6, scaffold - disabled by default,
    # gated on live trade-history count; see
    # engine/regime_weight_adaptation.py). db=None (caller didn't pass one)
    # simply fails the gate closed and returns the Priority-3 base weights
    # unchanged, same as the swing-buy side.
    _effective_exit_weights = None
    if cfg is not None:
        from engine.regime_weight_adaptation import get_effective_bucket_weights
        _effective_exit_weights = get_effective_bucket_weights(cfg, regime, db, engine="exit_score")

    # -- BUCKET 1: TREND DETERIORATION (25% weight, max 36pts) --
    b1_pts = 0.0
    b1_rules = []

    sma_20 = ticker_data.get("sma_20", 0)
    sma_50 = ticker_data.get("sma_50", 0)
    ema_9 = ticker_data.get("ema_9", 0)
    ema_21 = ticker_data.get("ema_21", 0)
    weekly_above_sma20 = ticker_data.get("weekly_above_sma20", True)
    weekly_above_sma50 = ticker_data.get("weekly_above_sma50", True)
    adx = ticker_data.get("adx", 0)
    prev_adx = position.get("prev_cycle_adx")

    if sma_20 and price < sma_20:
        b1_pts += 6; b1_rules.append("below_sma20")
    if sma_50 and price < sma_50:
        b1_pts += 10; b1_rules.append("below_sma50")
    if ema_9 and ema_21 and ema_9 < ema_21:
        b1_pts += 6; b1_rules.append("ema9_lt_ema21")
    if not weekly_above_sma20 and not weekly_above_sma50:
        b1_pts += 8; b1_rules.append("weekly_trend_broken")
    # adx_weakening: REAL delta vs last cycle's ADX (storage/database.py's
    # prev_cycle_adx column) - a true "weakening" check, not just "currently
    # low". Neutral (0) on a position's first Loop B cycle when there's no
    # prior reading yet.
    if prev_adx is not None and adx < prev_adx - 2:
        b1_pts += 6; b1_rules.append(f"adx_weakening_{prev_adx:.0f}to{adx:.0f}")
    # below_avwap_earnings: REAL as of 2026-07-16, same avwap_earnings field
    # as the buy side's above_avwap_earnings (rules/swing_buy_rules.py) and
    # the pre-entry BELOW_AVWAP hard veto (rules/hard_vetoes.py) - not
    # duplicate authority with that veto, since the veto only blocks NEW
    # buys and this bucket only scores EXISTING open positions during Loop B.
    # Symmetric 8-pt value, matching the buy side's above_avwap_earnings.
    avwap_earnings = ticker_data.get("avwap_earnings", 0)
    if avwap_earnings and price and price < avwap_earnings:
        b1_pts += 8; b1_rules.append("below_avwap_earnings")

    b1_max = 44.0
    b1 = ExitBucketScore("TREND_DETERIORATION", _exit_bucket_weight(cfg, "TREND_DETERIORATION", _effective_exit_weights), b1_pts, b1_max, b1_rules)

    # -- BUCKET 2: MOMENTUM WEAKNESS (20% weight, max 35pts) --
    b2_pts = 0.0
    b2_rules = []

    macd_hist = ticker_data.get("macd_hist", 0)
    macd_dir = ticker_data.get("macd_crossover_direction", "none")
    rsi = ticker_data.get("rsi", 50)
    stoch_k = ticker_data.get("stoch_k", 50)
    prev_stoch_k = position.get("prev_cycle_stoch_k")

    if macd_dir == "bearish":
        b2_pts += 12; b2_rules.append("macd_bearish_crossover")
    if macd_hist and macd_hist < 0:
        b2_pts += 8; b2_rules.append("macd_hist_negative")
    # stoch_rollover: REAL delta - was overbought last cycle AND has since
    # come down, not just "currently high". PROXY for a true stochastic
    # rollover pattern (doesn't confirm a full swing high formed).
    if prev_stoch_k is not None and prev_stoch_k >= 80 and stoch_k < prev_stoch_k:
        b2_pts += 8; b2_rules.append(f"stoch_rollover_{prev_stoch_k:.0f}to{stoch_k:.0f}")
    # rsi_overbought: PROXY for bearish price/RSI divergence - a true
    # divergence check needs a multi-bar pivot comparison this codebase
    # doesn't compute yet; this only checks RSI's own level, not whether
    # price made a higher high while RSI made a lower high.
    if rsi >= 70:
        b2_pts += 7; b2_rules.append(f"rsi_overbought_{rsi:.0f}")

    b2_max = 35.0
    b2 = ExitBucketScore("MOMENTUM_WEAKNESS", _exit_bucket_weight(cfg, "MOMENTUM_WEAKNESS", _effective_exit_weights), b2_pts, b2_max, b2_rules)

    # -- BUCKET 3: VOLUME DISTRIBUTION (20% weight, max 32pts) --
    b3_pts = 0.0
    b3_rules = []

    obv_falling = ticker_data.get("obv_falling", False)
    cmf = ticker_data.get("cmf", 0)
    change_pct = ticker_data.get("change_pct", 0)
    volume_ratio = ticker_data.get("volume_ratio", 1.0)
    avwap_swing_low = ticker_data.get("avwap_swing_low", 0)

    if obv_falling:
        b3_pts += 10; b3_rules.append("obv_falling")
    if cmf < 0:
        b3_pts += 8; b3_rules.append(f"cmf_negative_{cmf:.2f}")
    # distribution_day: REAL - a down day on above-average volume, the
    # classic institutional-selling tell (Volume Distribution's whole point,
    # per the deployment review: "institutional selling often appears here
    # first").
    if change_pct < 0 and volume_ratio >= 1.5:
        b3_pts += 8; b3_rules.append(f"distribution_day_{volume_ratio:.1f}x_vol")
    # avwap_swing_low_broken: REAL - price has broken below the anchored VWAP
    # from the last swing low, which used to act as a bounce level (see
    # rules/swing_buy_rules.py's avwap_swing_low_bounce for the entry-side
    # mirror of this same real field). PROXY for a true "AVWAP rejection"
    # candlestick pattern, which this doesn't detect.
    if avwap_swing_low and price and price < avwap_swing_low:
        b3_pts += 6; b3_rules.append("avwap_swing_low_broken")

    b3_max = 32.0
    b3 = ExitBucketScore("VOLUME_DISTRIBUTION", _exit_bucket_weight(cfg, "VOLUME_DISTRIBUTION", _effective_exit_weights), b3_pts, b3_max, b3_rules)

    # -- BUCKET 4: MARKET CONTEXT (15% weight, max 32pts) --
    b4_pts = 0.0
    b4_rules = []

    ad_ratio = market_data.get("ad_ratio", 0.5)
    mcclellan = market_data.get("mcclellan", 0)
    vix = market_data.get("vix", 18)
    sector_rs_1d = ticker_data.get("sector_rs_1d", 0)
    sector_rs_1m = ticker_data.get("sector_rs_1m", 0)
    entry_regime = position.get("entry_regime") or ""
    current_regime = getattr(regime, "dominant_regime", "") or ""

    if ad_ratio < 0.35 or mcclellan < -40:
        b4_pts += 10; b4_rules.append(f"breadth_collapsing_ad{ad_ratio:.2f}")
    # regime_deteriorated: REAL - compares the regime this position was
    # ENTERED under (position.entry_regime, stored at entry) to the CURRENT
    # regime - "sometimes you don't sell because the stock changed, you sell
    # because the market changed" (deployment review, Market Context bucket).
    if entry_regime and entry_regime not in ("BEAR", "CRISIS") and current_regime in ("BEAR", "CRISIS"):
        b4_pts += 8; b4_rules.append(f"regime_deteriorated_{entry_regime}to{current_regime}")
    # vix_spike: INFORMATIONAL ONLY as of 2026-07-15c (external review's
    # duplicate-authority point): VIX >= 28 is already a HARD exit in
    # rules/sell_rules.py - once that fires, these points can't change the
    # action, and before it fires they double-count the same regime change
    # the hard exit owns. Kept in the rules list (0 pts) for observability.
    if vix >= 28:
        b4_rules.append(f"vix_spike_{vix:.0f}_informational_hard_exit_owns_this")
    if sector_rs_1d < 0 and sector_rs_1m < 0:
        b4_pts += 6; b4_rules.append("sector_losing_leadership")

    # b4_max = achievable sum (breadth 10 + regime 8 + sector 6 = 24; was 32
    # when vix_spike scored 8 - now informational, hard exit owns it).
    b4_max = 24.0
    b4 = ExitBucketScore("MARKET_CONTEXT", _exit_bucket_weight(cfg, "MARKET_CONTEXT", _effective_exit_weights), b4_pts, b4_max, b4_rules)

    # -- BUCKET 5: FUNDAMENTAL RISK (10% weight, max 25pts) --
    b5_pts = 0.0
    b5_rules = []

    days_to_earnings = ticker_data.get("days_to_earnings", 999)
    insider_sells_30d = ticker_data.get("insider_sells_30d", 0)
    news_sentiment_score = ticker_data.get("news_sentiment_score", 0.5)

    # earnings <=2d: INFORMATIONAL ONLY as of 2026-07-15c - same duplicate-
    # authority fix as vix_spike above: rules/sell_rules.py's
    # earnings_approaching hard exit owns this condition (days_before: 2).
    # 3-4 days out still scores (below) - that's genuinely earlier warning
    # than the hard exit provides, not a duplicate.
    # 0 <= guards (2026-07-16): negative days_to_earnings = past earnings
    # date from finviz (no event risk) - must not score as "approaching".
    if 0 <= days_to_earnings <= 2:
        b5_rules.append(f"earnings_in_{days_to_earnings}d_informational_hard_exit_owns_this")
    elif 0 <= days_to_earnings <= 4:
        b5_pts += 6; b5_rules.append(f"earnings_approaching_{days_to_earnings}d")
    if insider_sells_30d >= 10000:
        b5_pts += 8; b5_rules.append(f"insider_selling_heavy_{insider_sells_30d:,}sh")
    if news_sentiment_score <= 0.25:
        b5_pts += 7; b5_rules.append(f"news_sentiment_crash_{news_sentiment_score:.2f}")
    # analyst_downgrade: REAL as of 2026-07-16 - same FMP /stable/grades
    # signal as the buy side's no_recent_downgrade, opposite polarity (an
    # existing HELD position getting downgraded is exit evidence). 5-pt
    # value mirrors the buy side's no_recent_downgrade.
    recent_downgrade = ticker_data.get("recent_downgrade", False)
    if recent_downgrade:
        b5_pts += 5; b5_rules.append("analyst_downgrade")
    # negative_guidance / regulatory_action: STILL PLACEHOLDER - no
    # guidance-cut or regulatory-event data source in this codebase (same
    # gap rules/hard_vetoes.py's REG_NEWS veto has). Not counted in b5_max.

    # b5_max = achievable sum (earnings_approaching 6 + insider 8 + news 7 +
    # analyst_downgrade 5 = 26; was 25 when earnings<=2d scored 10 - now
    # informational, hard exit owns it. 2026-07-16: analyst_downgrade 5 added.)
    b5_max = 26.0
    b5 = ExitBucketScore("FUNDAMENTAL_RISK", _exit_bucket_weight(cfg, "FUNDAMENTAL_RISK", _effective_exit_weights), b5_pts, b5_max, b5_rules)

    # -- BUCKET 6: POSITION HEALTH (10% weight, max 10pts) --
    # Folds in engine/position_health.py's independent score (inverted - low
    # health = high exit pressure) plus the MAE-percentile anomaly check
    # (engine/mae_mfe_engine.py - "winning NVDA trades usually pull back 3%,
    # this one's at 8%, confidence drops") and the time-stop check
    # (engine/position_management.py._check_time_stop), so those two no
    # longer need their own separate override branches in
    # _evaluate_priority() - they're evidence feeding the SAME score instead
    # of competing opinions next to it.
    b6_pts = 0.0
    b6_rules = []

    health_score = getattr(health, "score", 100.0)
    b6_pts += max(0.0, (100.0 - health_score) / 100.0 * 7.0)
    if health_score < 60:
        b6_rules.append(f"health_weak_{health_score:.0f}")

    mae_status = (mae_eval or {}).get("status")
    if mae_status == "anomalous":
        b6_pts += 2; b6_rules.append("mae_anomalous")
    elif mae_status == "elevated":
        b6_pts += 1; b6_rules.append("mae_elevated")

    if time_stop:
        b6_pts += 1; b6_rules.append(f"time_stop_{time_stop.get('type', 'triggered')}")

    b6_max = 10.0
    b6 = ExitBucketScore("POSITION_HEALTH", _exit_bucket_weight(cfg, "POSITION_HEALTH", _effective_exit_weights), min(b6_pts, b6_max), b6_max, b6_rules)

    # -- FINAL SCORE --
    all_buckets = [b1, b2, b3, b4, b5, b6]
    total = sum((b.points / b.max_points) * b.weight * 100 for b in all_buckets)
    total = max(0.0, min(100.0, total))

    all_reasons = [r for b in all_buckets for r in b.rules_fired]
    action, partial_pct = _action_for_score(total)

    return ExitScoreResult(
        total_score=total,
        buckets=all_buckets,
        reasons=all_reasons[:8],
        action=action,
        partial_exit_pct=partial_pct,
    )
