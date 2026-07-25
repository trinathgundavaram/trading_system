"""Scoring-engine sanity tests (2026-07-15, from the external review's
"add unit tests proving every max-point denominator equals achievable
points" recommendation). Run with:  python3 -m pytest tests/  (or just
python3 tests/test_scoring_sanity.py).

These build a deliberately maxed-out ticker/market dict, score it, and
assert that (a) every bucket's earned points exactly equal its declared
max_points (no unreachable points inflating a denominator, no achievable
sum exceeding it), and (b) the composite behaves: perfect setup ~100,
empty setup ~0, day-mode threshold is +3 over swing.
"""
import sys
import os
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub the mcp SDK so these tests run without it installed.
class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()
for m in ["mcp", "mcp.client", "mcp.client.stdio", "mcp.client.streamable_http"]:
    sys.modules.setdefault(m, types.ModuleType(m))
sys.modules["mcp"].ClientSession = _Any
sys.modules["mcp"].StdioServerParameters = _Any
sys.modules["mcp.client.stdio"].stdio_client = _Any

from dataclasses import dataclass

import yaml

from rules import swing_buy_rules as sbr
from rules.dynamic_thresholds import calculate as calc_threshold


@dataclass
class FakeRegime:
    bull_pct: float = 80.0
    bear_pct: float = 0.0
    choppy_pct: float = 20.0
    transition_probability: float = 0.0
    crisis_active: bool = False
    dominant_regime: str = "BULL"
    confidence_score: float = 70.0


def _cfg():
    with open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")) as f:
        return yaml.safe_load(f)


def _maxed_ticker():
    """A ticker that fires every achievable rule at its highest tier.
    RSI/stoch use the PULLBACK path (higher points than the momentum-zone
    path); bb uses the lower-touch path (8 > upper-ride 5); OBV uses the
    divergence tier (6 > new-high 4); NR7 (6 > NR4 3)."""
    return dict(
        price=100, sma_20=95, sma_50=90, sma_200=80, ema_9=99, ema_21=97,
        adx=30, plus_di=30, minus_di=10,  # adx_trending_bullish needs +DI > -DI (2026-07-15)
        external_data_available=True,
        weekly_above_sma20=True, weekly_above_sma50=True,
        # avwap_earnings=90 (2026-07-16: above_avwap_earnings went REAL,
        # was a 0/placeholder value here before) - must be < price=100 for
        # the rule to fire, same style as sma_50=90 above.
        avwap_earnings=90, donchian_20d_high=100, rs_vs_spy_1m=5.0,
        rsi=30, macd=1.0, macd_signal=0.5, macd_hist=0.5, stoch_k=10,
        macd_positive_days=12,
        rvol_quality_score=90, obv_rising=True, cmf=0.2, bb_pct=0.10,
        vwap=99, avwap_swing_low=100.5, poc_price=0,
        obv_new_high_20d=True, obv_divergence=True,
        dollar_vol_ratio_20_50=1.5, accumulation_days_10=7,
        maverick_bullish=True, finviz_technical_rating="Strong Buy",
        analyst_consensus="Buy", industry_rs_positive=True,
        unusual_options_bullish=True, analyst_estimate_raised=True,
        no_recent_downgrade=True,
        news_multiplier=2.5, sector_rs_1d=1.0, sector_rs_1m=2.0,
        insider_net_buying=True, short_float_pct=1,
        squeeze_active=True, is_nr7=True, is_nr4=False, is_inside_day=True,
        bid=99.99, ask=100.01, atr=2.0, quote_type="EQUITY", sector="Technology",
    )


def _maxed_market():
    return dict(
        fg_score=50, yield_spread_2s10s=0.5, vix=15,
        ad_ratio=0.9, pct_above_20ema=90, pct_above_50ema=90, nh_nl_ratio=3.0,
        mcclellan=50, ad_slope_5d_positive=True, spy_ad_aligned=True,
        breadth_acceleration=8.0, opex_status="normal",
    )


