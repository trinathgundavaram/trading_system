"""engine/intraday_momentum_backtest.py (2026-07-30, phase 1 of docs/
intraday_momentum_scoping_2026-07-30.md) - synthetic-bar tests, since this
environment has no working path to real multi-year 1-minute SPY history
(see that module's DATA BLOCKER docstring section). These tests pin the
math and the state machine in isolation; they are NOT a validated backtest
result and don't claim to be.

Follows this session's established synthetic-bar test pattern (tests/
test_regime_timed_backtest.py, tests/test_execution_costs.py).
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine.intraday_momentum_backtest import (  # noqa: E402
    prepare_bars, compute_noise_bands, compute_intraday_vwap, _is_clock_mark,
    compute_trailing_realized_vol_pct, leverage_multiplier, simulate_day, run_backtest, summarize,
)


# ---------------------------------------------------------------------------
# prepare_bars
# ---------------------------------------------------------------------------

def _raw_bar(ts_utc, o, h, lo, c, v):
    return {"timestamp": ts_utc, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def test_prepare_bars_filters_to_regular_session_and_sorts():
    # 13:30 UTC = 9:30 ET (summer). 20:00 UTC = 16:00 ET - the close print,
    # must be dropped (session end is EXCLUSIVE of 16:00).
    raw = pd.DataFrame([
        _raw_bar(datetime(2026, 6, 1, 13, 25, tzinfo=timezone.utc), 100, 100, 100, 100, 10),  # pre-open, dropped
        _raw_bar(datetime(2026, 6, 1, 13, 31, tzinfo=timezone.utc), 101, 101, 101, 101, 10),  # kept, out of order
        _raw_bar(datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc), 100, 100, 100, 100, 10),  # kept
        _raw_bar(datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc), 105, 105, 105, 105, 10),   # close print, dropped
    ])
    out = prepare_bars(raw)
    assert list(out["close"]) == [100, 101]
    assert out["date"].iloc[0] == date(2026, 6, 1)


def test_prepare_bars_missing_column_raises():
    bad = pd.DataFrame([{"timestamp": datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc),
                          "open": 100, "high": 100, "low": 100, "close": 100}])  # no volume
    with pytest.raises(ValueError):
        prepare_bars(bad)


def test_prepare_bars_dedups_duplicate_timestamps():
    raw = pd.DataFrame([
        _raw_bar(datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc), 100, 100, 100, 100, 10),
        _raw_bar(datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc), 999, 999, 999, 999, 999),
    ])
    out = prepare_bars(raw)
    assert len(out) == 1


# ---------------------------------------------------------------------------
# compute_noise_bands - hand-worked arithmetic, lookback_days=2 so the math
# stays checkable by hand. Two ET-local minute offsets per day (9:30, 9:31).
# ---------------------------------------------------------------------------

def _et_minute_bars(day_str, rows):
    """rows: list of (minute_offset_from_930, open, close). Builds bars at
    9:30 ET + offset, tz-aware in America/New_York directly (so this test
    doesn't depend on prepare_bars' UTC conversion - it's testing
    compute_noise_bands in isolation, one layer up)."""
    base = pd.Timestamp(f"{day_str} 09:30:00", tz="America/New_York")
    out = []
    for offset, o, c in rows:
        ts = base + pd.Timedelta(minutes=offset)
        out.append({"timestamp": ts, "date": ts.date(), "open": o, "high": max(o, c),
                     "low": min(o, c), "close": c, "volume": 1000})
    return pd.DataFrame(out)


def test_noise_band_warmup_is_nan_until_lookback_satisfied():
    # D1: 0 prior days. D2: 1 prior day (needs 2). Both must be NaN.
    df = pd.concat([
        _et_minute_bars("2026-06-01", [(0, 100, 100.5), (1, 100.5, 101.0)]),
        _et_minute_bars("2026-06-02", [(0, 110, 110.55), (1, 110.55, 111.1)]),
    ], ignore_index=True)
    out = compute_noise_bands(df, lookback_days=2, band_multiplier=1.0)
    assert out["sigma_t"].isna().all()
    assert out["upper_band"].isna().all()
    assert out["lower_band"].isna().all()


def test_noise_band_arithmetic_on_the_third_day():
    # D1, D2 identical shape (abs return 0.005 at offset0, 0.01 at offset1)
    # -> D3's sigma_t = exactly those values (mean of two identical inputs).
    # D3 opens at 120, gapping UP from D2's last close (111.1) by 8.9 -
    # gap-up pushes ONLY the lower band down by 8.9, per the paper's rule.
    df = pd.concat([
        _et_minute_bars("2026-06-01", [(0, 100, 100.5), (1, 100.5, 101.0)]),
        _et_minute_bars("2026-06-02", [(0, 110, 110.55), (1, 110.55, 111.1)]),
        _et_minute_bars("2026-06-03", [(0, 120, 120.3), (1, 120.3, 119.9)]),
    ], ignore_index=True)
    out = compute_noise_bands(df, lookback_days=2, band_multiplier=1.0)
    d3 = out[out["date"] == date(2026, 6, 3)].reset_index(drop=True)

    assert d3["sigma_t"].iloc[0] == pytest.approx(0.005)
    assert d3["sigma_t"].iloc[1] == pytest.approx(0.01)
    assert d3["gap"].iloc[0] == pytest.approx(8.9)

    # offset0: upper = 120*1.005 = 120.6 (unchanged - gap-up doesn't touch
    # the upper band); lower = 120*0.995 - 8.9 = 119.4 - 8.9 = 110.5
    assert d3["upper_band"].iloc[0] == pytest.approx(120.6)
    assert d3["lower_band"].iloc[0] == pytest.approx(110.5)
    # offset1: upper = 120*1.01 = 121.2; lower = 120*0.99 - 8.9 = 109.9
    assert d3["upper_band"].iloc[1] == pytest.approx(121.2)
    assert d3["lower_band"].iloc[1] == pytest.approx(109.9)


def test_noise_band_gap_down_pushes_upper_band_up():
    df = pd.concat([
        _et_minute_bars("2026-06-01", [(0, 100, 100.5), (1, 100.5, 101.0)]),
        _et_minute_bars("2026-06-02", [(0, 110, 110.55), (1, 110.55, 111.1)]),
        # D3 gaps DOWN from D2's close (111.1) to 105 -> gap = -6.1
        _et_minute_bars("2026-06-03", [(0, 105, 105.3), (1, 105.3, 104.9)]),
    ], ignore_index=True)
    out = compute_noise_bands(df, lookback_days=2, band_multiplier=1.0)
    d3 = out[out["date"] == date(2026, 6, 3)].reset_index(drop=True)
    assert d3["gap"].iloc[0] == pytest.approx(-6.1)
    # offset0: upper = 105*1.005 + 6.1 = 105.525 + 6.1 = 111.625; lower unchanged = 105*0.995 = 104.475
    assert d3["upper_band"].iloc[0] == pytest.approx(111.625)
    assert d3["lower_band"].iloc[0] == pytest.approx(104.475)


def test_band_multiplier_widens_the_band():
    df = pd.concat([
        _et_minute_bars("2026-06-01", [(0, 100, 100.5)]),
        _et_minute_bars("2026-06-02", [(0, 110, 110.55)]),
        _et_minute_bars("2026-06-03", [(0, 120, 120.3)]),
    ], ignore_index=True)
    narrow = compute_noise_bands(df, lookback_days=2, band_multiplier=1.0)
    wide = compute_noise_bands(df, lookback_days=2, band_multiplier=2.0)
    n3 = narrow[narrow["date"] == date(2026, 6, 3)].reset_index(drop=True)
    w3 = wide[wide["date"] == date(2026, 6, 3)].reset_index(drop=True)
    assert w3["upper_band"].iloc[0] > n3["upper_band"].iloc[0]
    assert w3["lower_band"].iloc[0] < n3["lower_band"].iloc[0]


# ---------------------------------------------------------------------------
# compute_intraday_vwap
# ---------------------------------------------------------------------------

def test_vwap_cumulative_typical_price():
    df = _et_minute_bars("2026-06-01", [(0, 100, 100), (1, 100, 102)])
    df.loc[0, ["high", "low"]] = [100, 100]
    df.loc[1, ["high", "low"]] = [102, 100]
    df.loc[0, "volume"] = 10
    df.loc[1, "volume"] = 30
    vwap = compute_intraday_vwap(df)
    # bar0 typical = (100+100+100)/3 = 100, pv=1000, cumvol=10 -> vwap0=100
    assert vwap.iloc[0] == pytest.approx(100.0)
    # bar1 typical = (102+100+102)/3 = 101.333, pv=3040, cum pv=4040, cum vol=40 -> vwap1=101.0
    assert vwap.iloc[1] == pytest.approx(101.0)


def test_vwap_resets_each_session_day():
    df = pd.concat([
        _et_minute_bars("2026-06-01", [(0, 100, 100)]),
        _et_minute_bars("2026-06-02", [(0, 50, 50)]),
    ], ignore_index=True)
    df["volume"] = 10
    vwap = compute_intraday_vwap(df)
    assert vwap.iloc[1] == pytest.approx(50.0)  # not blended with day 1's 100


# ---------------------------------------------------------------------------
# leverage_multiplier / compute_trailing_realized_vol_pct
# ---------------------------------------------------------------------------

def test_leverage_multiplier_scales_up_in_calm_markets():
    assert leverage_multiplier(0.5, target_daily_vol_pct=2.0, max_leverage=4.0) == pytest.approx(4.0)


def test_leverage_multiplier_scales_down_in_volatile_markets():
    assert leverage_multiplier(4.0, target_daily_vol_pct=2.0, max_leverage=4.0) == pytest.approx(0.5)


def test_leverage_multiplier_defaults_to_neutral_on_bad_input():
    assert leverage_multiplier(None) == 1.0
    assert leverage_multiplier(0.0) == 1.0
    assert leverage_multiplier(-1.0) == 1.0
    assert leverage_multiplier(float("nan")) == 1.0


def test_trailing_realized_vol_has_no_lookahead():
    # 5 sessions of closes. pct_change()'s leading NaN means the first
    # window with 2 real (non-NaN) returns is [ret1, ret2] - computed at
    # raw index 2, which shift(1) then moves to index 3. So indices 0-2
    # must all be NaN, and index 3's value must come from sessions 0-2's
    # PRICES (returns 1 and 2) - NOT from session 3's own price (101,
    # which follows the huge jump to 200 at index 2).
    closes = pd.DataFrame({
        "date": [date(2026, 6, 1) + timedelta(days=i) for i in range(5)],
        "close": [100, 102, 200, 101, 103],
    })
    vol = compute_trailing_realized_vol_pct(closes, lookback_days=2)
    assert pd.isna(vol.iloc[0]) and pd.isna(vol.iloc[1]) and pd.isna(vol.iloc[2])
    manual_rets = [102 / 100 - 1, 200 / 102 - 1]
    expected = np.std(manual_rets, ddof=1) * 100
    assert vol.iloc[3] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# simulate_day - the state machine. Bands/vwap are hand-set directly on a
# synthetic day rather than derived through compute_noise_bands, so this
# tests entry/exit/reversal/EOD-flatten logic in isolation from the band
# math (already covered above).
# ---------------------------------------------------------------------------

def _day_with_bands(rows):
    """rows: list of (hh, mm, open, close, upper_band, lower_band, vwap)."""
    base = pd.Timestamp("2026-06-01", tz="America/New_York")
    out = []
    for hh, mm, o, c, ub, lb, vw in rows:
        ts = base + pd.Timedelta(hours=hh, minutes=mm)
        out.append({"timestamp": ts, "date": ts.date(), "open": o, "high": max(o, c),
                     "low": min(o, c), "close": c, "volume": 1000,
                     "upper_band": ub, "lower_band": lb, "vwap": vw})
    return pd.DataFrame(out)


def test_simulate_day_next_bar_entry_and_band_stop_reversal():
    rows = [
        (9, 30, 100, 100, 105, 95, 100),
        (9, 31, 100, 100, 105, 95, 100),
        (10, 0, 100, 106, 105, 95, 100),    # signal: close 106 > upper 105 -> long
        (10, 1, 106.2, 106, 105, 95, 100),  # next_bar fill for entry: open 106.2
        (10, 29, 106, 106, 105, 95, 100),   # not a clock mark - ignored
        (10, 30, 106, 94, 105, 95, 100),    # signal: close 94 < lower 95 -> stop long, reverse short
        (10, 31, 93.8, 93.8, 105, 95, 100), # next_bar fill for exit AND reversal entry: open 93.8
        (11, 0, 93.8, 93.8, 105, 95, 100),  # holds - no stop/target
        (11, 30, 93.8, 93.5, 105, 95, 100), # last bar - EOD flatten at its close, 93.5
    ]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band", commission=0.0, slippage=0.0)

    assert len(trades) == 2
    t0, t1 = trades
    assert t0["direction"] == "long"
    assert t0["entry_price"] == pytest.approx(106.2)
    assert t0["exit_price"] == pytest.approx(93.8)
    assert t0["exit_reason"] == "trailing_stop"

    assert t1["direction"] == "short"
    assert t1["entry_price"] == pytest.approx(93.8)
    assert t1["exit_price"] == pytest.approx(93.5)
    assert t1["exit_reason"] == "eod_flatten"
    # short entered at 93.8, exited at 93.5 -> a gain of 0.3/share, frictionless
    assert t1["raw_pnl_per_share"] == pytest.approx(0.3)


def test_simulate_day_same_bar_fill_uses_signal_bars_own_price():
    rows = [
        (9, 30, 100, 100, 105, 95, 100),
        (10, 0, 100, 106, 105, 95, 100),   # signal AND fill bar under same_bar
        (10, 30, 106, 106, 105, 95, 100),  # holds
        (11, 0, 106, 106, 105, 95, 100),   # last bar - EOD flatten
    ]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="same_bar", stop_mode="band", commission=0.0, slippage=0.0)
    assert len(trades) == 1
    assert trades[0]["entry_price"] == pytest.approx(106.0)  # the signal bar's own close


def test_simulate_day_no_signal_no_trade():
    rows = [(9, 30, 100, 100, 105, 95, 100), (10, 0, 100, 101, 105, 95, 100),
            (10, 30, 101, 102, 105, 95, 100), (11, 0, 102, 103, 105, 95, 100)]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band")
    assert trades == []


def test_simulate_day_signal_on_last_bar_next_bar_unfillable_is_skipped():
    # Breakout signal on the very LAST bar of the day, with fill_timing=
    # next_bar - there's no bar left to fill on, so no trade should be
    # fabricated (not even an EOD-flatten one, since the position never
    # actually opened).
    rows = [(9, 30, 100, 100, 105, 95, 100), (15, 59, 100, 106, 105, 95, 100)]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band")
    assert trades == []


def test_simulate_day_band_or_vwap_stop_uses_the_higher_of_the_two_for_longs():
    rows = [
        (9, 30, 100, 100, 105, 95, 100),
        (10, 0, 100, 106, 105, 95, 100),     # entry signal, upper=105
        (10, 1, 106, 106, 105, 95, 100),     # next_bar fill: 106
        (10, 30, 106, 108, 105, 95, 107),    # vwap(107) > upper(105) -> stop level becomes 107; close 108 stays above -> holds
        (11, 0, 108, 106.5, 105, 95, 107.5), # stop level = max(105, 107.5) = 107.5; close 106.5 < 107.5 -> stop hit
        (11, 1, 106.4, 106.4, 105, 95, 107.5),  # next_bar fill for the exit
        (11, 30, 106.4, 106.4, 105, 95, 107.5),
    ]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band_or_vwap", commission=0.0, slippage=0.0)
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "trailing_stop"
    assert trades[0]["exit_price"] == pytest.approx(106.4)
    # band_or_vwap does NOT auto-reverse
    assert trades[0]["direction"] == "long"


def test_simulate_day_leverage_x_is_attached_to_every_trade():
    rows = [(9, 30, 100, 100, 105, 95, 100), (10, 0, 100, 106, 105, 95, 100),
            (10, 1, 106, 106, 105, 95, 100), (10, 30, 106, 106, 105, 95, 100)]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band", leverage_x=2.5)
    assert trades[0]["leverage_x"] == pytest.approx(2.5)


def test_simulate_day_cost_model_reduces_net_outcome_vs_raw():
    rows = [(9, 30, 100, 100, 105, 95, 100), (10, 0, 100, 106, 105, 95, 100),
            (10, 1, 106, 106, 105, 95, 100), (10, 30, 106, 106.5, 105, 95, 100)]
    day_df = _day_with_bands(rows)
    trades = simulate_day(day_df, fill_timing="next_bar", stop_mode="band",
                           commission=0.0035, slippage=0.001)
    assert trades[0]["net_outcome_pct"] < trades[0]["raw_outcome_pct"]


# ---------------------------------------------------------------------------
# run_backtest - light integration smoke test wiring prepare_bars ->
# compute_noise_bands -> compute_intraday_vwap -> simulate_day together.
# ---------------------------------------------------------------------------

def _raw_multiday_bars():
    """3 sessions of real UTC-timestamped minute bars (13:30-20:00 UTC =
    9:30-16:00 ET) with enough spread that a lookback_days=2 band should be
    computable on day 3 and produce at least one signal."""
    rows = []
    day_opens = {0: 400.0, 1: 402.0, 2: 405.0}
    for d in range(3):
        day = datetime(2026, 6, 1 + d, 13, 30, tzinfo=timezone.utc)
        o = day_opens[d]
        price = o
        for m in range(0, 390):  # full session, one bar per minute
            ts = day + timedelta(minutes=m)
            # oscillate a bit but drift up faster on day 3 to force a breakout
            drift = 0.01 if d < 2 else 0.05
            price = price + drift if m % 2 == 0 else price - drift * 0.5
            c = price
            rows.append(_raw_bar(ts, price - 0.02, price + 0.02, price - 0.05, c, 1000 + m))
    return pd.DataFrame(rows)


def test_run_backtest_end_to_end_smoke():
    raw = _raw_multiday_bars()
    result = run_backtest(raw, lookback_days=2, band_multiplier=1.0, fill_timing="next_bar", stop_mode="band")
    assert "trades" in result and "summary" in result and "config" in result
    assert result["config"]["fill_timing"] == "next_bar"
    # day 1 and 2 are warmup (lookback_days=2 needs 2 full prior sessions) -
    # only day 3 can possibly produce a signal.
    trade_dates = {t["date"] for t in result["trades"]}
    assert trade_dates <= {"2026-06-03"}


def test_run_backtest_same_bar_vs_next_bar_are_both_reportable_and_can_differ():
    raw = _raw_multiday_bars()
    same = run_backtest(raw, lookback_days=2, fill_timing="same_bar")
    nxt = run_backtest(raw, lookback_days=2, fill_timing="next_bar")
    # Not asserting a specific direction of difference (that's an empirical
    # question the scoping doc explicitly refuses to prejudge) - just that
    # both run cleanly and are independently inspectable, per the
    # dual-report commitment in docs/intraday_momentum_scoping_2026-07-30.md.
    assert same["config"]["fill_timing"] == "same_bar"
    assert nxt["config"]["fill_timing"] == "next_bar"


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

def test_summarize_empty():
    assert summarize([]) == {"n_trades": 0}


def test_summarize_computes_win_rate_and_profit_factor():
    trades = [
        {"date": "2026-06-01", "direction": "long", "exit_reason": "trailing_stop",
         "hold_minutes": 5, "raw_outcome_pct": 1.0, "net_outcome_pct": 0.9},
        {"date": "2026-06-01", "direction": "short", "exit_reason": "eod_flatten",
         "hold_minutes": 10, "raw_outcome_pct": -0.5, "net_outcome_pct": -0.6},
    ]
    s = summarize(trades)
    assert s["n_trades"] == 2
    assert s["n_days"] == 1
    assert s["win_rate_net"] == 50.0
    assert s["profit_factor_net"] == pytest.approx(0.9 / 0.6)
    assert s["direction_counts"] == {"long": 1, "short": 1}
    assert s["exit_reason_counts"] == {"trailing_stop": 1, "eod_flatten": 1}
