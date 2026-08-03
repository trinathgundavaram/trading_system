"""engine/regime_timed_backtest.py (2026-07-28) - the new regime-timing
overlay built after docs/beating_the_market_analysis_2026-07-28.md sec 7g's
full 9-config sweep confirmed every entry-only filter tried this session
failed to beat SPY. The one new mechanism this module adds - force-closing a
position ALREADY OPEN when the market turns, not just blocking new entries -
is worth pinning carefully, since it's the whole point of building this.

These tests use synthetic bars only (no network, no yfinance), following
this session's established pattern (tests/test_backtest_exit_replay.py,
tests/test_equity_curve.py).
"""
import os
import sys
import types
from datetime import date, timedelta

import numpy as np
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

from engine.regime_timed_backtest import (  # noqa: E402
    compute_regime_timeline, _regime_asof_yesterday, simulate_forward_exit_regime_gated,
    run_regime_timed_replay,
)
from engine.backtest_engine import simulate_forward_exit  # noqa: E402


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


def _bars(rows, start="2024-01-01"):
    """rows: list of (open, high, low, close). Index 0 is the entry bar.
    date column is a real datetime.date (not pd.Timestamp) to match
    fetch_daily_bars' own normalization - regime_timed_backtest's regime
    lookups are keyed by date, and comparing a Timestamp against a date
    raises in pandas, so the two must agree here or every gated test would
    be comparing apples to oranges relative to a real run."""
    return pd.DataFrame(
        [{"date": (pd.Timestamp(start) + pd.Timedelta(days=i)).date(),
          "open": o, "high": h, "low": lo, "close": c, "volume": 1e6}
         for i, (o, h, lo, c) in enumerate(rows)]
    )


def _spy_bars(closes, start="2023-01-01"):
    return pd.DataFrame(
        [{"date": (pd.Timestamp(start) + pd.Timedelta(days=i)).date(), "close": c}
         for i, c in enumerate(closes)]
    )


# --- compute_regime_timeline: hysteresis band ------------------------------

def test_defaults_to_risk_on_before_enough_history_for_an_sma():
    spy = _spy_bars([100.0] * 50)  # far fewer than sma_period=200
    timeline = compute_regime_timeline(spy, sma_period=200, buffer_pct=3.0)
    assert all(timeline.values())


def test_flips_risk_off_only_once_price_clears_the_lower_band():
    # Flat SMA=100 for a long warmup, then price craters.
    closes = [100.0] * 210 + [90.0] * 5  # 90 is -10%, clears a 3% lower band
    spy = _spy_bars(closes)
    timeline = compute_regime_timeline(spy, sma_period=200, buffer_pct=3.0)
    dates = spy["date"].tolist()
    # Still risk-on right up to the crash days (SMA hasn't caught up yet, but
    # price was AT the SMA, inside the band).
    assert timeline[dates[209]] is True
    # Once price is 10% below a still-near-100 SMA, must flip off.
    assert timeline[dates[-1]] is False


def test_deadband_does_not_flip_on_a_small_wobble_inside_the_band():
    """Price dips 1% below its SMA (inside a 3% band) then recovers - must
    NOT flip risk-off. This is the whole point of the hysteresis band: a
    raw price-vs-SMA crossing test would flip here, a banded one must not."""
    closes = [100.0] * 205 + [99.0, 98.5, 99.5, 100.5, 101.0]
    spy = _spy_bars(closes)
    timeline = compute_regime_timeline(spy, sma_period=200, buffer_pct=3.0)
    assert all(timeline[d] for d in spy["date"].tolist()[-5:])


def test_flips_back_risk_on_only_once_price_clears_the_upper_band():
    # Long flat warmup, crash below the band, small bounce still inside
    # (must hold risk-off), then a real breakout above the upper band.
    closes = [100.0] * 210 + [85.0] * 10 + [90.0] * 5 + [140.0] * 5
    spy = _spy_bars(closes)
    timeline = compute_regime_timeline(spy, sma_period=200, buffer_pct=3.0)
    dates = spy["date"].tolist()
    assert timeline[dates[219]] is False   # crashed
    assert timeline[dates[224]] is False   # bounce, still inside band, must hold off
    assert timeline[dates[-1]] is True     # real breakout


