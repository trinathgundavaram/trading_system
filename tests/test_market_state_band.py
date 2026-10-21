import pandas as pd

from rules.sector_momentum import market_state


def _spy(last, n=260, base=100.0):
    s = pd.Series([base] * n, dtype=float)
    s.iloc[-1] = last
    return pd.DataFrame({"SPY": s})


def test_band_keeps_previous_state_inside_the_band():
    assert market_state(_spy(101), prev_on=False, band=0.02) is False      # +1% is inside the band: stay off
    assert market_state(_spy(99), prev_on=True, band=0.02) is True         # -1% inside the band: stay on


def test_band_switches_outside_the_band():
    assert market_state(_spy(103), prev_on=False, band=0.02) is True
    assert market_state(_spy(97), prev_on=True, band=0.02) is False


def test_no_memory_is_plain_sma_rule():
    assert market_state(_spy(100.5), prev_on=None, band=0.02) is True
    assert market_state(_spy(99.5), prev_on=None, band=0.02) is False
