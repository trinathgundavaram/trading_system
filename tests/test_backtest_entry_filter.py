"""Backtest-only entry_tier/regime research filter (2026-07-28), added after
Trinath's directive #3: "test a stricter regime gate or a 'BULL-only/
high-conviction only' subset, since the current BULL regime is not helping
much." A post-hoc slice of the 2026-07-28 run had already shown standard-tier
+ non-BULL trades hold up far better under cost (PF 1.28 @ 25bps vs 1.02
unfiltered on a fresh run, n=375) - engine/backtest_engine.py's
entry_filter_from_cfg()/passes_entry_filter() turn that into a real pre-trade
filter so run_replay's i-pointer actually advances past a rejected candidate
(capital/time redeployed to the next opportunity) instead of the trade being
taken and then sliced out of the results afterward.

This file pins:
  1. default-off: no cfg, empty cfg, or an entry_filter with both fields
     unset all accept every tier/regime - unchanged pre-2026-07-28 behaviour.
  2. min_tier is a FLOOR (weak < standard < strong), not an exact match.
  3. exclude_regimes rejects an exact membership match only.
  4. malformed input (unknown tier string, a bare string instead of a list)
     degrades to "no filter" / "treat as a single-item list" rather than
     crashing a replay.
  5. this is backtest-only - rules/swing_buy_rules.py is never imported by
     this module and has no entry_filter concept.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

import types

for m in ["mcp", "mcp.client", "mcp.client.stdio", "mcp.client.streamable_http"]:
    sys.modules.setdefault(m, types.ModuleType(m))


class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()


sys.modules["mcp"].ClientSession = _Any
sys.modules["mcp"].StdioServerParameters = _Any
sys.modules["mcp.client.stdio"].stdio_client = _Any

import pandas as pd  # noqa: E402

from engine.backtest_engine import (  # noqa: E402
    entry_filter_from_cfg, passes_entry_filter, realized_vol_pct, spy_trend_pct,
)


# --- entry_filter_from_cfg ---------------------------------------------------

def test_default_is_off_with_no_cfg():
    ef = entry_filter_from_cfg(None)
    assert ef["min_tier"] is None
    assert ef["exclude_regimes"] == set()


def test_off_when_backtest_section_present_but_entry_filter_absent():
    ef = entry_filter_from_cfg({"backtest": {"months": 36}})
    assert ef["min_tier"] is None
    assert ef["exclude_regimes"] == set()


def test_reads_configured_values():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {
        "min_tier": "standard", "exclude_regimes": ["BULL"]}}})
    assert ef["min_tier"] == "standard"
    assert ef["exclude_regimes"] == {"BULL"}


def test_unknown_tier_string_falls_back_to_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "legendary"}}})
    assert ef["min_tier"] is None


def test_a_bare_string_exclude_regimes_is_treated_as_one_item():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"exclude_regimes": "BULL"}}})
    assert ef["exclude_regimes"] == {"BULL"}


def test_max_vix_default_is_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "standard"}}})
    assert ef["max_vix"] is None


def test_max_vix_reads_configured_value():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_vix": 25}}})
    assert ef["max_vix"] == 25.0


def test_max_vix_malformed_falls_back_to_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_vix": "not-a-number"}}})
    assert ef["max_vix"] is None


# --- passes_entry_filter -----------------------------------------------------

_NO_FILTER = entry_filter_from_cfg(None)


def test_no_filter_accepts_every_tier_and_regime():
    for tier in ["weak", "standard", "strong"]:
        for regime in ["BULL", "BEAR", "CHOPPY"]:
            assert passes_entry_filter(tier, regime, _NO_FILTER) is True


def test_min_tier_is_a_floor_not_an_exact_match():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "standard"}}})
    assert passes_entry_filter("weak", "CHOPPY", ef) is False
    assert passes_entry_filter("standard", "CHOPPY", ef) is True
    assert passes_entry_filter("strong", "CHOPPY", ef) is True


def test_exclude_regimes_rejects_exact_membership_only():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"exclude_regimes": ["BULL"]}}})
    assert passes_entry_filter("strong", "BULL", ef) is False
    assert passes_entry_filter("strong", "BEAR", ef) is True
    assert passes_entry_filter("strong", "CHOPPY", ef) is True


def test_combined_min_tier_and_exclude_regimes_matches_the_review_finding():
    """The exact combination the 2026-07-28 review flagged as promising:
    standard-tier or better, and never during a BULL regime."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {
        "min_tier": "standard", "exclude_regimes": ["BULL"]}}})
    assert passes_entry_filter("standard", "CHOPPY", ef) is True
    assert passes_entry_filter("weak", "CHOPPY", ef) is False   # tier fails
    assert passes_entry_filter("standard", "BULL", ef) is False  # regime fails
    assert passes_entry_filter("weak", "BULL", ef) is False      # both fail