def test_buffer_zero_reduces_to_a_raw_crossing_test():
    closes = [100.0] * 205 + [99.0, 101.0]
    spy = _spy_bars(closes)
    timeline = compute_regime_timeline(spy, sma_period=200, buffer_pct=0.0)
    dates = spy["date"].tolist()
    assert timeline[dates[-2]] is False  # 99 < sma(~100)
    assert timeline[dates[-1]] is True   # 101 > sma(~100)


# --- _regime_asof_yesterday: no same-day leakage ---------------------------

def test_asof_yesterday_uses_the_prior_days_state_not_todays():
    d0, d1, d2 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
    regime_by_date = {d0: True, d1: False, d2: True}
    sorted_dates = [d0, d1, d2]
    # Querying d1 itself must return d0's state (True), NOT d1's own (False).
    assert _regime_asof_yesterday(sorted_dates, regime_by_date, d1) is True
    # Querying d2 must return d1's state (False), not d2's own (True).
    assert _regime_asof_yesterday(sorted_dates, regime_by_date, d2) is False


def test_asof_yesterday_defaults_risk_on_with_no_prior_history():
    d0 = date(2024, 1, 1)
    regime_by_date = {d0: False}
    assert _regime_asof_yesterday([d0], regime_by_date, date(2023, 1, 1)) is True


def test_asof_yesterday_falls_back_to_the_most_recent_prior_date_on_a_gap():
    """A ticker's bar calendar can have a date SPY didn't trade (unlikely but
    not impossible with data gaps) - must use the most recent SPY date
    strictly before it, not crash or silently return the wrong thing."""
    d0, d2 = date(2024, 1, 1), date(2024, 1, 3)  # no Jan 2 in SPY's own calendar
    regime_by_date = {d0: False, d2: True}
    sorted_dates = [d0, d2]
    assert _regime_asof_yesterday(sorted_dates, regime_by_date, date(2024, 1, 2)) is False
    assert _regime_asof_yesterday(sorted_dates, regime_by_date, date(2024, 1, 4)) is True


# --- simulate_forward_exit_regime_gated ------------------------------------

def _run_gated(rows, regime_by_date=None, spy_dates_sorted=None, atr=2.0, score=70.0,
               r_multiple=3.0, max_hold=20):
    bars = _bars(rows)
    return simulate_forward_exit_regime_gated(
        bars, 0, float(bars.iloc[0]["close"]), atr, score,
        stop_loss_swing_pct=8, r_multiple=r_multiple, max_hold_days=max_hold,
        cfg=CFG, atrs=[atr] * len(bars),
        spy_dates_sorted=spy_dates_sorted, regime_by_date=regime_by_date)


def test_no_regime_data_behaves_identically_to_the_plain_simulator():
    """Zero behavioral drift when the new mechanism is switched off (both
    spy_dates_sorted/regime_by_date left None) - same scenario as
    test_backtest_exit_replay's breakeven-ratchet test, compared directly
    against the original simulate_forward_exit()."""
    rows = [(100, 100, 100, 100), (100, 102, 100, 101.6), (101, 101, 90, 90)]
    bars = _bars(rows)
    plain = simulate_forward_exit(bars, 0, 100.0, 2.0, 70.0, stop_loss_swing_pct=8,
                                  r_multiple=3.0, max_hold_days=20, cfg=CFG, atrs=[2.0] * len(bars))
    gated = _run_gated(rows)
    assert gated["exit_price"] == plain["exit_price"]
    assert gated["exit_reason"] == plain["exit_reason"]
    assert gated["exit_idx"] == plain["exit_idx"]
    assert not gated.get("regime_forced_exit")


