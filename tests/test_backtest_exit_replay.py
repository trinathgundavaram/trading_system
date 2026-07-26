"""Bar-by-bar stop-machine replay in engine/backtest_engine.py (2026-07-26).

The Stage 1 backtest used to hold ONE stop price for a whole 20-day hold while
production ran engine/stop_state_machine.py's ratcheting 6-state stop on every
cycle. Once the scoring-ceiling fix let trades flow, that gap turned out to
dominate the result: 213 of 302 trades reached breakeven_r (0.5R) and 123 of
those still recorded a full stop-out, because nothing ever moved the stop up.

These tests pin the three properties that make the replay trustworthy:
  1. it uses the REAL stop machine (not a copy of its multipliers),
  2. the stop ratchets and never widens,
  3. it does not look ahead - the stop in force during a bar is priced off the
     PREVIOUS bar's close.

(3) is the one that matters most. Pricing the stop off the current bar's own
close and then testing that bar's low against it would flatter trailing stops
specifically, which is the exact mechanism under test.
"""
import os
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TP_REQUIRE_REFERENCE_TA", "0")

for m in ["mcp", "mcp.client", "mcp.client.stdio", "mcp.client.streamable_http"]:
    sys.modules.setdefault(m, types.ModuleType(m))


class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()


sys.modules["mcp"].ClientSession = _Any
sys.modules["mcp"].StdioServerParameters = _Any
sys.modules["mcp.client.stdio"].stdio_client = _Any

from engine.backtest_engine import simulate_forward_exit  # noqa: E402
from engine import stop_state_machine as ssm  # noqa: E402


CFG = {
    "risk_level": "TURBO",
    "risk": {"TURBO": {"stop_loss_swing_pct": 8}},
    "stop_machine": {
        "SWING": {
            "atr_multiplier_strong": 2.0, "atr_multiplier_standard": 1.5,
            "atr_multiplier_weak": 1.2, "breakeven_r": 0.5, "breakeven_lock_r": 0.05,
            "profit_protect_r": 1.0, "profit_protect_lock_r": 0.25,
            "profit_protect_trail_atr_mult": 1.5, "trend_trail_r": 2.0,
            "trend_trail_atr_mult": 1.5,
        },
        "atr_spike": {"atr_pct_threshold": 5.0, "multiplier_bonus": 0.4},
    },
}


def _bars(rows):
    """rows: list of (open, high, low, close). Index 0 is the entry bar."""
    return pd.DataFrame(
        [{"date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
          "open": o, "high": h, "low": lo, "close": c, "volume": 1e6}
         for i, (o, h, lo, c) in enumerate(rows)]
    )


def _run(rows, atr=2.0, score=70.0, r_multiple=3.0, max_hold=20):
    bars = _bars(rows)
    return simulate_forward_exit(bars, 0, float(bars.iloc[0]["close"]), atr, score,
                                 stop_loss_swing_pct=8, r_multiple=r_multiple,
                                 max_hold_days=max_hold, cfg=CFG, atrs=[atr] * len(bars))


def test_initial_stop_comes_from_the_real_stop_machine():
    """Not a local copy of the multipliers. Entry 100, ATR 2, score 70 ->
    standard tier 1.5xATR -> stop 97.0, which is inside the 8% cap."""
    pos = {"entry_price": 100.0, "entry_signal_score": 70.0, "trade_mode": "SWING",
           "high_watermark_price": 100.0, "stop_state": None}
    expected = ssm.calculate(pos, {"price": 100.0, "atr": 2.0}, 0.0, CFG).stop_price
    assert abs(expected - 97.0) < 1e-9, expected
    # A trade that gaps straight through it must exit exactly there.
    res = _run([(100, 100, 100, 100), (97, 97, 90, 91)])
    assert res["exit_reason"] == "stop_loss", res
    assert abs(res["exit_price"] - expected) < 1e-9, (res["exit_price"], expected)


def test_breakeven_ratchet_converts_a_full_loss_into_a_scratch():
    """THE regression this replay was built for. Entry 100, risk 3 (stop 97).
    Bar 1 closes at 101.6 = +0.53R, clearing breakeven_r 0.5 -> the stop
    ratchets to entry + 0.05R = 100.15. Bar 2 collapses. Under the old fixed-
    stop model this exited at 97 (-3.0%); it must now exit at 100.15 (+0.15%)."""
    res = _run([(100, 100, 100, 100), (100, 102, 100, 101.6), (101, 101, 90, 90)])
    assert res["exit_price"] > 100.0, res
    assert abs(res["exit_price"] - 100.15) < 1e-6, res
    assert res["outcome_pct"] > 0, res
    # And it must be labelled a trailing stop, not a loss.
    assert res["exit_reason"] == "trailing_stop", res
    assert res["exit_stop_state"] == "BREAKEVEN", res