def test_every_bucket_max_is_achievable_and_tight():
    cfg = _cfg()
    res = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing")
    for b in res.buckets:
        assert abs(b.points - b.max_points) < 1e-9, (
            f"{b.name}: earned {b.points} != declared max {b.max_points} on a "
            f"maxed-out setup - denominator is stale (fired: {b.rules_fired})"
        )
    # A perfect setup should compute to ~100 before the volatility bonus
    # (bonus is clamped so the total never exceeds 100).
    assert res.final_score_pct >= 99.0, res.final_score_pct


def test_empty_setup_scores_near_zero():
    cfg = _cfg()
    dead = dict(
        price=100, sma_20=110, sma_50=115, sma_200=120, ema_9=99, ema_21=101,
        adx=10, rsi=80, stoch_k=95, macd=-1, macd_signal=0, macd_hist=-0.5,
        rvol_quality_score=10, obv_rising=False, cmf=-0.1, bb_pct=0.5,
        vwap=101, bid=99.99, ask=100.01, atr=2.0, news_multiplier=0.7,
        fg_score=90, short_float_pct=30, quote_type="EQUITY", sector="Technology",
    )
    weak_mkt = dict(fg_score=90, yield_spread_2s10s=-1.0, vix=29, ad_ratio=0.2,
                    pct_above_20ema=20, pct_above_50ema=20, nh_nl_ratio=0.3,
                    mcclellan=-80, ad_slope_5d_positive=False, spy_ad_aligned=False,
                    breadth_acceleration=-5.0, opex_status="normal")
    res = sbr.score(dead, weak_mkt, FakeRegime(bull_pct=0, choppy_pct=50, bear_pct=50,
                                               dominant_regime="BEAR"), cfg)
    assert res.final_score_pct < 10.0, res.final_score_pct
    assert not res.passed


def test_day_mode_threshold_is_plus_3():
    kw = dict(base_threshold=55, regime=FakeRegime(), vix=15, day_of_week=1,
              opex_status="normal", breadth_data={"mcclellan": 10, "ad_ratio": 0.6})
    swing = calc_threshold(mode="swing", **kw)
    day = calc_threshold(mode="day", **kw)
    assert day["final_threshold"] == swing["final_threshold"] + 3.0


def test_calendar_is_log_only_by_default():
    kw = dict(base_threshold=55, regime=FakeRegime(), vix=15, day_of_week=4,  # Friday
              opex_status="opex_week", breadth_data={"mcclellan": 10, "ad_ratio": 0.6})
    t = calc_threshold(**kw)
    assert t["cal_adj"] == 0.0, "calendar applied despite calendar_enabled=False default"
    assert t["cal_computed"] == 10.0, t["cal_computed"]  # Fri+5 + OpEx+5, logged
    t_on = calc_threshold(calendar_enabled=True, **kw)
    assert t_on["final_threshold"] == t["final_threshold"] + 10.0


def test_rule_fire_never_decreases_score():
    """Invariant (external review): turning any non-negative rule condition
    true must never lower the final score."""
    cfg = _cfg()
    base = _maxed_ticker()
    base_no_obv = dict(base, obv_rising=False, obv_new_high_20d=False, obv_divergence=False)
    lo = sbr.score(base_no_obv, _maxed_market(), FakeRegime(), cfg).final_score_pct
    hi = sbr.score(base, _maxed_market(), FakeRegime(), cfg).final_score_pct
    assert hi >= lo, (hi, lo)


def test_bucket_independence():
    """A change confined to VOLUME_PA inputs must not alter TREND's points."""
    cfg = _cfg()
    a = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg)
    b = sbr.score(dict(_maxed_ticker(), cmf=-0.5, obv_rising=False),
                  _maxed_market(), FakeRegime(), cfg)
    ta = next(x for x in a.buckets if x.name == "TREND")
    tb = next(x for x in b.buckets if x.name == "TREND")
    assert ta.points == tb.points


