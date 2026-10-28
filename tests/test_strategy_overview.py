import json
from datetime import date

from engine import strategy_overview as so


class Db:
    def __init__(self):
        self.state = {"last_prices": json.dumps({"XLK": 110.0, "IEF": 100.0}),
                      "market": json.dumps({"as_of": "2026-10-20", "spy": 769.0, "sma200": 725.0,
                                            "band_hi": 740.0, "band_lo": 711.0, "risk_on": True})}
    def get_sm_state(self, k, d=None): return self.state.get(k, d)
    def get_sm_account(self): return {"starting_cash": 10000.0, "cash": -5000.0, "realized_pnl": -1.0}
    def get_open_sm_positions(self):
        return [{"ticker": "XLK", "shares": 100.0, "entry_price": 100.0}, {"ticker": "IEF", "shares": 40.0, "entry_price": 100.0}]
    def get_sm_equity_log(self):
        return [{"as_of_date": "2026-10-19", "total_value": 10000.0, "spy_close": 700.0},
                {"as_of_date": "2026-10-20", "total_value": 10100.0, "spy_close": 707.0}]
    def get_paper_equity_history(self, limit=0): return [{"timestamp": "2026-10-19T10:00:00", "total_value": 5000.0, "n_open": 0}]
    def get_paper_account(self): return {"starting_cash": 5000.0, "cash": 5000.0, "realized_pnl": 0.0}
    def get_rsi2_account(self): return None


CFG = {"sector_momentum_sleeve": {"paper_enabled": True, "strategy": "blend", "leverage": 2.0, "band_pct": 2.0},
       "trading": {"mode": "HYBRID", "day_trade_scan_interval_minutes": 15}}


def test_overview_numbers_and_weights():
    o = so.build(Db(), CFG, today=date(2026, 10, 20))
    assert abs(o["account"]["equity"] - 10000.0) < 1e-6           # -5000 cash + 11000 + 4000
    assert abs(o["account"]["gross"] - 1.5) < 1e-6 and o["account"]["borrowed"] == 5000.0
    assert [p["ticker"] for p in o["positions"]] == ["XLK", "IEF"]
    assert abs(sum(p["weight"] for p in o["positions"]) - 1.5) < 1e-6
    assert o["market"]["risk_on"] is True and o["next_rebalance"] == "2026-10-30"
    assert abs(o["stats"]["sleeve_total"] - 0.01) < 1e-9 and o["stats"]["spy_total"] > 0
    assert {j["name"] for j in o["jobs"]} >= {"Sector momentum", "Swing engine scan"}


def test_next_month_end_skips_weekends_and_rolls_over():
    assert so.next_month_end(date(2026, 10, 20)) == "2026-10-30"      # Oct 31 is a Saturday
    assert so.next_month_end(date(2026, 10, 30)) == "2026-10-30"
    assert so.next_month_end(date(2026, 10, 31)) == "2026-11-30"


class _SchedDb:
    def __init__(self, running=False, last_minutes_ago=3.0):
        from datetime import datetime, timedelta
        self.running = running
        self.last = (datetime.utcnow() - timedelta(minutes=last_minutes_ago)).isoformat()
        self.start = datetime.utcnow().isoformat()

    def get_cycle_status(self): return {"is_running": self.running, "stage": "screener", "started_at": self.start}

    def _conn(self):
        import contextlib
        last = self.last

        class _C:
            row_factory = None
            def execute(self, *a, **k):
                class R:
                    def fetchone(_s): return {"timestamp": last, "triggered_by": "scheduler", "duration": 200.0}
                return R()
        @contextlib.contextmanager
        def cm(): yield _C()
        return cm()


def test_scheduler_health_statuses(monkeypatch):
    import scheduler
    cfg = {"trading": {"day_trade_scan_interval_minutes": 15, "hard_kill_minutes": 15}}
    monkeypatch.setattr(scheduler, "is_market_open", lambda c: True)
    assert so.scheduler_health(_SchedDb(running=True), cfg)["status"] == "running"
    assert so.scheduler_health(_SchedDb(last_minutes_ago=10), cfg)["status"] == "ok"
    assert so.scheduler_health(_SchedDb(last_minutes_ago=90), cfg)["status"] == "stale"      # market open, silent > 2 intervals
    monkeypatch.setattr(scheduler, "is_market_open", lambda c: False)
    assert so.scheduler_health(_SchedDb(last_minutes_ago=600), cfg)["status"] == "ok"        # closed market: quiet is normal
