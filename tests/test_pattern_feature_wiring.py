"""engine/pattern_features.py records REAL adx/cmf/sector RS/squeeze/
unusual-options/opex, not the constants it recorded before 2026-07-26.

WHY THIS FILE EXISTS, and what it is deliberately NOT testing.

The defect these tests pin was not a broken function. `ticker_to_dict` was
correct, `_calc_indicators` was correct, `get_sector_return` was correct -
every producer of these values worked, and had working tests. The bug was
that `build_pattern_features` never asked any of them, and kept writing
0.0/False/"normal" long after the real sources landed. A test suite made
entirely of "does the helper compute the right number" says nothing about
that, which is exactly how it survived several sessions of review.

So the assertions below are about PLUMBING - does a real value at the input
end arrive at the output end - and one of them (test_scheduler_passes_the_
dicts) reads the call site itself rather than any behaviour, because the
failure mode being guarded is a caller that stops passing an argument while
every function involved stays green.

The second class of assertion is about what must stay honest: the remaining
placeholders are asserted to STILL be placeholders. If someone wires a real
VIX percentile later, that test failing is the reminder to update the
docstring and README rather than let a new REAL field masquerade as one of
the documented-fake ones.
"""
import inspect
import re
from pathlib import Path

import pytest

from engine.pattern_features import build_pattern_features
from learning.pattern_database import CATEGORICAL_FEATURES, NUMERIC_FEATURES

REPO = Path(__file__).resolve().parents[1]


class _TD:
    """Minimal TickerData stand-in - only the attributes this module reads."""
    price = 100.0
    change_pct = 1.5
    volume_ratio = 1.8
    rsi = 55.0
    bb_pct = 0.6
    stoch_k = 62.0
    macd_crossover_direction = "bullish"
    technical_rating = "buy"
    analyst_rating = "buy"
    insider_net_direction = "buying"
    sector = "Technology"
    adx = 31.4
    cmf = 0.22
    squeeze_active = True
    unusual_options_bullish = True


class _MKT:
    vix_level = 17.2
    fear_greed_score = 61
    fear_greed_rating = "greed"


class _BUY:
    should_buy = True
    pct_score = 71.0

    class _Sig:
        name = "breakout"

    top_signals = [_Sig()]
    rules_passed = [_Sig()]


TICKER_DICT = {
    "adx": 31.4,
    "cmf": 0.22,
    "sector_rs_1d": 0.83,
    "sector_rs_1m": -2.41,
    "squeeze_active": True,
    "unusual_options_bullish": True,
}
MARKET_DICT = {"opex_status": "opex_week"}


def _build(**kw):
    return build_pattern_features("AAPL", _TD(), _MKT(), _BUY(), {}, **kw)


# ── the plumbing ────────────────────────────────────────────────────────────

def test_real_values_arrive_when_the_dicts_are_passed():
    f = _build(ticker_dict=TICKER_DICT, market_dict=MARKET_DICT)
    assert f["adx"] == pytest.approx(31.4)
    assert f["cmf"] == pytest.approx(0.22)
    assert f["sector_rs_1d"] == pytest.approx(0.83)
    assert f["sector_rs_1m"] == pytest.approx(-2.41)
    assert f["squeeze_active"] is True
    assert f["unusual_options"] is True
    assert f["opex_status"] == "opex_week"


def test_a_negative_sector_rs_is_not_flattened_to_zero():
    """sector_rs_1m is legitimately negative most of the time. The old code
    wrote 0.0 for it, which is not a missing value - it is the specific claim
    'this sector exactly matched SPY', i.e. the single most neutral reading
    available. A falsy-value bug here would silently restore that."""
    f = _build(ticker_dict={**TICKER_DICT, "sector_rs_1m": -2.41}, market_dict=MARKET_DICT)
    assert f["sector_rs_1m"] < 0


def test_a_genuine_zero_reading_is_preserved_not_treated_as_absent():
    """ADX of 0.0 is a real (if rare) reading. `or`-style defaulting would
    turn it into the fallback path; `is None` checking does not."""
    f = _build(ticker_dict={**TICKER_DICT, "adx": 0.0}, market_dict=MARKET_DICT)
    assert f["adx"] == 0.0


def test_falls_back_to_tickerdata_when_no_dicts_passed():
    """confirm_fill.py and any older caller must keep working, and should
    still get the real values off the dataclass rather than the constants."""
    f = _build()
    assert f["adx"] == pytest.approx(31.4)
    assert f["cmf"] == pytest.approx(0.22)
    assert f["squeeze_active"] is True
    assert f["opex_status"] == "normal"  # no market_dict -> documented default


def test_unusual_options_outage_is_none_not_false():
    """None means 'the scanner did not answer', which is a different cohort
    from 'no unusual flow'. Collapsing the two would pool an outage with a
    quiet tape in every similarity query."""
    td = _TD()
    td.unusual_options_bullish = None
    f = build_pattern_features("AAPL", td, _MKT(), _BUY(), {},
                               ticker_dict={"unusual_options_bullish": None},
                               market_dict=MARKET_DICT)
    assert f["unusual_options"] is None


def test_a_malformed_value_degrades_instead_of_raising():
    """These dicts are assembled from live MCP payloads. A string where a
    float belongs must not take down the pattern write - the signal is worth
    more than the one feature."""
    f = _build(ticker_dict={**TICKER_DICT, "adx": "n/a"}, market_dict=MARKET_DICT)
    assert f["adx"] == 0.0


# ── the call site (this is the assertion the old suite was missing) ─────────

def test_scheduler_passes_the_dicts():
    """Asserts PLACEMENT, not behaviour. Everything above can pass while the
    only production caller quietly omits both arguments - which is the exact
    shape of the bug being fixed. Reads scheduler.py's source because there
    is no way to observe this from the function under test."""
    src = (REPO / "scheduler.py").read_text()
    call = re.search(r"features = build_pattern_features\((.*?)\)\n", src, re.S)
    assert call, "the build_pattern_features call site moved or was renamed"
    args = call.group(1)
    assert "ticker_dict=" in args, "scheduler stopped passing ticker_dict"
    assert "market_dict=" in args, "scheduler stopped passing market_dict"


def test_both_new_arguments_are_optional():
    """Keeps the fallback path a contract rather than an accident."""
    params = inspect.signature(build_pattern_features).parameters
    for name in ("ticker_dict", "market_dict"):
        assert params[name].default is None


# ── the honesty guarantees ──────────────────────────────────────────────────

def test_every_declared_feature_is_actually_produced():
    """The schema and the producer drifting apart is how these fields went
    stale in the first place - a feature listed in pattern_database.py but
    never written just reads as an absent key at query time."""
    f = _build(ticker_dict=TICKER_DICT, market_dict=MARKET_DICT)
    missing = [k for k in NUMERIC_FEATURES + CATEGORICAL_FEATURES if k not in f]
    assert not missing, f"declared but never written: {missing}"


def test_the_remaining_placeholders_are_still_placeholders():
    """If one of these becomes real, this test failing is the prompt to move
    it out of the placeholder block and update README.md's list - so the docs
    cannot drift behind the code the way they just did."""
    f = _build(ticker_dict=TICKER_DICT, market_dict=MARKET_DICT)
    for key in ("vix_percentile_1y", "vix_percentile_3m",
                "gap_pct", "premarket_gap", "premarket_rvol"):
        assert f[key] == 0.0, (
            f"{key} is no longer a constant - if it is now real, move it out "
            "of the placeholder block in engine/pattern_features.py and out "
            "of README.md's placeholder list"
        )