def test_external_unavailable_renormalizes_not_zeroes():
    """UNKNOWN != FALSE: EXTERNAL sources all down -> 75% of its weight is
    redistributed (score HIGHER than the same stock with sources up but all
    negative), 25% left dead (score LOWER than with sources up and all
    positive)."""
    cfg = _cfg()
    m = _maxed_market()
    up_all_positive = sbr.score(_maxed_ticker(), m, FakeRegime(), cfg).final_score_pct
    down = dict(_maxed_ticker(), external_data_available=False,
                maverick_bullish=False, finviz_technical_rating="",
                analyst_consensus="", industry_rs_positive=False)
    unavailable = sbr.score(down, m, FakeRegime(), cfg)
    up_all_negative = sbr.score(dict(down, external_data_available=True), m,
                                FakeRegime(), cfg).final_score_pct
    assert unavailable.final_score_pct > up_all_negative, \
        (unavailable.final_score_pct, up_all_negative)
    assert unavailable.final_score_pct < up_all_positive
    assert unavailable.threshold_result["data_coverage"]["unavailable_buckets"] == ["EXTERNAL"]


def test_oversold_in_broken_trend_scores_token_points():
    """Falling-knife guard: RSI oversold below a broken SMA50 earns 4, not 12."""
    cfg = _cfg()
    knife = dict(_maxed_ticker(), price=85, sma_50=90, sma_20=95, donchian_20d_high=200,
                 avwap_swing_low=0, vwap=0, bb_pct=0.5)
    res = sbr.score(knife, _maxed_market(), FakeRegime(), cfg)
    mom = next(b for b in res.buckets if b.name == "MOMENTUM")
    assert any(r.startswith("rsi_oversold_broken_trend") for r in mom.rules_fired), mom.rules_fired


def test_qualification_multiplier_continuous_and_monotonic():
    """Pins the canonical qualification semantics (flagged by two external
    reviews as historically ambiguous): a CONTINUOUS anchor-table curve,
    monotonically non-decreasing, with no cliff anywhere - including around
    any min_qualify_pct value, which must have NO effect on the multiplier."""
    from rules.swing_buy_rules import _qualification_multiplier as q
    prev = -1.0
    for i in range(0, 1001):
        p = i / 1000.0
        v = q(p, 0.40)
        assert v >= prev - 1e-12, f"decreasing at {p}: {v} < {prev}"
        # continuity: neighboring points can't jump more than the steepest
        # anchor segment slope (0.30->0.40 maps 0.35->0.50 = 1.5/unit) times
        # the step + epsilon
        if prev >= 0:
            assert abs(v - prev) <= 1.5 / 1000 + 1e-9, f"jump at {p}"
        prev = v
    assert q(0.0, 0.40) == 0.0 and q(1.0, 0.40) == 1.0
    # min_pct is presentation-only: same multiplier regardless of min_pct
    for p in (0.1, 0.3, 0.45, 0.7):
        assert q(p, 0.30) == q(p, 0.50) == q(p, 0.0)


def test_exit_scorer_no_duplicate_hard_exit_authority():
    """vix_spike (>=28) and earnings<=2d are hard exits - the soft Exit
    Score must list them as informational only, never as points."""
    import inspect
    from rules import exit_scorer
    src = inspect.getsource(exit_scorer)
    assert "informational_hard_exit_owns_this" in src


def test_cumulative_trend_evidence_monotonic():
    """Review round 4's cross-bucket harness: adding TREND evidence one rule
    at a time must never DECREASE the composite score (the structure cap may
    flatten it, never invert it)."""
    cfg = _cfg()
    base = dict(_maxed_ticker(), sma_200=200, sma_50=150, sma_20=120, ema_9=90, ema_21=95,
                adx=10, weekly_above_sma20=False, weekly_above_sma50=False,
                donchian_20d_high=999, rs_vs_spy_1m=-1.0)  # no trend evidence
    steps = [
        {"sma_20": 95},                        # + above_sma20
        {"sma_50": 90},                        # + above_sma50
        {"sma_200": 80},                       # + above_sma200
        {"ema_9": 99, "ema_21": 97},           # + ema alignment
        {"weekly_above_sma20": True, "weekly_above_sma50": True},  # + weekly
        {"adx": 30},                           # + adx (plus_di/minus_di already set)
        {"donchian_20d_high": 100},            # + breakout
        {"rs_vs_spy_1m": 5.0},                 # + RS
    ]
    prev = sbr.score(base, _maxed_market(), FakeRegime(), cfg).final_score_pct
    t = dict(base)
    for step in steps:
        t.update(step)
        cur = sbr.score(t, _maxed_market(), FakeRegime(), cfg).final_score_pct
        assert cur >= prev - 1e-9, f"composite decreased after adding {step}: {prev} -> {cur}"
        prev = cur