def test_regime_flip_force_closes_at_the_next_bars_open_not_its_close():
    """Entry day 0 (risk-on). Day 1's close is what actually triggers the
    regime flip in the timeline (a real bear leg), so day 1 itself must
    still trade under risk-on (asof-yesterday uses day 0's state). Day 2
    is the first day whose PRIOR state (day 1's, now flipped) is risk-off -
    must force-exit at day 2's OPEN, not wait for the stop/target and not
    peek at day 2's close."""
    rows = [
        (100, 100, 100, 100),   # day 0: entry
        (100, 102, 99, 101),    # day 1: healthy bar, well inside stop/target
        (95, 130, 90, 129),     # day 2: would look like a huge winner if the
                                 # stop/target logic ran - must NOT be allowed to,
                                 # the regime exit must fire on the OPEN (95) first.
        (129, 135, 128, 134),   # day 3: irrelevant, must never be reached
    ]
    d0 = date(2024, 1, 1)
    regime_by_date = {d0 + timedelta(days=i): (i < 1) for i in range(4)}  # day0 True, day1+ False
    sorted_dates = sorted(regime_by_date.keys())
    res = _run_gated(rows, regime_by_date=regime_by_date, spy_dates_sorted=sorted_dates)
    assert res["regime_forced_exit"] is True
    assert res["exit_reason"] == "rule_exit"
    assert res["exit_idx"] == 2
    assert abs(res["exit_price"] - 95.0) < 1e-9, res  # day 2's OPEN, not its close (129)


def test_regime_exit_takes_priority_over_a_stop_that_would_also_fire_that_day():
    """Day 2 both flips risk-off (as of day 1's close) AND gaps through the
    stop. The regime exit must win - it's checked first - and the fill must
    be the day's open, not the stop price."""
    rows = [
        (100, 100, 100, 100),
        (100, 101, 99, 100.5),
        (80, 82, 78, 81),   # gaps way below any stop AND is the regime-off day
    ]
    d0 = date(2024, 1, 1)
    regime_by_date = {d0: True, d0 + timedelta(days=1): False, d0 + timedelta(days=2): False}
    sorted_dates = sorted(regime_by_date.keys())
    res = _run_gated(rows, regime_by_date=regime_by_date, spy_dates_sorted=sorted_dates)
    assert res["regime_forced_exit"] is True
    assert abs(res["exit_price"] - 80.0) < 1e-9, res  # day 2's open, not a stop price


def test_regime_exit_is_a_legal_exit_kind():
    from rules.common import EXIT_KINDS
    rows = [(100, 100, 100, 100), (100, 101, 99, 100.5), (95, 96, 94, 95)]
    d0 = date(2024, 1, 1)
    regime_by_date = {d0: True, d0 + timedelta(days=1): False, d0 + timedelta(days=2): False}
    sorted_dates = sorted(regime_by_date.keys())
    res = _run_gated(rows, regime_by_date=regime_by_date, spy_dates_sorted=sorted_dates)
    assert res["exit_reason"] in EXIT_KINDS


# --- run_regime_timed_replay: the entry_filter stacking regression -------
#
# 2026-07-28, same-day: the first version of run_regime_timed_replay never
# applied backtest_engine's existing entry_filter (min_tier/exclude_regimes)
# at all, so a real 44-ticker/5yr sweep quietly regressed to sec 1's
# UNFILTERED baseline (matched its trades/ticker/year almost exactly) while
# being compared in the handoff against sec 7g's FILTERED baseline - a badly
# misleading result that had nothing to do with whether regime timing
# itself works. This test pins the fix: with an entry_filter set in cfg,
# run_regime_timed_replay must actually reject candidates because of it.

def _synthetic_bars(start, end):
    # MUST span the actual (start, end) the caller requested - run_regime_timed_replay
    # fetches from a warmup-padded fetch_start well before the replay's own
    # `start`, and requires >=20 bars before `start` or it skips the ticker
    # ENTIRELY without ever scoring it (2026-07-28: an earlier draft of this
    # helper ignored its start/end args and always returned a fixed
    # "2023-01-01"-anchored range, so start_idx landed at 0 < 20 and every
    # ticker got silently skipped - n_scored stayed 0, which trivially made
    # n_entry_filter_rejected == 0 too, passing this test for the wrong
    # reason). Also deliberately does NOT reseed inside - the caller seeds
    # ONCE before fetching SPY/VIX/every ticker, so each series draws
    # different values off the advancing global RNG state; reseeding
    # identically per call made every series identical and appeared to trip
    # a veto universally.
    dates = pd.bdate_range(start, end)
    n = len(dates)
    rets = np.random.normal(0.0006, 0.015, n)
    mid = n // 2
    rets[mid:mid + min(40, n - mid)] -= 0.02
    price = 100 * np.cumprod(1 + rets)
    high = price * (1 + np.abs(np.random.normal(0, 0.01, n)))
    low = price * (1 - np.abs(np.random.normal(0, 0.01, n)))
    open_ = price * (1 + np.random.normal(0, 0.003, n))
    vol = np.random.randint(1_000_000, 5_000_000, n)
    return pd.DataFrame({"date": dates.date, "open": open_, "high": high, "low": low,
                         "close": price, "volume": vol})


