"""Baseline benchmark, price cross-check, rebalance check, tax/cost, readiness checklist, decision date, digest."""
from datetime import date

import pandas as pd

from engine import baseline, maintenance, price_check, promotion_checklist, rebalance_check, tax_cost


def test_baseline_is_flat_until_a_month_end_signal_then_levers_up():
    idx = pd.bdate_range("2025-01-01", periods=400)
    spy = pd.Series([100 + i * 0.1 for i in range(400)], index=idx)       # steady uptrend
    c = baseline.curve(spy, leverage=2.0, band=0.0, financing_bps=0.0)
    assert (c.iloc[:200] == 1.0).all()                        # no signal yet: out of the market, nothing earned
    assert c.iloc[-1] > spy.iloc[-1] / spy.iloc[230]          # once on, 2x an uptrend beats the index over the same span
    down = pd.Series([200 - i * 0.2 for i in range(400)], index=idx)
    assert baseline.curve(down, 2.0, 0.0, 0.0).iloc[-1] > down.iloc[-1] / down.iloc[0]   # out of the market in a downtrend


def test_price_check_flags_only_big_disagreements():
    assert price_check.mismatches({"A": 100.0, "B": 50.0}, {"A": 100.5, "B": 55.0}) == ["B: 50.00 vs 55.00 (9.1% apart)"]
    assert price_check.mismatches({"A": 100.0}, {}) == []


def test_rebalance_deviations():
    pos = [{"ticker": "A", "shares": 10, "entry_price": 10}, {"ticker": "B", "shares": 1, "entry_price": 10}]
    out = rebalance_check.deviations(pos, {"A": 0.5, "C": 0.1}, {"A": 10, "B": 10}, equity=200.0, tolerance=0.01)
    assert any(x.startswith("B:") for x in out) and any(x.startswith("C:") for x in out) and not any(x.startswith("A:") for x in out)


def test_tax_cost_splits_friction_and_tax():
    class L:
        p = "x"
        def get_sm_account(self): return {"starting_cash": 1000, "realized_pnl": 90.0}
        def get_open_sm_positions(self): return [{"ticker": "A", "entry_price": 10, "shares": 10}]
        def _conn(self):
            class C:
                row_factory = None
                def __enter__(s): return s
                def __exit__(s, *a): pass
                def execute(s, q): return type("R", (), {"fetchall": lambda self: [{"realized_pnl": 100.0}]})()
            return C()
    t = tax_cost.summarize(L(), {"A": 12.0}, rate=0.3)
    assert round(t["costs_and_financing"], 6) == 10.0 and t["tax_on_realized"] == 30.0
    assert round(t["tax_if_sold_today"], 6) == round(0.3 * 120, 6)


def test_readiness_counts_manual_flags():
    ov = {"stats": {"days": 70}, "drift": {"status": "ok"}, "scheduler": {"status": "ok"}, "experiment": {"verdict": "pass"},
          "curves": {"sleeve": [100, 110], "baseline": [100, 105]}}
    r = promotion_checklist.evaluate(ov, {"live_readiness": {"drawdown_plan_written": True}})
    assert r["passed"] >= 5 and not r["ready"]
    names = {c["name"]: c["ok"] for c in r["checks"]}
    assert names["Drawdown plan written down"] and not names["Runs on an always-on machine"]


class _DB:
    def __init__(self): self.s = {}
    def get_sm_state(self, k, d=None): return self.s.get(k, d)
    def set_sm_state(self, k, v): self.s[k] = v
    def _conn(self): raise RuntimeError("no db")


def test_swing_decision_sent_once_on_the_date():
    db = _DB()
    cfg = {"swing_experiment": {"decision_date": "2026-12-15"}}
    from engine import strategy_overview as so
    so_real = so.swing_experiment_status
    so.swing_experiment_status = lambda db, cfg, today=None: {"decision_date": "2026-12-15", "verdict": "fail", "entry_days": 12,
                                                              "needs_days": 30, "profit_factor": 0.9, "needs_pf": 1.3}
    try:
        assert maintenance.swing_decision_due(db, cfg, today=date(2026, 12, 14)) is None
        msg = maintenance.swing_decision_due(db, cfg, today=date(2026, 12, 15))
        assert msg and "FAILED" in msg
        assert maintenance.swing_decision_due(db, cfg, today=date(2026, 12, 16)) is None
    finally:
        so.swing_experiment_status = so_real