def test_challenger_shadow_never_affects_champion():
    """Challenger shadow scoring is logged, never acted on: identical
    champion score and passed decision with or without a challenger profile
    configured, and the challenger result is present + internally coherent."""
    import copy
    cfg = _cfg()
    cfg2 = copy.deepcopy(cfg)
    cfg2["weights"]["swing_buy_challenger"] = {"bucket_weights": {
        "TREND": 0.20, "MOMENTUM": 0.19, "VOLUME_PA": 0.18, "EXTERNAL": 0.15,
        "SENTIMENT_MACRO": 0.15, "MARKET_BREADTH": 0.13, "VOLATILITY_EXPANSION": 0.0}}
    a = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg)
    b = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg2)
    assert abs(a.final_score_pct - b.final_score_pct) < 1e-9
    assert a.passed == b.passed
    ch = b.threshold_result.get("challenger")
    assert ch and ch["profile"] == "swing_buy_challenger"
    assert 0 <= ch["final_score_pct"] <= 100
    assert ch["champion_score_pct"] == round(b.final_score_pct, 1)


def test_bucket_invariants_hold_across_scenarios():
    """Code-level assertions (2026-07-21, external review round 2 - "add
    startup/unit assertions... this prevents a later rule-point edit from
    silently reintroducing denominator inflation"): for every bucket, on
    every scenario this file exercises (maxed, empty, partial/knife), the
    three invariants a correct effective-max/qual_mult implementation must
    never violate:
      1. earned_points <= max_points (no rule combination can exceed its
         own bucket's declared denominator - this is what
         test_every_bucket_max_is_achievable_and_tight already pins for the
         maxed case; here it's checked on every scenario, not just one).
      2. 0.0 <= bucket_pct <= 1.0 (pct is earned/max - a direct
         restatement of #1, but checked explicitly since pct, not raw
         points, is what feeds _qualification_multiplier()).
      3. 0.0 <= qual_mult <= 1.0 (the soft-qualification curve's output
         range - see _qualification_multiplier()'s own anchor table).
    """
    cfg = _cfg()
    scenarios = [
        sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing"),
        sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="day"),
        sbr.score(
            dict(price=100, sma_20=110, sma_50=115, sma_200=120, ema_9=99, ema_21=101,
                 adx=10, rsi=80, stoch_k=95, macd=-1, macd_signal=0, macd_hist=-0.5,
                 rvol_quality_score=10, obv_rising=False, cmf=-0.1, bb_pct=0.5,
                 vwap=101, bid=99.99, ask=100.01, atr=2.0, news_multiplier=0.7,
                 fg_score=90, short_float_pct=30, quote_type="EQUITY", sector="Technology"),
            dict(fg_score=90, yield_spread_2s10s=-1.0, vix=29, ad_ratio=0.2,
                 pct_above_20ema=20, pct_above_50ema=20, nh_nl_ratio=0.3, mcclellan=-80,
                 ad_slope_5d_positive=False, spy_ad_aligned=False, breadth_acceleration=-5.0,
                 opex_status="normal"),
            FakeRegime(bull_pct=0, choppy_pct=50, bear_pct=50, dominant_regime="BEAR"), cfg,
        ),
        # Partial/mixed setup - some rules fire, some don't, exercising the
        # mutually-exclusive branches (RSI/stoch/bb tiers) and the family
        # caps at a non-boundary value.
        sbr.score(dict(_maxed_ticker(), rsi=55, stoch_k=50, bb_pct=0.4, adx=5,
                       macd_positive_days=3, no_recent_downgrade=False),
                  _maxed_market(), FakeRegime(), cfg, mode="hybrid"),
    ]
    for res in scenarios:
        for b in res.buckets:
            assert b.points <= b.max_points + 1e-9, (
                f"{b.name}: earned {b.points} > declared max {b.max_points} - "
                f"denominator inflation (fired: {b.rules_fired})"
            )
            pct = (b.points / b.max_points) if b.max_points else 0.0
            assert -1e-9 <= pct <= 1.0 + 1e-9, f"{b.name}: bucket_pct {pct} out of [0,1]"
            assert -1e-9 <= b.qual_mult <= 1.0 + 1e-9, f"{b.name}: qual_mult {b.qual_mult} out of [0,1]"


