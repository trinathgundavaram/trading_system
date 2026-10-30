"""Backfill of missed marks, swing auto-promotion, notification broadcast, daily maintenance helpers."""
from datetime import date

import pandas as pd

from engine import maintenance
from engine.mark_backfill import backfill_missing_marks
from engine.swing_experiment import sizing_stage


class _Led:
    def __init__(self, marks, positions, cash):
        self.marks = {d: v for d, v in marks}
        self.pos, self.cash = positions, cash

    def get_sm_equity_log(self): return [{"as_of_date": d, "total_value": v, "spy_close": 1} for d, v in sorted(self.marks.items())]
    def get_open_sm_positions(self): return self.pos
    def get_sm_account(self): return {"cash": self.cash}
    def log_sm_equity(self, d, v, spy): self.marks[d] = v


def _closes():
    idx = pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"])
    return pd.DataFrame({"AAA": [10, 11, 12, 13, 14.0], "SPY": [100, 101, 102, 103, 104.0]}, index=idx)


def test_backfill_fills_only_the_gap_between_marks():
    led = _Led([("2026-10-20", 1000), ("2026-10-26", 1100)], [{"ticker": "AAA", "shares": 10, "entry_date": "2026-10-20"}], cash=-50.0)
    added = backfill_missing_marks(led, _closes(), today="2026-10-27")
    assert added == ["2026-10-21", "2026-10-22", "2026-10-23"]
    assert led.marks["2026-10-22"] == -50 + 10 * 12


def test_backfill_refuses_when_the_book_changed_after_the_last_mark():
    led = _Led([("2026-10-20", 1000), ("2026-10-26", 1100)], [{"ticker": "AAA", "shares": 10, "entry_date": "2026-10-26"}], cash=0.0)
    assert backfill_missing_marks(led, _closes(), today="2026-10-27") == []


def _trades(n_days, win):
    out = []
    for i in range(n_days):
        d = f"2026-11-{i + 1:02d}" if i < 28 else f"2026-12-{i - 27:02d}"
        px = 105 if (win and i % 4) else 95
        out += [{"ticker": f"T{i}", "side": "buy", "price": 100, "created_at": d + "T10:00", "pnl": 0},
                {"ticker": f"T{i}", "side": "sell", "price": px, "created_at": d + "T15:00", "pnl": 1}]
    return out


def test_sizing_stage_promotes_only_on_a_passing_forward_test():
    assert sizing_stage([]) == "small"
    assert sizing_stage(_trades(10, True)) == "small"       # still collecting
    assert sizing_stage(_trades(30, True)) == "promoted"
    assert sizing_stage(_trades(30, False)) == "small"      # failed


def test_notify_broadcast_reaches_every_transport(monkeypatch):
    from engine import notifications as n
    hits = []
    monkeypatch.setitem(n.TRANSPORTS, "desktop", lambda *a, **k: hits.append("desktop") or True)
    monkeypatch.setitem(n.TRANSPORTS, "webhook", lambda *a, **k: hits.append("webhook") or True)
    cfg = {"notifications": {"enabled": True, "broadcast": True, "transports": ["desktop", "webhook", "log"]}}
    assert n.notify(cfg, "t", "m") is True
    assert hits == ["desktop", "webhook"]


def test_health_problems_flags_a_stale_sleeve_mark(tmp_path, monkeypatch):
    class DB:
        def get_cycle_status(self): return {}
        def _conn(self): raise RuntimeError("no db")
    import storage.sleeve_ledger as sl
    monkeypatch.setattr(sl, "SleeveLedger", lambda db, p: type("L", (), {"get_sm_equity_log": lambda self: [{"as_of_date": "2026-10-01"}]})())
    probs = maintenance.health_problems(DB(), {"sector_momentum_sleeve": {"paper_enabled": True}}, today=date(2026, 10, 30))
    assert any("sector momentum sleeve has no daily mark since 2026-10-01" in p for p in probs)
