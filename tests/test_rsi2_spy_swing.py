"""Unit tests for rules/rsi2_spy_swing.py, the live decision layer over the
validated (engine/rsi2_mean_reversion_backtest.py) RSI(2) SPY math.

Reuses the exact synthetic fixture (DIP_AND_RECOVER_CLOSES) already hand-
verified in tests/test_rsi2_mean_reversion_backtest.py against compute_rsi/
compute_sma's actual output (see that file's module docstring) - the same
known rsi2/sma_trend/sma_exit values at each index apply here, since
evaluate() calls the identical compute_rsi/compute_sma functions. Module-
level constants (SMA_TREND_PERIOD etc.) are monkeypatched down to match
that fixture's small scale for these tests; the real frozen production
defaults (200/5/2/10/rsi70) are exercised separately in the smoke test
against real data at the bottom of this file.
"""
import pandas as pd
import pytest

from rules import rsi2_spy_swing
from tests.test_rsi2_mean_reversion_backtest import DIP_AND_RECOVER_CLOSES, _make_ohlc


@pytest.fixture
def small_periods(monkeypatch):
    """Matches TestGenerateTrades.COMMON_KW in test_rsi2_mean_reversion_backtest.py:
    trend_period=10, exit_period=3, rsi_period=2, rsi_threshold=25 - small
    enough to trace the DIP_AND_RECOVER_CLOSES fixture by hand."""
    monkeypatch.setattr(rsi2_spy_swing, "SMA_TREND_PERIOD", 10)
    monkeypatch.setattr(rsi2_spy_swing, "SMA_EXIT_PERIOD", 3)
    monkeypatch.setattr(rsi2_spy_swing, "RSI_PERIOD", 2)
    monkeypatch.setattr(rsi2_spy_swing, "RSI_THRESHOLD", 25)
    monkeypatch.setattr(rsi2_spy_swing, "EXIT_MODE", "rsi70")
    monkeypatch.setattr(rsi2_spy_swing, "MAX_HOLD_DAYS", 5)


def test_missing_columns_raises(small_periods):
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES).drop(columns=["open"])
    with pytest.raises(ValueError, match="missing required column"):
        rsi2_spy_swing.evaluate(df)


def test_insufficient_bars_raises_with_real_default_period(monkeypatch):
    monkeypatch.setattr(rsi2_spy_swing, "SMA_TREND_PERIOD", 200)  # real default, not the small test override
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES)  # only 16 rows
    with pytest.raises(ValueError, match="need at least 201"):
        rsi2_spy_swing.evaluate(df)


def test_enter_signal_when_flat_and_conditions_met(small_periods):
    # index 12 (rsi2=20.0 < 25, trend_ok=True) is the fixture's only
    # qualifying signal - see test_rsi2_mean_reversion_backtest.py's
    # DIP_AND_RECOVER_CLOSES comment block.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:13])  # through index 12 inclusive
    result = rsi2_spy_swing.evaluate(df, position=None)
    assert result["action"] == "ENTER_NEXT_OPEN"
    assert result["reason"] == "rsi2_oversold_in_uptrend"
    assert result["trend_ok"] is True
    assert result["rsi2"] == pytest.approx(20.0, abs=0.1)


def test_no_signal_when_flat_and_conditions_not_met(small_periods):
    # index 10 (rsi2=100.0, trend_ok=True but not oversold) - no signal.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:11])
    result = rsi2_spy_swing.evaluate(df, position=None)
    assert result["action"] == "NO_SIGNAL_STAY_FLAT"


def test_hold_when_in_position_and_no_exit_condition(small_periods):
    # index 13: entered here (next-open fill after index 12's signal).
    # rsi2=48.7 (not >70), close=108.2 < sma_exit=108.233 (not >) - holds.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:14])  # through index 13
    entry_date = str(df["date"].iloc[13])
    result = rsi2_spy_swing.evaluate(df, position={"entry_date": entry_date, "entry_price": df["open"].iloc[13]})
    assert result["action"] == "HOLD"
    assert result["days_held"] == 0


def test_exit_this_close_rsi_overbought(small_periods):
    # index 14: rsi2=86.75 > 70 - exits.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:15])  # through index 14
    entry_date = str(df["date"].iloc[13])
    result = rsi2_spy_swing.evaluate(df, position={"entry_date": entry_date, "entry_price": df["open"].iloc[13]})
    assert result["action"] == "EXIT_THIS_CLOSE"
    assert result["reason"] == "rsi_overbought"
    assert result["days_held"] == 1


def test_exit_this_close_sma5_cross(small_periods, monkeypatch):
    monkeypatch.setattr(rsi2_spy_swing, "EXIT_MODE", "sma5")
    # index 14: close=111.0 > sma_exit=108.9 - exits.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:15])
    entry_date = str(df["date"].iloc[13])
    result = rsi2_spy_swing.evaluate(df, position={"entry_date": entry_date, "entry_price": df["open"].iloc[13]})
    assert result["action"] == "EXIT_THIS_CLOSE"
    assert result["reason"] == "sma5_cross"


def test_time_stop_fires_even_when_no_crossing_condition_met(small_periods, monkeypatch):
    monkeypatch.setattr(rsi2_spy_swing, "EXIT_MODE", "sma5")
    monkeypatch.setattr(rsi2_spy_swing, "MAX_HOLD_DAYS", 0)
    # index 13 itself: sma5 condition is false there (108.2 < 108.233), but
    # days_held=0 >= MAX_HOLD_DAYS=0 forces an exit anyway.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:14])
    entry_date = str(df["date"].iloc[13])
    result = rsi2_spy_swing.evaluate(df, position={"entry_date": entry_date, "entry_price": df["open"].iloc[13]})
    assert result["action"] == "EXIT_THIS_CLOSE"
    assert result["reason"] == "time_stop"
    assert result["days_held"] == 0


def test_unknown_entry_date_degrades_to_hold_not_crash(small_periods):
    # entry_date the caller passed isn't in daily_bars at all (e.g. a stale
    # or corrupted state file) - days_held can't be computed, but this must
    # not crash; it should fall through to HOLD rather than false-triggering
    # a time_stop it has no basis for.
    df = _make_ohlc(DIP_AND_RECOVER_CLOSES[:14])
    result = rsi2_spy_swing.evaluate(df, position={"entry_date": "1999-01-01", "entry_price": 100.0})
    assert result["days_held"] is None
    assert result["action"] == "HOLD"


class TestRealDataSmoke:
    """Integration smoke test against the real, already-fetched SPY daily
    OHLCV this session validated the strategy on - exercises the REAL frozen
    production defaults (200/5/2/10/rsi70), not the monkeypatched small ones."""

    def test_evaluate_runs_clean_on_real_recent_data(self):
        from pathlib import Path
        csv_path = Path(__file__).resolve().parent.parent / "spy_daily_ohlc.csv"
        if not csv_path.exists():
            pytest.skip("spy_daily_ohlc.csv not present in this environment")
        df = pd.read_csv(csv_path).tail(400).reset_index(drop=True)
        result = rsi2_spy_swing.evaluate(df, position=None)
        assert result["action"] in ("ENTER_NEXT_OPEN", "NO_SIGNAL_STAY_FLAT")
        assert result["rsi_threshold"] == 10
        assert result["exit_mode"] == "rsi70"
        assert 0 <= result["rsi2"] <= 100
