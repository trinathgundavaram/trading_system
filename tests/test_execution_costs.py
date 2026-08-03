"""engine/execution_costs.py (2026-07-28) - the slippage/spread haircut added
after the 2542-trade/PF-1.14 backtest review (Trinath's directive: "add
execution costs now... the current edge is not large enough to ignore
friction"). Pins:

  1. default-off: no cfg, an empty cfg, or backtest.execution_cost_bps<=0/
     missing/malformed all behave identically to no cost model at all - this
     is what makes shipping the feature safe for every existing/automatic
     backtest run that doesn't opt in.
  2. the round-trip bps is split evenly across entry (haircut up) and exit
     (haircut down), and scales linearly with size.
  3. wired into simulate_forward_exit/_exit_result: outcome_pct/outcome_r
     drop when a cost is configured, but risk_pct/risk_per_share (the stop
     machine's own risk framing) stay on the RAW entry price, unaffected by
     the fill-cost assumption.
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

from engine.execution_costs import apply_execution_costs, get_round_trip_bps  # noqa: E402
from engine.backtest_engine import simulate_forward_exit  # noqa: E402


# --- get_round_trip_bps -----------------------------------------------------

def test_default_is_off_with_no_cfg():
    assert get_round_trip_bps(None) == 0.0
    assert get_round_trip_bps({}) == 0.0


def test_off_when_backtest_section_present_but_key_absent():
    assert get_round_trip_bps({"backtest": {"months": 36}}) == 0.0


def test_reads_configured_value():
    assert get_round_trip_bps({"backtest": {"execution_cost_bps": 25}}) == 25.0


def test_negative_or_malformed_falls_back_to_off():
    assert get_round_trip_bps({"backtest": {"execution_cost_bps": -10}}) == 0.0
    assert get_round_trip_bps({"backtest": {"execution_cost_bps": "not-a-number"}}) == 0.0
    assert get_round_trip_bps({"backtest": {"execution_cost_bps": None}}) == 0.0


# --- apply_execution_costs ---------------------------------------------------

def test_apply_is_a_no_op_when_off():
    entry, exit_ = apply_execution_costs(100.0, 110.0, {})
    assert entry == 100.0 and exit_ == 110.0


def test_apply_splits_round_trip_evenly_and_scales_linearly():
    # 20bps round-trip -> 10bps each way: entry up 0.10%, exit down 0.10%.
    entry, exit_ = apply_execution_costs(100.0, 110.0, {"backtest": {"execution_cost_bps": 20}})
    assert abs(entry - 100.10) < 1e-9, entry
    assert abs(exit_ - 109.89) < 1e-9, exit_
    # Doubling bps doubles the haircut on each side.
    entry2, exit2 = apply_execution_costs(100.0, 110.0, {"backtest": {"execution_cost_bps": 40}})
    assert abs((entry2 - 100.0) - 2 * (entry - 100.0)) < 1e-9
    assert abs((110.0 - exit2) - 2 * (110.0 - exit_)) < 1e-9


# --- wired into the replay ---------------------------------------------------

CFG_BASE = {
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
    return pd.DataFrame(
        [{"date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
          "open": o, "high": h, "low": lo, "close": c, "volume": 1e6}
         for i, (o, h, lo, c) in enumerate(rows)]
    )


def _run(rows, cfg, atr=2.0, score=70.0, r_multiple=3.0, max_hold=20):
    bars = _bars(rows)
    return simulate_forward_exit(bars, 0, float(bars.iloc[0]["close"]), atr, score,
                                 stop_loss_swing_pct=8, r_multiple=r_multiple,
                                 max_hold_days=max_hold, cfg=cfg, atrs=[atr] * len(bars))


# Same take-profit scenario as test_backtest_exit_replay.py's
# test_take_profit_still_fires_at_the_r_multiple: entry 100, risk 3,
# r_multiple 3 -> raw target 109 -> raw outcome_pct = +9.0%.
_TP_ROWS = [(100, 100, 100, 100), (100, 110, 99, 109.5)]


def test_no_cost_config_matches_the_existing_frictionless_result():
    res = _run(_TP_ROWS, CFG_BASE)
    assert res["execution_cost_bps"] == 0.0
    assert abs(res["outcome_pct"] - 9.0) < 1e-6, res


def test_cost_config_reduces_outcome_pct_and_outcome_r():
    cfg = dict(CFG_BASE, backtest={"execution_cost_bps": 30})
    baseline = _run(_TP_ROWS, CFG_BASE)
    costed = _run(_TP_ROWS, cfg)
    assert costed["execution_cost_bps"] == 30.0
    assert costed["outcome_pct"] < baseline["outcome_pct"], (costed, baseline)
    assert costed["outcome_r"] < baseline["outcome_r"], (costed, baseline)
    # 15bps haircut each side on a 100 -> 109 move: exit reduced by
    # 109 * 0.0015 = 0.1635, entry raised by 100 * 0.0015 = 0.15.
    expected_pct = ((109.0 * (1 - 0.0015)) / (100.0 * (1 + 0.0015)) - 1) * 100
    assert abs(costed["outcome_pct"] - round(expected_pct, 2)) < 1e-6, costed


def test_risk_pct_is_unaffected_by_the_cost_model():
    """The stop machine's risk framing (risk_pct/risk_per_share, and the
    denominator under outcome_r) must not move just because a fill-cost
    assumption was turned on - only the P&L numerator should change."""
    cfg = dict(CFG_BASE, backtest={"execution_cost_bps": 30})
    baseline = _run(_TP_ROWS, CFG_BASE)
    costed = _run(_TP_ROWS, cfg)
    assert costed["risk_pct"] == baseline["risk_pct"], (costed, baseline)


def test_raw_exit_price_field_is_unchanged_by_cost_model():
    """exit_price still records the actual bar-triggered level (the stop/
    target price), not the cost-adjusted fill - only outcome_pct/outcome_r
    are cost-adjusted. See _exit_result's docstring note."""
    cfg = dict(CFG_BASE, backtest={"execution_cost_bps": 30})
    baseline = _run(_TP_ROWS, CFG_BASE)
    costed = _run(_TP_ROWS, cfg)
    assert costed["exit_price"] == baseline["exit_price"] == 109.0
