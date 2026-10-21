import numpy as np
import pandas as pd
import pytest

from engine import sector_momentum_paper as smp
from rules import sector_momentum as rule


def _closes(n=300, winners=("XLK", "XLE", "XLV")):
    idx = pd.bdate_range("2024-01-01", periods=n)
    d = {t: 100 * np.cumprod(1 + np.full(n, 0.0001)) for t in rule.UNIVERSE}
    for i, t in enumerate(winners):
        d[t] = 100 * np.cumprod(1 + np.full(n, 0.002 - 0.0004 * i))
    return pd.DataFrame(d, index=idx)


def test_picks_top_momentum_sectors_and_sums_to_one():
    w = rule.target_weights(_closes())
    assert set(w) == {"XLK", "XLE", "XLV"} and abs(sum(w.values()) - 1) < 1e-9


def test_downtrend_sector_goes_to_ief():
    c = _closes()
    c.iloc[-30:, c.columns.get_loc("XLK")] *= np.linspace(1, 0.5, 30)   # falls below its 200d SMA
    w = rule.target_weights(c)
    assert "XLK" not in w and w["IEF"] > 0.3


def test_short_history_raises_not_cash():
    with pytest.raises(ValueError):
        rule.target_weights(_closes(100))


class FakeDb:
    def __init__(self):
        self.acct = None; self.pos = []; self._id = 0
    def get_sm_account(self): return self.acct
    def init_sm_account(self, c): self.acct = {"starting_cash": c, "cash": c, "realized_pnl": 0.0}; return self.acct
    def adjust_sm_cash(self, d, realized_pnl_delta=0.0): self.acct["cash"] += d; self.acct["realized_pnl"] += realized_pnl_delta
    def get_open_sm_positions(self): return [p for p in self.pos if p["status"] == "open"]
    def open_sm_position(self, t, d, px, sh):
        self._id += 1; self.pos.append({"id": self._id, "ticker": t, "status": "open", "entry_price": px, "shares": sh}); return self._id
    def update_sm_position_shares(self, i, sh): next(p for p in self.pos if p["id"] == i)["shares"] = sh
    def update_sm_position_entry_price(self, i, px): next(p for p in self.pos if p["id"] == i)["entry_price"] = px
    def get_sm_state(self, k, d=None): return getattr(self, '_st', {}).get(k, d)
    def set_sm_state(self, k, v): self.__dict__.setdefault('_st', {})[k] = v
    def close_sm_position(self, i, d, px, pnl): next(p for p in self.pos if p["id"] == i)["status"] = "closed"


def test_rebalance_conserves_value_less_costs_and_rotates():
    db, cfg = FakeDb(), {"sector_momentum_sleeve": {"paper_starting_cash": 10000}}
    px = {"XLK": 100.0, "XLE": 50.0, "IEF": 100.0}
    smp.rebalance(db, cfg, {"XLK": 0.5, "IEF": 0.5}, px)
    assert abs(smp.equity(db, px) - 10000) < 10000 * 0.001            # only slippage lost
    smp.rebalance(db, cfg, {"XLE": 1.0}, px)
    held = {p["ticker"] for p in db.get_open_sm_positions()}
    assert held == {"XLE"}
    assert smp.equity(db, px) > 9950


def test_missing_price_refuses():
    db = FakeDb()
    with pytest.raises(ValueError):
        smp.rebalance(db, {}, {"XLK": 1.0}, {"IEF": 100.0})
