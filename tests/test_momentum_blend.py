import numpy as np
import pandas as pd
import pytest

from engine import sector_momentum_paper as smp
from rules import sector_momentum as rule
from tests.test_sector_momentum_sleeve import FakeDb


def _closes(n=320, spy_slope=0.0005):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(3)
    d = {}
    for i, t in enumerate(rule.UNIVERSE_BLEND):
        d[t] = 100 * np.cumprod(1 + 0.0002 * (i % 7) + rng.normal(0, 0.004, n))
    d["SPY"] = 100 * np.cumprod(1 + np.full(n, spy_slope))
    return pd.DataFrame(d, index=idx)


def test_uptrend_levers_risk_positions_only():
    w = rule.target_weights_blend(_closes(), leverage=1.25)
    risk = sum(v for t, v in w.items() if t not in ("IEF", "SHY"))
    defensive = sum(v for t, v in w.items() if t in ("IEF", "SHY"))
    assert 1.0 < risk + defensive <= 1.25 + 1e-9
    assert risk <= 1.25 + 1e-9 and "SHY" not in w


def test_spy_below_200d_goes_fully_to_tbills():
    assert rule.target_weights_blend(_closes(spy_slope=-0.002), leverage=1.25) == {"SHY": 1.0}


def test_short_history_raises():
    with pytest.raises(ValueError):
        rule.target_weights_blend(_closes(100))


def test_leveraged_rebalance_borrows_and_charges_financing():
    db, cfg = FakeDb(), {"sector_momentum_sleeve": {"paper_starting_cash": 10000, "financing_bps": 400}}
    px = {"XLK": 100.0, "XLE": 50.0}
    smp.rebalance(db, cfg, {"XLK": 0.75, "XLE": 0.5}, px, max_gross=1.25)
    assert db.acct["cash"] < 0 and abs(db.acct["cash"] + 2500) < 100          # ~25% borrowed
    gross = sum(p["shares"] * px[p["ticker"]] for p in db.get_open_sm_positions())
    assert 12000 < gross < 12600
    c = smp.accrue_financing(db, cfg)
    assert 0.35 < c < 0.45                                                    # ~2500 * 4% / 252
    smp.rebalance(db, cfg, {"XLK": 1.0, "XLE": 0.5}, px, max_gross=1.25)       # no more than allowed
    assert smp.equity(db, px) > 9900


def test_unlevered_cap_never_goes_negative():
    db, cfg = FakeDb(), {"sector_momentum_sleeve": {"paper_starting_cash": 10000}}
    smp.rebalance(db, cfg, {"XLK": 1.5}, {"XLK": 100.0}, max_gross=1.0)
    assert db.acct["cash"] >= -1e-6


def test_small_adds_skipped_by_default_but_trade_with_a_lower_floor():
    from tests.test_sector_momentum_sleeve import FakeDb
    cfg = {"stock_momentum_sleeve": {"paper_starting_cash": 10000}}
    px = {f"S{i}": 100.0 for i in range(40)}
    even = {t: 1.0 / 40 for t in px}
    db = FakeDb()
    smp.rebalance(db, cfg, even, px, section="stock_momentum_sleeve", min_trade_frac=0.002)
    bigger = {t: 1.25 / 40 for t in px}                                   # +0.625% of equity per name
    smp.rebalance(db, cfg, bigger, px, max_gross=1.25, section="stock_momentum_sleeve")        # default 1% floor: no trades
    assert abs(sum(p["shares"] * 100 for p in db.get_open_sm_positions()) - 10000 * 0.9995) < 30
    smp.rebalance(db, cfg, bigger, px, max_gross=1.25, section="stock_momentum_sleeve", min_trade_frac=0.002)
    assert sum(p["shares"] * 100 for p in db.get_open_sm_positions()) > 12000