def test_max_vix_rejects_above_threshold_only():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_vix": 25}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=24.9) is True
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=25.0) is True   # boundary: not strictly above
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=25.1) is False


def test_max_vix_missing_vix_data_never_rejects():
    """No VIX reading for this candidate-day - don't fake a filter decision
    on missing data, same posture as the rest of this module."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_vix": 20}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=None) is True


def test_max_vix_off_ignores_vix_entirely():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "standard"}}})
    assert passes_entry_filter("standard", "CHOPPY", ef, vix=99.0) is True


def test_bear_regime_already_excludable_via_exclude_regimes_no_new_code_needed():
    """BEAR needs no max_vix support - dominant_regime is a plain string, so
    exclude_regimes: [BEAR] (or [BULL, BEAR]) already works via the existing
    membership check."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"exclude_regimes": ["BULL", "BEAR"]}}})
    assert passes_entry_filter("strong", "BEAR", ef) is False
    assert passes_entry_filter("strong", "BULL", ef) is False
    assert passes_entry_filter("strong", "CHOPPY", ef) is True


def test_combined_regime_and_vix_gate_matches_the_analysis_recommendation():
    """docs/beating_the_market_analysis_2026-07-28.md sec 2.4/6: exclude BULL
    AND high-VIX days, together."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {
        "min_tier": "standard", "exclude_regimes": ["BULL"], "max_vix": 25}}})
    assert passes_entry_filter("standard", "CHOPPY", ef, vix=18.0) is True
    assert passes_entry_filter("standard", "BULL", ef, vix=18.0) is False    # regime fails
    assert passes_entry_filter("standard", "CHOPPY", ef, vix=30.0) is False  # vix fails
    assert passes_entry_filter("weak", "CHOPPY", ef, vix=18.0) is False      # tier fails


# --- max_realized_vol_pct (2026-07-28, sec 7's follow-up to the failed
# BEAR/VIX gate: exclude_regimes=[BEAR]+max_vix=25 still let all of 2022
# through mislabeled CHOPPY - this checks a price-derived volatility number
# that never touches dominant_regime or the fake F&G/AD-ratio inputs) -------

def test_max_realized_vol_default_is_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "standard"}}})
    assert ef["max_realized_vol_pct"] is None


def test_max_realized_vol_reads_configured_value():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_realized_vol_pct": 25}}})
    assert ef["max_realized_vol_pct"] == 25.0


def test_max_realized_vol_malformed_falls_back_to_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_realized_vol_pct": "loud"}}})
    assert ef["max_realized_vol_pct"] is None


def test_max_realized_vol_rejects_above_threshold_only():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_realized_vol_pct": 25}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, realized_vol_pct_value=24.9) is True
    assert passes_entry_filter("strong", "CHOPPY", ef, realized_vol_pct_value=25.1) is False


def test_max_realized_vol_missing_data_never_rejects():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"max_realized_vol_pct": 20}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, realized_vol_pct_value=None) is True


def test_max_realized_vol_independent_of_regime_and_vix():
    """The whole point: this can reject a CHOPPY-labeled, low-VIX day purely
    on price-derived volatility - something exclude_regimes/max_vix cannot
    do, which is exactly the gap sec 7 found."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {
        "exclude_regimes": ["BULL"], "max_vix": 100, "max_realized_vol_pct": 25}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=15.0, realized_vol_pct_value=30.0) is False


# --- realized_vol_pct (pure price-derived, no regime_engine involvement) ---

