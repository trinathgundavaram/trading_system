"""engine/intraday_execution_costs.py (2026-07-30) - the $/share cost model
for the intraday momentum backtest (docs/
intraday_momentum_scoping_2026-07-30.md phase 1). Pins:
  1. the default is the PAPER's own disclosed cost assumption, not zero -
     the inverse default posture from engine/execution_costs.py, and the
     reason is load-bearing (see module docstring), so it's pinned here.
  2. cost is symmetric across long/short and independent of position size.
  3. get_cost_params degrades to the paper's defaults (not zero, not a
     crash) on a missing/malformed cfg.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.intraday_execution_costs import (  # noqa: E402
    round_trip_cost_per_share, apply_execution_costs, get_cost_params,
    DEFAULT_PER_SHARE_COMMISSION, DEFAULT_PER_SHARE_SLIPPAGE,
)


# --- round_trip_cost_per_share ----------------------------------------------

def test_default_cost_per_fill_matches_the_paper():
    assert round_trip_cost_per_share() == pytest.approx(0.0045)


def test_negative_inputs_clamped_to_zero_not_allowed_to_go_negative():
    assert round_trip_cost_per_share(commission=-1.0, slippage=-1.0) == 0.0


# --- apply_execution_costs ---------------------------------------------------

def test_long_trade_pnl_and_cost():
    r = apply_execution_costs(100.0, 101.0, "long")
    assert r["raw_pnl_per_share"] == pytest.approx(1.0)
    assert r["cost_per_share_round_trip"] == pytest.approx(0.009)   # 2 * 0.0045
    assert r["net_pnl_per_share"] == pytest.approx(0.991)
    assert r["raw_outcome_pct"] == pytest.approx(1.0)
    assert r["net_outcome_pct"] == pytest.approx(0.991)


def test_short_trade_pnl_direction_is_flipped_cost_is_not():
    r = apply_execution_costs(100.0, 99.0, "short")
    assert r["raw_pnl_per_share"] == pytest.approx(1.0)
    assert r["net_pnl_per_share"] == pytest.approx(0.991)


def test_cost_is_symmetric_regardless_of_win_or_loss():
    win = apply_execution_costs(100.0, 101.0, "long")
    loss = apply_execution_costs(100.0, 99.0, "long")
    assert win["cost_per_share_round_trip"] == loss["cost_per_share_round_trip"]


def test_zero_cost_config_reproduces_raw_pnl():
    r = apply_execution_costs(100.0, 101.0, "long", commission=0.0, slippage=0.0)
    assert r["net_pnl_per_share"] == pytest.approx(r["raw_pnl_per_share"])


def test_invalid_direction_raises():
    with pytest.raises(ValueError):
        apply_execution_costs(100.0, 101.0, "sideways")


def test_zero_price_raises_rather_than_dividing_by_zero():
    with pytest.raises(ValueError):
        apply_execution_costs(0.0, 101.0, "long")


# --- get_cost_params ----------------------------------------------------------

def test_get_cost_params_defaults_to_the_papers_assumption_not_zero():
    assert get_cost_params(None) == (DEFAULT_PER_SHARE_COMMISSION, DEFAULT_PER_SHARE_SLIPPAGE)
    assert get_cost_params({}) == (DEFAULT_PER_SHARE_COMMISSION, DEFAULT_PER_SHARE_SLIPPAGE)


def test_get_cost_params_reads_configured_values():
    cfg = {"intraday_backtest": {"commission_per_share": 0.01, "slippage_per_share": 0.02}}
    assert get_cost_params(cfg) == (0.01, 0.02)


def test_get_cost_params_malformed_falls_back_to_default():
    cfg = {"intraday_backtest": {"commission_per_share": "oops", "slippage_per_share": None}}
    assert get_cost_params(cfg) == (DEFAULT_PER_SHARE_COMMISSION, DEFAULT_PER_SHARE_SLIPPAGE)
