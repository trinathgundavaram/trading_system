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


def test_risk_based_sizing_ties_size_to_the_stop_and_caps_it(monkeypatch):
    from types import SimpleNamespace as NS

    from engine import position_sizing as ps
    cfg = {"risk_level": "T", "risk": {"T": {"stop_loss_swing_pct": 8, "stop_loss_day_pct": 4}, "max_position_size_usd": 1000},
           "trading": {}, "position_sizing": {"method": "risk", "risk_per_trade_pct": 0.2, "max_position_pct_of_purse": 5.0,
                                              "min_size_pct": 100, "max_size_pct": 100}}
    cfg["risk"]["max_position_size_usd"] = 1000
    db = type("D", (), {"get_all_positions": lambda s, simulated=True: [], "get_paper_account": lambda s: {"cash": 10000.0}})()
    monkeypatch.setattr(ps, "_get_equity", lambda db, simulated=True: 10000.0)
    br, sr = NS(should_buy=True, pct_score=90), NS(ev_result=None, execution_quality=None)
    # volatile stock: 6% stop -> 0.2% x 10000 / 0.06 = $333, under the $500 cap
    r = ps.calculate(br, sr, {"price": 100.0, "atr": 5.0}, None, cfg, db=db)
    assert round(r.suggested_dollar_amount) == 333
    # calm stock: 1.5% stop floor -> $1333 uncapped, held to 5% of purse = $500
    r = ps.calculate(br, sr, {"price": 100.0, "atr": 0.5}, None, cfg, db=db)
    assert r.suggested_dollar_amount == 500.0


def test_second_opinion_flags_chasing_and_stays_quiet_on_clean_setups():
    from engine import second_opinion as so
    chase = {"price": 120, "sma_20": 100, "sma_50": 95, "sma_200": 130, "rsi": 80, "change_pct": 8, "volume_ratio": 0.6,
             "days_to_earnings": 2, "macd_hist": -0.1}
    r = so.evaluate(chase)
    assert r["verdict"] == "DISAGREE" and len(r["bear"]) >= 5
    clean = {"price": 100, "sma_20": 98, "sma_50": 95, "sma_200": 85, "rsi": 55, "change_pct": 1, "volume_ratio": 1.5,
             "days_to_earnings": 30, "macd_hist": 0.2, "obv_rising": True}
    r = so.evaluate(clean)
    assert r["verdict"] == "AGREE" and r["bear"] == [] and len(r["bull"]) >= 4
    assert so.evaluate({})["verdict"] == "AGREE"          # no data, no flags


def test_second_opinion_outcomes_match_by_entry_time():
    from engine import second_opinion as so
    ops = [{"ticker": "A", "created_at": "2026-11-02T14:00:00", "verdict": "CAUTION"},
           {"ticker": "B", "created_at": "2026-11-02T14:00:00", "verdict": "AGREE"}]
    rts = [{"ticker": "A", "entry": "2026-11-02T14:05:00", "pnl_pct": -2.0},
           {"ticker": "B", "entry": "2026-11-02T14:05:00", "pnl_pct": 3.0},
           {"ticker": "C", "entry": "2026-11-02T14:05:00", "pnl_pct": 9.0}]
    out = so.outcome_stats(ops, rts)
    assert out["CAUTION"]["avg_pct"] == -2.0 and out["AGREE"]["win_pct"] == 100.0 and sum(v["n"] for v in out.values()) == 2


def test_swing_scorecard_groups_and_verdict():
    from engine import swing_scorecard as sc

    def t(tick, side, price, ts, reason="", mode="DAY", pnl=0):
        return {"ticker": tick, "side": side, "price": price, "created_at": ts, "trade_mode": mode, "reason": reason, "pnl": pnl}
    trades = [t("A", "buy", 100, "2026-11-02T10:00"), t("A", "sell", 103, "2026-11-02T15:00", "loop_b_urgent: x", pnl=3),
              t("B", "buy", 100, "2026-11-02T10:00", mode="SWING"), t("B", "sell", 95, "2026-11-06T15:00", "price_watch:stop", pnl=-5)]
    out = sc.build(trades)
    assert out["overall"]["n"] == 2 and out["overall"]["win_pct"] == 50.0
    assert {g["name"] for g in out["by_mode"]} == {"DAY", "SWING"}
    assert any(g["name"] == "Exit engine (urgent)" for g in out["by_exit"])
    assert any(g["name"] == "3-6 days" and g["n"] == 1 for g in out["by_hold"])
    assert out["verdict"]["label"] == "Unproven"


def test_book_health_reflects_performance_not_neutral_scores(tmp_path):
    from storage.database import Database
    db = Database(str(tmp_path / "h.db"))
    pos = lambda r, d: {"simulated": 1, "current_profit_r": r, "days_held": d, "current_stop_price": 1.0, "trade_mode": "SWING"}
    db.get_all_positions = lambda: [pos(1.0, 1), pos(0.5, 2)]
    good = db.get_latest_health_score()
    db.get_all_positions = lambda: [pos(-1.0, 12), pos(-0.5, 9)]
    bad = db.get_latest_health_score()
    assert good > 75 > 40 > bad
    db.get_all_positions = lambda: []
    assert db.get_latest_health_score() is None


def test_reevaluation_tracker_logs_history_and_flags_transitions(db):
    from datetime import date
    from engine import reevaluation
    cfg = {"trading": {"max_positions": 40}, "swing_experiment": {"decision_date": "2026-12-15"}}
    first = reevaluation.run_all(db, cfg, date(2026, 10, 31))
    assert {r["id"] for r in first} == {i["id"] for i in reevaluation.ITEMS}
    snap = {r["id"]: r for r in reevaluation.snapshot(db)}
    assert snap["swing_forward_test"]["status"] == "waiting" and snap["swing_forward_test"]["checked_at"]
    # same status again is not a "change" worth a notice
    assert not any(r["changed"] for r in reevaluation.run_all(db, cfg, date(2026, 10, 31)) if r["status"] == "waiting")
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM reeval_log").fetchone()[0] == 2 * len(reevaluation.ITEMS)


def test_stale_bid_ask_is_not_a_spread_veto():
    from rules.spread_quality import evaluate
    # liquid large cap with a stale ask: 4% "spread" is 216% of a $1.85 ATR -> ignored, not vetoed
    r = evaluate({"bid": 100.0, "ask": 104.0, "price": 100.0, "atr": 1.85}, mode="swing")
    assert not r.hard_veto
    # a real, merely wide spread on a volatile name still vetoes: 1.2% of price, 20% of a $6 ATR
    r2 = evaluate({"bid": 100.0, "ask": 101.2, "price": 100.0, "atr": 6.0}, mode="swing")
    assert r2.hard_veto
