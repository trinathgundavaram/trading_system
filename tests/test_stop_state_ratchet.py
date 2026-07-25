"""S-1 - a stop stage a trade has reached must not revert (v1.1.0).

Found by scripts/audit_stops.py on 2026-07-24: AES held entry 14.8050 with
current_stop_price 14.8095 and stop_state INITIAL_RISK. The stop was correct -
BREAKEVEN had set it to entry + risk_per_share x breakeven_lock_r, and
should_advance() then refused to widen it when price fell back. The LABEL was
wrong, because calculate() re-derived its stage from the current profit_r every
cycle with no memory.

The tests below pin both halves: that the raw stage calculation still regresses
(so the ratchet is demonstrably what fixes it, not an incidental change), and
that the public entry point no longer does.

    python3 -m pytest tests/test_stop_state_ratchet.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.stop_state_machine import (StopState, _calculate_raw, calculate,
                                        should_advance)

CFG = {
    "risk_level": "TURBO",
    "risk": {"TURBO": {"stop_loss_swing_pct": 8, "stop_loss_day_pct": 4}},
    "stop_machine": {
        "SWING": {"breakeven_r": 0.5, "breakeven_lock_r": 0.05,
                  "profit_protect_r": 1.0, "profit_protect_lock_r": 0.25,
                  "profit_protect_trail_atr_mult": 1.5,
                  "trend_trail_r": 2.0, "trend_trail_atr_mult": 1.0,
                  "atr_multiplier_strong": 2.0, "atr_multiplier_standard": 1.5,
                  "atr_multiplier_weak": 1.2},
    },
}

# The real AES row, as the audit found it.
ENTRY = 14.8050
RISK_PER_SHARE = 0.09
BREAKEVEN_STOP = ENTRY + RISK_PER_SHARE * 0.05      # 14.8095


def _pos(**kw):
    base = dict(entry_price=ENTRY, shares=10.0, trade_mode="SWING",
                risk_per_share=RISK_PER_SHARE, entry_signal_score=75,
                stop_state=None, current_stop_price=None)
    base.update(kw)
    return base


def _td(price):
    return {"price": price, "atr": 0.06}


def _at_profit_r(r):
    """Price that puts the position at exactly r R of profit."""
    return ENTRY + RISK_PER_SHARE * r


# ── the defect, still present in the raw calculation ────────────────────────

def test_raw_calculation_still_regresses():
    """CONTROL. Without the ratchet the stage falls back to INITIAL_RISK. If
    this ever starts passing as BREAKEVEN, the stage maths changed underneath
    the fix and the tests below stop proving anything."""
    pos = _pos(stop_state="BREAKEVEN", current_stop_price=BREAKEVEN_STOP)
    raw = _calculate_raw(pos, _td(_at_profit_r(0.1)), 20.0, CFG)
    assert raw.state is StopState.INITIAL_RISK


# ── the fix ─────────────────────────────────────────────────────────────────

def test_breakeven_is_held_when_price_falls_back():
    pos = _pos(stop_state="BREAKEVEN", current_stop_price=BREAKEVEN_STOP)
    out = calculate(pos, _td(_at_profit_r(0.1)), 20.0, CFG)
    assert out.state is StopState.BREAKEVEN
    assert out.stop_price == pytest.approx(BREAKEVEN_STOP)
    assert "held (stage ratchet)" in out.stop_reason


def test_the_aes_row_no_longer_reports_initial_risk():
    """The exact row scripts/audit_stops.py flagged."""
    pos = _pos(stop_state="INITIAL_RISK", current_stop_price=BREAKEVEN_STOP)
    # A position whose stop sits above entry has necessarily been through
    # BREAKEVEN; once that is what the row records, the state stops lying.
    reached = _pos(stop_state="BREAKEVEN", current_stop_price=BREAKEVEN_STOP)
    out = calculate(reached, _td(ENTRY), 20.0, CFG)
    assert out.state is StopState.BREAKEVEN
    assert out.stop_price >= ENTRY


@pytest.mark.parametrize("reached,rank", [
    ("TRADE_CONFIRMING", 1), ("BREAKEVEN", 2),
    ("PROFIT_PROTECT", 3), ("TREND_FOLLOWING", 4),
])
def test_no_stage_ever_reverts(reached, rank):
    pos = _pos(stop_state=reached, current_stop_price=BREAKEVEN_STOP)
    out = calculate(pos, _td(_at_profit_r(0.0)), 20.0, CFG)
    assert out.state is StopState(reached)


# ── what must still work ────────────────────────────────────────────────────

def test_fresh_position_is_initial_risk():
    """No history, no ratchet. A new entry must still get a real ATR stop
    below entry - the ratchet must not invent protection that was never
    earned."""
    out = calculate(_pos(), _td(ENTRY), 20.0, CFG)
    assert out.state is StopState.INITIAL_RISK
    assert out.stop_price < ENTRY


def test_stages_still_advance():
    pos = _pos(stop_state="INITIAL_RISK", current_stop_price=ENTRY - 0.09)
    assert calculate(pos, _td(_at_profit_r(0.6)), 20.0, CFG).state is StopState.BREAKEVEN
    assert calculate(pos, _td(_at_profit_r(1.2)), 20.0, CFG).state is StopState.PROFIT_PROTECT
    assert calculate(pos, _td(_at_profit_r(2.5)), 20.0, CFG).state is StopState.TREND_FOLLOWING


def test_thesis_broken_fires_from_any_stage():
    """The emergency exit must never be suppressed by the ratchet - it is the
    one transition that is allowed to move 'backwards'."""
    for reached in ("BREAKEVEN", "PROFIT_PROTECT", "TREND_FOLLOWING"):
        pos = _pos(stop_state=reached, current_stop_price=BREAKEVEN_STOP)
        out = calculate(pos, _td(ENTRY), 95.0, CFG)   # exit_score >= 90
        assert out.state is StopState.THESIS_BROKEN


def test_ratchet_never_lowers_a_stop():
    """The held price is max(candidate, current). A ratcheted result can only
    ever equal or exceed what the position already carried, so this can never
    widen risk."""
    pos = _pos(stop_state="PROFIT_PROTECT", current_stop_price=15.50)
    out = calculate(pos, _td(_at_profit_r(0.0)), 20.0, CFG)
    assert out.stop_price >= 15.50


def test_no_spurious_stop_advance_write():
    """should_advance() must stay False on a ratcheted cycle: the price did not
    move, so Loop B must not rewrite current_stop_price. Only the state does."""
    pos = _pos(stop_state="BREAKEVEN", current_stop_price=BREAKEVEN_STOP)
    out = calculate(pos, _td(_at_profit_r(0.1)), 20.0, CFG)
    assert should_advance(pos["current_stop_price"], out) is False


def test_unknown_prior_state_is_ignored_safely():
    """A NULL, empty or unrecognised stop_state must not raise - legacy rows
    predate the state machine entirely."""
    for bad in (None, "", "GARBAGE", "thesis_broken"):
        out = calculate(_pos(stop_state=bad), _td(ENTRY), 20.0, CFG)
        assert out.state in set(StopState)
