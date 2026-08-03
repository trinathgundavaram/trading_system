"""scripts/analyze_backtest_windows.py (2026-07-28) - the time-sliced
validation Trinath asked for after the standard-tier/exclude-BULL entry
filter run: "demand it survive time-sliced validation before you trust the
PF 1.42 headline." Pins the pure-function pieces (windowing/stats/verdict) -
no DB, no network, no backtest_engine import needed."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_spec = importlib.util.spec_from_file_location(
    "analyze_backtest_windows",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "analyze_backtest_windows.py"),
)
abw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(abw)


def _trade(entry_date, outcome_pct, outcome_r=None):
    return {"entry_date": entry_date, "outcome_pct": outcome_pct,
            "outcome_r": outcome_r if outcome_r is not None else outcome_pct / 3.0}


# --- _window_key --------------------------------------------------------

def test_quarter_bucketing():
    assert abw._window_key("2024-01-15", 3) == "2024-Q1"
    assert abw._window_key("2024-03-31", 3) == "2024-Q1"
    assert abw._window_key("2024-04-01", 3) == "2024-Q2"
    assert abw._window_key("2024-12-31", 3) == "2024-Q4"


def test_half_year_bucketing():
    assert abw._window_key("2024-01-01", 6) == "2024-H1"
    assert abw._window_key("2024-07-01", 6) == "2024-H2"


def test_year_bucketing():
    assert abw._window_key("2024-06-15", 12) == "2024"


# --- _stats --------------------------------------------------------------

def test_stats_empty():
    s = abw._stats([])
    assert s["n"] == 0
    assert s["pf"] is None


def test_stats_basic_pf_and_win_rate():
    trades = [_trade("2024-01-01", 10.0), _trade("2024-01-02", -5.0), _trade("2024-01-03", 5.0)]
    s = abw._stats(trades)
    assert s["n"] == 3
    assert s["win_rate"] == round(2 / 3 * 100, 1)
    assert s["pf"] == round(15.0 / 5.0, 2)


def test_stats_all_losses_gives_zero_pf_not_a_crash():
    """All-loss window: gross_win is 0 and gross_loss > 0, so PF is a real
    0.0 (not None) - None is reserved for the gross_loss==0 case (no losers
    to divide by), which _stats never hits when there's at least one trade
    priced below/at zero."""
    trades = [_trade("2024-01-01", -1.0), _trade("2024-01-02", -2.0)]
    s = abw._stats(trades)
    assert s["pf"] == 0.0
    assert s["win_rate"] == 0.0


# --- windowed_stats --------------------------------------------------------

def test_windowed_stats_groups_by_quarter():
    trades = [
        _trade("2024-01-05", 1.0), _trade("2024-02-10", 2.0),  # Q1
        _trade("2024-04-01", -1.0),                             # Q2
    ]
    w = abw.windowed_stats(trades, 3)
    assert set(w.keys()) == {"2024-Q1", "2024-Q2"}
    assert w["2024-Q1"]["n"] == 2
    assert w["2024-Q2"]["n"] == 1


# --- _verdict --------------------------------------------------------------

def test_verdict_flags_a_negative_window_with_enough_trades():
    windows = {
        "2024-Q1": {"n": 20, "pf": 0.7, "win_rate": 30.0, "avg_outcome_pct": -1.0, "expectancy_r": -0.2},
        "2024-Q2": {"n": 20, "pf": 1.5, "win_rate": 60.0, "avg_outcome_pct": 1.0, "expectancy_r": 0.3},
    }
    v = abw._verdict(windows)
    assert "2024-Q1" in v
    assert "not winning in every period" in v


def test_verdict_ignores_thin_windows_below_ten_trades():
    windows = {
        "2024-Q1": {"n": 3, "pf": 0.1, "win_rate": 10.0, "avg_outcome_pct": -5.0, "expectancy_r": -1.0},
        "2024-Q2": {"n": 15, "pf": 1.5, "win_rate": 60.0, "avg_outcome_pct": 1.0, "expectancy_r": 0.3},
    }
    v = abw._verdict(windows)
    # the thin, terrible Q1 must not trigger the negative-window flag
    assert "every window" in v.lower()


def test_verdict_all_windows_too_thin():
    windows = {"2024-Q1": {"n": 2, "pf": None, "win_rate": None, "avg_outcome_pct": None, "expectancy_r": None}}
    v = abw._verdict(windows)
    assert "too few trades" in v.lower()