def test_external_partial_unavailable_never_scores_below_measured_negative():
    """2026-07-22 regression (found via manual simulation while validating
    the finviz/FMP-outage fix, not by this file's own coverage - the
    existing test_external_unavailable_renormalizes_not_zeroes only
    exercises a FULL EXTERNAL outage): a PARTIAL outage (some EXTERNAL
    rules confirmed-unavailable, e.g. only finviz + FMP down, others still
    measured) was scoring WORSE than the identical ticker with those same
    rules explicitly MEASURED NEGATIVE - the opposite of "UNKNOWN != FALSE".
    Root cause: the redistribution math cut the bucket's WEIGHT for the
    dark fraction but still applied the bucket's naive points/max_points
    ratio (which already treats unmeasured rules as 0, same as measured-
    negative, since max_points still counts them in the denominator) -
    double-penalizing the same missing evidence once in the ratio and
    again in the weight cut. Fixed by excluding confirmed-dark points from
    the denominator too (_effective_bucket_pct), so partial-unavailable
    lands strictly between measured-negative and measured-positive."""
    cfg = _cfg()
    mkt = _maxed_market()
    # A moderate, non-maxed setup so the EXTERNAL bucket's measured portion
    # isn't already saturated (which would ceiling-clip the comparison).
    base = dict(
        _maxed_ticker(), rsi=45, stoch_k=45, adx=15, macd_positive_days=3,
    )
    # 3 EXTERNAL rules in play: finviz technical rating (10pt),
    # analyst_estimate_raised (6pt), no_recent_downgrade (2pt) = 18/54.
    neg = dict(base, finviz_technical_rating="Strong Sell",
               analyst_estimate_raised=False, no_recent_downgrade=False,
               external_unavailable_points=0, external_bucket_max_points=54)
    unavail = dict(base, finviz_technical_rating="N/A",
                   analyst_estimate_raised=None, no_recent_downgrade=None,
                   external_unavailable_points=18, external_bucket_max_points=54)
    pos = dict(base, finviz_technical_rating="Strong Buy",
               analyst_estimate_raised=True, no_recent_downgrade=True,
               external_unavailable_points=0, external_bucket_max_points=54)
    r_neg = sbr.score(neg, mkt, FakeRegime(), cfg, mode="swing").final_score_pct
    r_unavail = sbr.score(unavail, mkt, FakeRegime(), cfg, mode="swing").final_score_pct
    r_pos = sbr.score(pos, mkt, FakeRegime(), cfg, mode="swing").final_score_pct
    assert r_neg < r_unavail < r_pos, (r_neg, r_unavail, r_pos)


def test_momentum_and_external_effective_max_arithmetic():
    """Code-level assertion (2026-07-21, external review round 2) pinning
    the exact declared max_points for the two buckets the review flagged as
    needing an explicit arithmetic check, so a future point-value edit to
    any one rule can't silently drift the bucket's denominator out of sync:

    MOMENTUM (b2_max=35): rsi pullback 12 + stoch pullback 5 + MACD family
    (cross 8 + hist 6 + persistence-10d+ 5 = 19, capped at 18) = 35.
    EXTERNAL (b4_max=54, 2026-07-22: was 48 - unusual_options_bullish went
    from a permanent 0-point placeholder to a real 6-point rule once
    stock_scanner.py was remapped to options_unusual_activity, an already-
    connected tool the stale tool map never actually called - see
    mcp_clients/stock_scanner.py's 2026-07-22 note): maverick 12 + finviz 10
    + analyst 5 + sector_rs_1m_positive_proxy 13 + unusual_options_bullish 6
    + estimate_raised 6 + no_recent_downgrade 2 = 54.
    """
    cfg = _cfg()
    res = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing")
    mom = next(b for b in res.buckets if b.name == "MOMENTUM")
    ext = next(b for b in res.buckets if b.name == "EXTERNAL")
    assert mom.max_points == 35.0, mom.max_points
    assert ext.max_points == 54.0, ext.max_points
    # And the maxed fixture should actually reach both (the achievability
    # half test_every_bucket_max_is_achievable_and_tight already pins, but
    # cheap to re-assert here alongside the literal arithmetic).
    assert abs(mom.points - 35.0) < 1e-9, mom.points
    assert abs(ext.points - 54.0) < 1e-9, ext.points