def test_entry_filter_in_cfg_actually_rejects_candidates():
    """The regression this exists to catch: with cfg["backtest"]["entry_filter"]
    set (the same standard-tier/exclude-BULL filter every §7g config used),
    run_regime_timed_replay must reject SOME candidates because of it, and
    the filtered run must produce no more trades than the unfiltered one on
    identical data."""
    from engine import backtest_engine as be
    from config_loader import load_config_dict

    original_fetch = be.fetch_daily_bars
    original_state = np.random.get_state()
    try:
        np.random.seed(42)
        be.fetch_daily_bars = lambda ticker, start, end: _synthetic_bars(start, end)

        base_cfg = dict(load_config_dict())
        base_cfg["backtest"] = dict(base_cfg.get("backtest") or {}, execution_cost_bps=25.0)

        cfg_no_filter = dict(base_cfg)
        cfg_filtered = dict(base_cfg)
        cfg_filtered["backtest"] = dict(base_cfg["backtest"],
                                        entry_filter={"min_tier": "standard", "exclude_regimes": ["BULL"]})

        res_no_filter = run_regime_timed_replay(["AAA", "BBB"], "2023-01-01", "2024-06-01",
                                                 cfg_no_filter, sma_period=50, buffer_pct=3.0)
        res_filtered = run_regime_timed_replay(["AAA", "BBB"], "2023-01-01", "2024-06-01",
                                                cfg_filtered, sma_period=50, buffer_pct=3.0)

        assert res_filtered["n_entry_filter_rejected"] > 0, (
            "entry_filter set in cfg but nothing was rejected - the stacking is broken again")
        assert (res_filtered["summary"].get("n_trades", 0)
                <= res_no_filter["summary"].get("n_trades", 0)), (
            "a filtered run produced MORE trades than the unfiltered one - impossible if the "
            "filter is actually being applied")
        assert res_filtered["config"]["entry_filter"]["min_tier"] == "standard"
        assert res_filtered["config"]["entry_filter"]["exclude_regimes"] == ["BULL"]
        # And the no-filter run must report it as genuinely off, not silently on.
        assert res_no_filter["n_entry_filter_rejected"] == 0
        assert res_no_filter["config"]["entry_filter"]["min_tier"] is None
    finally:
        be.fetch_daily_bars = original_fetch
        np.random.set_state(original_state)


def test_regime_staying_risk_on_never_force_exits():
    """A regime timeline that's risk-on for the entire hold must produce
    exactly the same result as the plain simulator - the gate should never
    fire spuriously just because it's wired up."""
    rows = [(100, 100, 100, 100), (100, 102, 100, 101.6), (101, 101, 90, 90)]
    bars = _bars(rows)
    plain = simulate_forward_exit(bars, 0, 100.0, 2.0, 70.0, stop_loss_swing_pct=8,
                                  r_multiple=3.0, max_hold_days=20, cfg=CFG, atrs=[2.0] * len(bars))
    d0 = date(2024, 1, 1)
    regime_by_date = {d0 + timedelta(days=i): True for i in range(len(rows))}
    sorted_dates = sorted(regime_by_date.keys())
    gated = _run_gated(rows, regime_by_date=regime_by_date, spy_dates_sorted=sorted_dates)
    assert gated["exit_price"] == plain["exit_price"]
    assert gated["exit_reason"] == plain["exit_reason"]
    assert not gated.get("regime_forced_exit")
