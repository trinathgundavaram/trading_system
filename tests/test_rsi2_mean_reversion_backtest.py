"""Unit tests for engine/rsi2_mean_reversion_backtest.py.

RSI(2) values below were cross-checked two ways before being hard-coded as
expected values: (1) the module's pandas/ewm implementation, and (2) an
independent, plain-Python Wilder-seeded loop (simple average of the first
`period` gain/loss values, then recursive (avg*(period-1)+new)/period
smoothing from there) written directly against Wilder's original
description rather than against this module's code. Both produced
identical output to float precision for a 10-point test series - see
session notes; the values asserted in test_compute_rsi_matches_hand_
verified_wilder_values below are that cross-checked series.

The SMA/RSI-crossing scenarios in TestGenerateTrades use trend_period=10,
exit_period=3, rsi_period=2 (not the strategy's real 200/5/2 defaults) so
the whole scenario is small enough to trace by hand; every close/open in
those series was chosen by first computing compute_rsi/compute_sma on
candidate series and reading off the actual values (also captured in
session notes), then picking thresholds relative to those already-known
values - not tuned backwards from a desired test result.
"""
import pandas as pd
import pytest

from engine.rsi2_mean_reversion_backtest import compute_rsi, compute_sma, generate_trades


class TestComputeRSI:
    def test_compute_rsi_matches_hand_verified_wilder_values(self):
        closes = pd.Series([10, 10.5, 10.2, 10.8, 11.0, 10.6, 10.4, 10.9, 11.2, 11.0])
        rsi = compute_rsi(closes, period=2)
        expected = [None, None, 62.5, 85.0, 89.285714, 41.666667, 27.173913, 73.412698, 84.90991, 53.857143]
        for actual_val, exp in zip(rsi.tolist(), expected):
            if exp is None:
                assert pd.isna(actual_val)
            else:
                assert actual_val == pytest.approx(exp, abs=1e-4)

    def test_first_period_values_are_nan(self):
        closes = pd.Series([10, 10.5, 10.2, 10.8, 11.0])
        rsi = compute_rsi(closes, period=2)
        assert pd.isna(rsi.iloc[0])
        assert pd.isna(rsi.iloc[1])
        assert not pd.isna(rsi.iloc[2])

    def test_all_gains_yields_rsi_100(self):
        closes = pd.Series([10, 11, 12, 13, 14])
        rsi = compute_rsi(closes, period=2)
        assert rsi.iloc[2] == pytest.approx(100.0)
        assert rsi.iloc[3] == pytest.approx(100.0)


class TestComputeSMA:
    def test_basic_rolling_mean(self):
        closes = pd.Series([1, 2, 3, 4, 5])
        sma = compute_sma(closes, period=3)
        assert pd.isna(sma.iloc[0]) and pd.isna(sma.iloc[1])
        assert sma.iloc[2] == pytest.approx(2.0)
        assert sma.iloc[3] == pytest.approx(3.0)
        assert sma.iloc[4] == pytest.approx(4.0)


def _make_ohlc(closes, opens=None):
    """Builds a minimal date/open/high/low/close frame. opens defaults to
    close - 0.2 (a distinct, arbitrary value) unless explicitly overridden
    for a given index via the opens dict."""
    opens = opens or {}
    n = len(closes)
    dates = pd.date_range("2020-01-01", periods=n, freq="D")
    open_vals = [opens.get(i, closes[i] - 0.2) for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": open_vals,
        "high": [max(o, c) + 0.1 for o, c in zip(open_vals, closes)],
        "low": [min(o, c) - 0.1 for o, c in zip(open_vals, closes)],
        "close": closes,
    })


# Verified via direct computation (see module docstring): with
# trend_period=10 / exit_period=3 / rsi_period=2, this series has its only
# qualifying signal (trend_ok=True AND rsi2 < 25) at index 12
# (rsi2=20.0, sma_trend=106.85, close=107.5). Entry fills at index 13's
# open. sma_exit crosses (close > sma_exit) first at index 14
# (close=111.0 > sma_exit=108.9); index 13 itself does NOT cross
# (close=108.2 < sma_exit=108.233...). rsi2 also first exceeds 70 at
# index 14 (86.75), not index 13 (48.7) - so both exit modes independently
# resolve to the same exit day in this fixture, which is fine: each test
# below still exercises its own code path and asserts its own exit_reason.
DIP_AND_RECOVER_CLOSES = [
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
    110, 109, 107.5, 108.2, 111, 113,
]


