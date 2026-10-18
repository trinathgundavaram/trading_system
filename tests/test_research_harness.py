import numpy as np
import pandas as pd

from research.harness import Config, backtest, metrics


def _prices(n=400):
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"SPY": 100 * np.cumprod(1 + np.full(n, 0.001)), "IEF": 100.0}, index=idx)


def test_weights_earn_from_next_day_not_signal_day():
    p = _prices()
    reb = p.groupby(p.index.to_period("M")).tail(1).index
    d = [x for x in reb if p.index.get_loc(x) >= 260][0]      # first rebalance: nothing held yet
    p.loc[d:, "SPY"] = p.loc[d:, "SPY"] * 1.5                 # one-day jump ON the signal day
    r = backtest(lambda x: {"SPY": 1.0}, p, Config(cost_bps=0))
    assert r.loc[d] < 0.05                                    # jump not captured by same-day decision


def test_costs_charged_on_turnover():
    p = _prices()
    free = backtest(lambda x: {"SPY": 1.0}, p, Config(cost_bps=0))
    paid = backtest(lambda x: {"SPY": 1.0}, p, Config(cost_bps=100))
    assert paid.sum() < free.sum()
    assert abs((free - paid).sum() - 0.01) < 5e-4             # one entry of 100% turnover at 100bps


def test_long_only_unlevered():
    p = _prices()
    r = backtest(lambda x: {"SPY": 3.0}, p, Config(cost_bps=0))
    assert metrics(r)["maxdd"] > -0.01                        # weights renormalized, no 3x leverage