def test_day_mode_bucket_weights_reweighted_vs_swing():
    """2026-07-22 (full DAY/SWING/HYBRID separation): mode='day' must select
    weights.swing_buy_day (not swing_buy) - VOLUME_PA/MOMENTUM/MARKET_BREADTH
    higher, TREND/EXTERNAL lower, per config.yaml's swing_buy_day comment.
    Weights still sum to 1.0 (score() itself doesn't renormalize - config.yaml
    is the source of truth there, already asserted at load time, but cheap
    to re-check the invariant holds through the actual score() call path)."""
    cfg = _cfg()
    swing = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing")
    day = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="day")
    sw = {b.name: b.weight for b in swing.buckets}
    dy = {b.name: b.weight for b in day.buckets}
    assert dy["VOLUME_PA"] > sw["VOLUME_PA"], (dy["VOLUME_PA"], sw["VOLUME_PA"])
    assert dy["MOMENTUM"] > sw["MOMENTUM"], (dy["MOMENTUM"], sw["MOMENTUM"])
    assert dy["MARKET_BREADTH"] > sw["MARKET_BREADTH"], (dy["MARKET_BREADTH"], sw["MARKET_BREADTH"])
    assert dy["TREND"] < sw["TREND"], (dy["TREND"], sw["TREND"])
    assert dy["EXTERNAL"] < sw["EXTERNAL"], (dy["EXTERNAL"], sw["EXTERNAL"])
    assert abs(sum(dy[k] for k in dy if k != "VOLATILITY_EXPANSION")
               + dy["VOLATILITY_EXPANSION"] - 1.0) < 1e-9, dy


def test_day_mode_base_threshold_higher_than_swing():
    """base_threshold itself (before dynamic_thresholds' own +3 mode_adj) is
    now mode-aware - buy_score_threshold_day_pct, not just
    buy_score_threshold_pct + a flat adjustment. TURBO (this config's active
    risk_level): 50 swing -> 55 day, so day's base_threshold alone should be
    5 points higher, before mode_adj even applies."""
    cfg = _cfg()
    swing = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing")
    day = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="day")
    swing_base = swing.threshold_result["base_threshold"]
    day_base = day.threshold_result["base_threshold"]
    assert day_base > swing_base, (day_base, swing_base)
    expected_day_base = cfg["risk"][cfg["risk_level"]]["buy_score_threshold_day_pct"]
    assert day_base == expected_day_base, (day_base, expected_day_base)


def test_hybrid_mode_scores_identically_to_swing():
    """HYBRID must NOT take the DAY-reweighted path at scoring time - see
    pre_selection_criteria_and_trading_modes.md Section 3: HYBRID scores
    through the swing engine and only splits into DAY/SWING legs AFTER a buy
    signal fires (scheduler.py's _classify_hybrid_leg), never at entry
    scoring. Bucket weights AND base_threshold must be bit-for-bit identical
    between mode='swing' and mode='hybrid'."""
    cfg = _cfg()
    swing = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="swing")
    hybrid = sbr.score(_maxed_ticker(), _maxed_market(), FakeRegime(), cfg, mode="hybrid")
    sw = {b.name: b.weight for b in swing.buckets}
    hy = {b.name: b.weight for b in hybrid.buckets}
    assert sw == hy, (sw, hy)
    assert swing.threshold_result["base_threshold"] == hybrid.threshold_result["base_threshold"]
    assert swing.threshold_result["mode_adj"] == hybrid.threshold_result["mode_adj"] == 0.0


