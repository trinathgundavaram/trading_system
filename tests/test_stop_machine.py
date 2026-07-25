"""T-4 - the initial stop must scale with ATR, never collapse to a fixed floor.

THE BUG THIS EXISTS TO CATCH
----------------------------
Until 2026-07-20, calculate() clamped the initial stop with

    floor = max(entry - atr*1.5, entry*0.9925, entry - 0.50)
    initial = max(initial, floor)

These are PRICES, so max() picks whichever candidate is CLOSEST to entry - the
tightest stop, not the widest. `entry*0.9925` (0.75%) or `entry - 0.50` was
almost always tightest, so the tiered atr_multiplier design above it (2.0/1.5/
1.2x ATR for strong/standard/weak setups) collapsed to the same ~0.75% stop
regardless of score or volatility. Positions had no room to work before normal
noise stopped them out: 14 of 28 paper exits were premature stop-outs averaging
-1.05%, and it is the single largest contributor to the -0.75% expectancy the
evaluation measured.

§12 makes the point that one test asserting the stop scales with ATR would have
caught this the day it was written. This is that test.

WHY THESE PARTICULAR PRICES
---------------------------
The bug was invisible at $15 and glaring at $237. At entry $1.50 the $0.50 term
is 33% - never the tightest, so max() ignores it and the ATR distance survives.
At entry $614.25 (SMFL, a real holding) it is 0.08%, which beats every ATR
distance and pins the stop 8 basis points below entry. Parametrising across two
and a half orders of magnitude is what turns "the stop looks fine" into a test.
ODFL's $237.16 and SMFL's $614.25 are the real rows from the evaluation.

    python3 -m pytest tests/test_stop_machine.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.stop_state_machine import StopState, calculate

CFG = {
    "risk_level": "TURBO",
    "risk": {"TURBO": {"stop_loss_swing_pct": 8, "stop_loss_day_pct": 4}},
    "stop_machine": {
        "SWING": {"atr_multiplier_strong": 2.0, "atr_multiplier_standard": 1.5,
                  "atr_multiplier_weak": 1.2, "breakeven_r": 0.5,
                  "breakeven_lock_r": 0.05, "profit_protect_r": 1.0,
                  "profit_protect_lock_r": 0.25,
                  "profit_protect_trail_atr_mult": 1.5,
                  "trend_trail_r": 2.0, "trend_trail_atr_mult": 1.0},
        "DAY": {"atr_multiplier_strong": 1.5, "atr_multiplier_standard": 1.2,
                "atr_multiplier_weak": 1.0, "breakeven_r": 0.3,
                "breakeven_lock_r": 0.05, "profit_protect_r": 1.0,
                "profit_protect_lock_r": 0.25,
                "profit_protect_trail_atr_mult": 1.5,
                "trend_trail_r": 2.0, "trend_trail_atr_mult": 1.0},
    },
}

# Real prices from the 2026-07-24 evaluation. ODFL and SMFL are the two that
# made the floor bug visible.
ENTRIES = [1.50, 15.00, 87.00, 237.16, 614.25]
ATR_PCTS = [0.5, 1.5, 3.0, 6.0]


def _fresh(entry, score=75, mode="SWING"):
    """A position with no stop history - so calculate() returns INITIAL_RISK
    and the stage ratchet (S-1) has nothing to floor."""
    return {"entry_price": entry, "shares": 1.0, "trade_mode": mode,
            "entry_signal_score": score, "stop_state": None,
            "current_stop_price": None}


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("atr_pct", ATR_PCTS)
def test_initial_stop_is_atr_proportional(entry, atr_pct):
    atr = entry * atr_pct / 100
    stop = calculate(_fresh(entry), {"price": entry, "atr": atr}, 20.0, CFG)
    dist_pct = (entry - stop.stop_price) / entry * 100
    cap = CFG["risk"]["TURBO"]["stop_loss_swing_pct"]

    assert stop.state is StopState.INITIAL_RISK
    assert stop.stop_price < entry, "stop must be below entry"
    # 0.8x rather than 1.0x leaves room for the risk-level cap to bind at the
    # wide end without the assertion becoming vacuous at the narrow end.
    assert dist_pct >= min(atr_pct * 0.8, cap), \
        f"stop is {dist_pct:.3f}% from entry on {atr_pct}% ATR - it is not " \
        f"scaling with volatility, which is the T-4 floor bug"
    assert dist_pct <= cap + 1e-9, "stop must respect the risk-level cap"


@pytest.mark.parametrize("entry", ENTRIES)
def test_stop_distance_grows_with_volatility(entry):
    """The ordering property, which a fixed floor destroys outright: a more
    volatile stock gets a wider stop. Under the old clamp every one of these
    came back at ~0.75% and this test would fail on the first comparison."""
    dists = []
    for atr_pct in [0.5, 1.0, 2.0]:
        atr = entry * atr_pct / 100
        stop = calculate(_fresh(entry), {"price": entry, "atr": atr}, 20.0, CFG)
        dists.append((entry - stop.stop_price) / entry * 100)
    assert dists[0] < dists[1] < dists[2], \
        f"stop distances {dists} are not increasing with ATR"


@pytest.mark.parametrize("entry", ENTRIES)
def test_entry_score_tiers_are_distinguishable(entry):
    """A strong setup earns more room than a weak one. The tiered multipliers
    existed before T-4 too - the floor made them indistinguishable, which is
    why the bug survived review."""
    atr = entry * 2.0 / 100
    weak = calculate(_fresh(entry, score=60), {"price": entry, "atr": atr}, 20.0, CFG)
    standard = calculate(_fresh(entry, score=75), {"price": entry, "atr": atr}, 20.0, CFG)
    strong = calculate(_fresh(entry, score=90), {"price": entry, "atr": atr}, 20.0, CFG)
    assert weak.stop_price > standard.stop_price > strong.stop_price, \
        "tighter stop for weaker setups - the tiers are collapsed"


def test_the_smfl_row_cannot_recur():
    """SMFL was found with current_stop_price == entry_price exactly
    ($614.2501 / $614.2501). A zero-distance stop is either an instant exit or
    a silently ignored one; it is never a stop."""
    entry = 614.2501
    for atr_pct in ATR_PCTS:
        stop = calculate(_fresh(entry), {"price": entry, "atr": entry * atr_pct / 100},
                         20.0, CFG)
        assert stop.stop_price != entry
        assert (entry - stop.stop_price) >= 0.50, \
            "a sub-50c stop on a $614 stock is the T-4 floor, back again"


def test_missing_atr_still_produces_a_sane_stop():
    """calculate() falls back to atr = entry * 0.015 when ticker_data has no
    ATR. §5's audit called out a missing ATR as the suspected cause of the
    zero-distance stop, so the fallback needs its own assertion rather than
    being assumed."""
    for entry in ENTRIES:
        for td in ({"price": entry}, {"price": entry, "atr": 0}, {"price": entry, "atr": None}):
            stop = calculate(_fresh(entry), td, 20.0, CFG)
            dist_pct = (entry - stop.stop_price) / entry * 100
            assert 0 < dist_pct <= CFG["risk"]["TURBO"]["stop_loss_swing_pct"]


@pytest.mark.parametrize("entry", ENTRIES)
def test_day_positions_are_capped_tighter_than_swing(entry):
    """A DAY position is flattened at the bell, so it must never carry
    swing-sized risk. Uses a large ATR so both hit their caps."""
    atr = entry * 6.0 / 100
    day = calculate(_fresh(entry, mode="DAY"), {"price": entry, "atr": atr}, 20.0, CFG)
    swing = calculate(_fresh(entry, mode="SWING"), {"price": entry, "atr": atr}, 20.0, CFG)
    day_pct = (entry - day.stop_price) / entry * 100
    swing_pct = (entry - swing.stop_price) / entry * 100
    assert day_pct <= CFG["risk"]["TURBO"]["stop_loss_day_pct"] + 1e-9
    assert day_pct < swing_pct