class TestGenerateTrades:
    COMMON_KW = dict(rsi_threshold=25, sma_trend_period=10, sma_exit_period=3, rsi_period=2)

    def test_sma5_exit_after_multi_day_hold(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES)
        trades = generate_trades(df, exit_mode="sma5", max_hold_days=5, **self.COMMON_KW)
        assert len(trades) == 1
        t = trades[0]
        assert t["entry_date"] == str(df["date"].iloc[13])
        assert t["entry_price"] == pytest.approx(df["open"].iloc[13])
        assert t["exit_date"] == str(df["date"].iloc[14])
        assert t["exit_price"] == pytest.approx(df["close"].iloc[14])
        assert t["exit_reason"] == "sma5_cross"
        assert t["hold_days"] == 1

    def test_rsi70_exit_mode(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES)
        trades = generate_trades(df, exit_mode="rsi70", max_hold_days=5, **self.COMMON_KW)
        assert len(trades) == 1
        t = trades[0]
        assert t["exit_date"] == str(df["date"].iloc[14])
        assert t["exit_reason"] == "rsi_overbought"
        assert t["hold_days"] == 1

    def test_time_stop_fires_when_no_exit_condition_met_in_window(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES)
        # max_hold_days=1 only lets the scan reach index 13 (entry_pos),
        # which does not satisfy the sma5 exit condition (108.2 < 108.233).
        trades = generate_trades(df, exit_mode="sma5", max_hold_days=1, **self.COMMON_KW)
        assert len(trades) == 1
        t = trades[0]
        assert t["exit_reason"] == "time_stop"
        assert t["hold_days"] == 0
        assert t["exit_date"] == str(df["date"].iloc[13])

    def test_no_signal_when_trend_filter_fails(self):
        # Every close deep in a downtrend - close is always below sma_trend,
        # so no entry should ever fire regardless of RSI(2).
        closes = [110, 105, 100, 95, 90, 85, 80, 75, 70, 65, 60, 55]
        df = _make_ohlc(closes)
        trades = generate_trades(df, rsi_threshold=99, sma_trend_period=10,
                                  sma_exit_period=3, rsi_period=2, exit_mode="sma5")
        assert trades == []

    def test_signal_on_last_bar_produces_no_trade(self):
        # A qualifying signal on the very last row has no next-day open to
        # fill on and must be silently skipped, not raise or fabricate a fill.
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:13])  # truncate so index 12 (the signal) is last
        trades = generate_trades(df, exit_mode="sma5", max_hold_days=5, **self.COMMON_KW)
        assert trades == []

    def test_invalid_exit_mode_raises(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES)
        with pytest.raises(ValueError, match="exit_mode"):
            generate_trades(df, exit_mode="bogus")

    def test_missing_required_column_raises(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES).drop(columns=["low"])
        with pytest.raises(ValueError, match="missing required column"):
            generate_trades(df)

    def test_cost_model_reduces_net_return_vs_zero_cost(self):
        df = _make_ohlc(DIP_AND_RECOVER_CLOSES)
        free = generate_trades(df, exit_mode="sma5", max_hold_days=5, round_trip_bps=0.0, **self.COMMON_KW)
        costly = generate_trades(df, exit_mode="sma5", max_hold_days=5, round_trip_bps=20.0, **self.COMMON_KW)
        assert len(free) == 1 and len(costly) == 1
        # 20bps round-trip = 0.10% haircut on entry (up) and exit (down) each,
        # so the costly trade's return must be measurably worse (lower) than
        # the frictionless one for a winning long trade.
        assert costly[0]["outcome_pct"] < free[0]["outcome_pct"]
        entry, exit_ = df["open"].iloc[13], df["close"].iloc[14]
        raw_free_return = (exit_ / entry - 1) * 100
        assert free[0]["outcome_pct"] == pytest.approx(raw_free_return, abs=1e-3)
        half = (20.0 / 2.0) / 10000.0
        expected_costly_entry = entry * (1 + half)
        expected_costly_exit = exit_ * (1 - half)
        expected_costly_pct = (expected_costly_exit / expected_costly_entry - 1) * 100
        assert costly[0]["outcome_pct"] == pytest.approx(expected_costly_pct, abs=1e-3)

    def test_trades_are_flat_between_entries_no_overlap(self):
        # Two independent dip-and-recover cycles back to back - the second
        # entry must not be considered until strictly after the first
        # trade's exit bar (no overlapping positions in a single-instrument
        # strategy).
        closes = DIP_AND_RECOVER_CLOSES + [112, 111, 109.5, 108, 109, 107.5, 108.2, 111, 113]
        df = _make_ohlc(closes)
        trades = generate_trades(df, exit_mode="sma5", max_hold_days=5, **self.COMMON_KW)
        assert len(trades) >= 1
        for k in range(len(trades) - 1):
            exit_idx = df.index[df["date"].astype(str) == trades[k]["exit_date"]][0]
            entry_idx = df.index[df["date"].astype(str) == trades[k + 1]["entry_date"]][0]
            assert entry_idx > exit_idx