def test_day_position_gets_tighter_stop_than_swing():
    """2026-07-22: engine/stop_state_machine.py's max_stop_pct ceiling reads
    stop_loss_day_pct for a position tagged trade_mode='DAY', vs
    stop_loss_swing_pct for everything else (including untagged legacy rows,
    which must be completely unaffected)."""
    from engine.stop_state_machine import calculate as calc_stop
    cfg = _cfg()
    entry = 100.0
    # Deep-ITM-style ticker data with a huge ATR so the ATR*mult stop would
    # normally be far wider than either pct ceiling - forces the pct floor
    # (stop_loss_swing_pct / stop_loss_day_pct) to be the binding constraint
    # for both, isolating exactly the thing this test checks.
    ticker_data = {"price": entry, "atr": 20.0}
    swing_pos = {"entry_price": entry, "entry_signal_score": 75, "trade_mode": "SWING"}
    day_pos = {"entry_price": entry, "entry_signal_score": 75, "trade_mode": "DAY"}
    untagged_pos = {"entry_price": entry, "entry_signal_score": 75}

    swing_stop = calc_stop(swing_pos, ticker_data, 0, cfg)
    day_stop = calc_stop(day_pos, ticker_data, 0, cfg)
    untagged_stop = calc_stop(untagged_pos, ticker_data, 0, cfg)

    turbo = cfg["risk"]["TURBO"]
    assert abs(swing_stop.stop_price - entry * (1 - turbo["stop_loss_swing_pct"] / 100)) < 1e-6
    assert abs(day_stop.stop_price - entry * (1 - turbo["stop_loss_day_pct"] / 100)) < 1e-6
    assert day_stop.stop_price > swing_stop.stop_price, (
        "DAY stop should sit CLOSER to entry (tighter) than SWING's - "
        f"day={day_stop.stop_price} swing={swing_stop.stop_price}")
    assert untagged_stop.stop_price == swing_stop.stop_price, (
        "a position with no trade_mode at all must behave exactly like SWING - "
        "never silently tightened/loosened by this change")


def test_day_position_sizing_applies_multiplier():
    """2026-07-22: engine/position_sizing.py's calculate(mode='DAY') applies
    position_sizing.day_size_multiplier (0.5 by default) on top of the
    existing score/EV/volatility/regime chain; mode='SWING' (or omitted) is
    a strict no-op (multiplier 1.0)."""
    from engine.position_sizing import calculate as calc_size

    class FakeBuyResult:
        should_buy = True
        pct_score = 90.0

    class FakeScoreResult:
        ev_result = None
        execution_quality = None

    cfg = _cfg()
    ticker_data = {"price": 100.0, "atr": 1.0}
    swing_result = calc_size(FakeBuyResult(), FakeScoreResult(), ticker_data, None, cfg, mode="SWING")
    day_result = calc_size(FakeBuyResult(), FakeScoreResult(), ticker_data, None, cfg, mode="DAY")
    default_result = calc_size(FakeBuyResult(), FakeScoreResult(), ticker_data, None, cfg)  # mode omitted

    expected_mult = cfg["position_sizing"].get("day_size_multiplier", 0.5)
    assert day_result.factors["day_mode_multiplier"] == expected_mult
    assert swing_result.factors["day_mode_multiplier"] == 1.0
    assert default_result.factors["day_mode_multiplier"] == 1.0, "omitted mode must default to SWING behavior"
    assert abs(day_result.suggested_size_pct - swing_result.suggested_size_pct * expected_mult) < 1e-6, (
        day_result.suggested_size_pct, swing_result.suggested_size_pct, expected_mult)


if __name__ == "__main__":
    test_cumulative_trend_evidence_monotonic()
    test_challenger_shadow_never_affects_champion()
    test_qualification_multiplier_continuous_and_monotonic()
    test_exit_scorer_no_duplicate_hard_exit_authority()
    test_every_bucket_max_is_achievable_and_tight()
    test_empty_setup_scores_near_zero()
    test_day_mode_threshold_is_plus_3()
    test_calendar_is_log_only_by_default()
    test_rule_fire_never_decreases_score()
    test_bucket_independence()
    test_external_unavailable_renormalizes_not_zeroes()
    test_external_partial_unavailable_never_scores_below_measured_negative()
    test_oversold_in_broken_trend_scores_token_points()
    test_bucket_invariants_hold_across_scenarios()
    test_momentum_and_external_effective_max_arithmetic()
    test_day_mode_bucket_weights_reweighted_vs_swing()
    test_day_mode_base_threshold_higher_than_swing()
    test_hybrid_mode_scores_identically_to_swing()
    test_day_position_gets_tighter_stop_than_swing()
    test_day_position_sizing_applies_multiplier()
    print("ALL SCORING SANITY TESTS PASS")