def _spy_bars(closes):
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=len(closes), freq="D"),
        "close": closes,
    })


def test_realized_vol_none_with_too_little_history():
    df = _spy_bars([100.0])
    assert realized_vol_pct(df, lookback_days=20) is None


def test_realized_vol_zero_for_a_flat_series():
    df = _spy_bars([100.0] * 25)
    rv = realized_vol_pct(df, lookback_days=20)
    assert rv is not None
    assert abs(rv) < 1e-9


def test_realized_vol_positive_for_a_choppy_series():
    import itertools
    vals = list(itertools.islice(itertools.cycle([100.0, 105.0, 97.0, 103.0]), 25))
    df = _spy_bars(vals)
    rv = realized_vol_pct(df, lookback_days=20)
    assert rv is not None and rv > 0


def test_realized_vol_higher_for_more_volatile_series():
    calm = _spy_bars([100 + (i % 2) * 0.5 for i in range(25)])
    wild = _spy_bars([100 + (i % 2) * 15 for i in range(25)])
    rv_calm = realized_vol_pct(calm, lookback_days=20)
    rv_wild = realized_vol_pct(wild, lookback_days=20)
    assert rv_wild > rv_calm


# --- min_spy_trend_pct (2026-07-28 sec 7d follow-up: the ONE conceptually
# different idea left after regime label, VIX, and realized vol all failed
# to protect 2022 - a slow-moving 200-day trend read, not another same-day
# threshold) --------------------------------------------------------------

def test_min_spy_trend_default_is_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_tier": "standard"}}})
    assert ef["min_spy_trend_pct"] is None


def test_min_spy_trend_reads_zero_as_a_real_active_value():
    """0 (require SPY at/above its 200-SMA) must NOT be treated as 'off' -
    it's a legitimate, likely-to-be-used threshold, unlike falsy-as-off."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": 0}}})
    assert ef["min_spy_trend_pct"] == 0.0


def test_min_spy_trend_reads_negative_cushion_value():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": -5}}})
    assert ef["min_spy_trend_pct"] == -5.0


def test_min_spy_trend_malformed_falls_back_to_off():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": "up"}}})
    assert ef["min_spy_trend_pct"] is None


def test_min_spy_trend_rejects_below_threshold_only():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": 0}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, spy_trend_pct_value=0.5) is True   # above SMA
    assert passes_entry_filter("strong", "CHOPPY", ef, spy_trend_pct_value=0.0) is True   # exactly at SMA
    assert passes_entry_filter("strong", "CHOPPY", ef, spy_trend_pct_value=-0.1) is False  # below SMA


def test_min_spy_trend_missing_data_never_rejects():
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": 0}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, spy_trend_pct_value=None) is True


def test_min_spy_trend_independent_of_regime_vix_and_realized_vol():
    """This is the whole point: can reject a CHOPPY/low-VIX/low-realized-vol
    day purely on being in a sustained downtrend below its 200-SMA."""
    ef = entry_filter_from_cfg({"backtest": {"entry_filter": {"min_spy_trend_pct": 0}}})
    assert passes_entry_filter("strong", "CHOPPY", ef, vix=12.0, realized_vol_pct_value=8.0,
                               spy_trend_pct_value=-10.0) is False


# --- spy_trend_pct (pure price-derived, no regime_engine involvement) ------

def test_spy_trend_none_with_under_200_days():
    df = _spy_bars([100.0] * 199)
    assert spy_trend_pct(df) is None


def test_spy_trend_zero_for_a_flat_series():
    df = _spy_bars([100.0] * 250)
    assert abs(spy_trend_pct(df)) < 1e-9


def test_spy_trend_positive_when_price_above_its_200sma():
    # flat at 100 for the SMA window, then a recent run-up
    df = _spy_bars([100.0] * 200 + [110.0] * 20)
    trend = spy_trend_pct(df)
    assert trend is not None and trend > 0


def test_spy_trend_negative_when_price_below_its_200sma():
    df = _spy_bars([100.0] * 200 + [80.0] * 20)
    trend = spy_trend_pct(df)
    assert trend is not None and trend < 0