def test_stop_never_widens_on_a_pullback():
    """Ratchet: once BREAKEVEN is earned at bar 1, a bar-2 pullback that would
    re-derive INITIAL_RISK must not hand the protection back."""
    res = _run([(100, 100, 100, 100),
                (100, 102, 100, 101.6),   # earns breakeven -> stop 100.15
                (101, 101, 100.5, 100.6), # pulls back, still above the stop
                (100, 100, 95, 95)])      # then breaks
    assert abs(res["exit_price"] - 100.15) < 1e-6, res
    assert res["exit_reason"] == "trailing_stop", res


def test_stop_is_priced_off_the_previous_bar_not_the_current_one():
    """No look-ahead. On the bar where price first closes above breakeven_r,
    that bar's OWN low must still be tested against the OLD (initial) stop -
    the ratchet cannot use a close that had not printed when the low did.

    Bar 1 both dips to 96 (below the 97 initial stop) and closes at 101.6
    (which would earn a 100.15 breakeven stop). Correct behaviour is to stop
    out at 97. A look-ahead implementation would survive the bar."""
    res = _run([(100, 100, 100, 100), (100, 102, 96, 101.6), (101, 105, 101, 104)])
    assert res["exit_reason"] == "stop_loss", res
    assert abs(res["exit_price"] - 97.0) < 1e-9, res
    assert res["hold_days"] == 1, res


def test_take_profit_still_fires_at_the_r_multiple():
    """Entry 100, risk 3, r_multiple 3 -> target 109."""
    res = _run([(100, 100, 100, 100), (100, 110, 99, 109.5)])
    assert res["exit_reason"] == "take_profit", res
    assert abs(res["exit_price"] - 109.0) < 1e-9, res


def test_same_bar_stop_and_target_resolves_to_the_stop():
    """Conservative convention, unchanged from the old model."""
    res = _run([(100, 100, 100, 100), (100, 115, 90, 100)])
    assert res["exit_reason"] == "stop_loss", res


def test_time_stop_uses_the_canonical_vocabulary():
    """rules/common.py's EXIT_KINDS calls this 'time_stop'. The old model
    emitted 'time_based_close', which is not a member of that frozenset."""
    from rules.common import EXIT_KINDS
    res = _run([(100, 100, 100, 100)] + [(100, 100.5, 99.5, 100)] * 5, max_hold=5)
    assert res["exit_reason"] == "time_stop", res
    assert res["exit_reason"] in EXIT_KINDS
    for r in ["stop_loss", "trailing_stop", "take_profit"]:
        assert r in EXIT_KINDS


def test_every_exit_reason_is_a_valid_exit_kind():
    """Whatever path a trade takes, its label must be vocabulary-legal."""
    from rules.common import EXIT_KINDS
    scenarios = [
        [(100, 100, 100, 100), (97, 97, 90, 91)],
        [(100, 100, 100, 100), (100, 102, 100, 101.6), (101, 101, 90, 90)],
        [(100, 100, 100, 100), (100, 110, 99, 109.5)],
        [(100, 100, 100, 100)] + [(100, 100.5, 99.5, 100)] * 4,
    ]
    for s in scenarios:
        assert _run(s, max_hold=5)["exit_reason"] in EXIT_KINDS


def test_trend_following_trail_protects_a_large_winner():
    """A trade that runs past trend_trail_r (2.0R) trails 1.5xATR under the
    high watermark, so a sharp reversal keeps most of the gain instead of
    riding back to the initial stop."""
    res = _run([(100, 100, 100, 100),
                (100, 108, 100, 107),   # +2.33R -> TREND_FOLLOWING
                (107, 107, 95, 95)],    # reversal
               r_multiple=99)           # disable the target so the trail is what fires
    assert res["exit_stop_state"] == "TREND_FOLLOWING", res
    assert res["exit_reason"] == "trailing_stop", res
    assert res["exit_price"] >= 104.0, res   # watermark 108 - 1.5*2.0 ATR
    assert res["outcome_pct"] > 3.0, res
