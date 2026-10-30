import threading
import time
from datetime import datetime, timedelta

import pytz

from scripts.sector_momentum_paper_cycle import needs_rebalance


def test_catch_up_rebalance_rules():
    assert not needs_rebalance("2026-10-28", "2026-10-20")      # no month-end since
    assert needs_rebalance("2026-11-02", "2026-10-20")          # Oct 30 month-end was missed
    assert not needs_rebalance("2026-11-02", "2026-10-30")      # done on the month-end itself
    assert needs_rebalance("2026-12-01", "2026-10-30")          # Nov month-end missed
    assert needs_rebalance("2026-10-28", None)                  # never rebalanced


class _Job:
    def __init__(self, id, nrt): self.id, self.next_run_time = id, nrt


class _Sched:
    def __init__(self, jobs): self.jobs, self.woken = jobs, threading.Event()
    def get_jobs(self): return self.jobs
    def wakeup(self): self.woken.set()


def test_watchdog_wakes_scheduler_when_a_job_is_overdue():
    import scheduler
    et = pytz.timezone("US/Eastern")
    s = _Sched([_Job("trading_cycle", datetime.now(et) - timedelta(minutes=30))])
    threading.Thread(target=scheduler._scheduler_watchdog, args=(s, 0.05, 45.0), daemon=True).start()
    assert s.woken.wait(2.0)


def test_watchdog_stays_quiet_when_nothing_is_due():
    import scheduler
    et = pytz.timezone("US/Eastern")
    s = _Sched([_Job("trading_cycle", datetime.now(et) + timedelta(hours=1))])
    threading.Thread(target=scheduler._scheduler_watchdog, args=(s, 0.05, 45.0), daemon=True).start()
    assert not s.woken.wait(0.5)


def test_screener_source_timeout_does_not_block_the_cycle(monkeypatch):
    from engine import screener

    def hang(params):
        time.sleep(5)
        return []

    monkeypatch.setattr(screener, "_screen_rs_gainers", hang)
    monkeypatch.setattr(screener, "_screen_volume_surge", lambda params: [])
    cfg = {"screener": {"source_timeout_seconds": 1,
                        "sources": {k: {"enabled": k in ("rs_gainers", "volume_surge") } for k in
                                    ("rs_gainers", "volume_surge", "finviz_screen", "sector_leaders", "pre_market_movers",
                                     "gap_candidates", "universe_sweep", "alpha_movers", "fmp_movers", "fq_movers",
                                     "momentum_screen", "insider_buying")}}}
    t0 = time.monotonic()
    result = screener.run_screener(cfg, mode="swing")
    assert time.monotonic() - t0 < 4.0
    assert "timed out" in result.sources_failed.get("rs_gainers", "")


def test_second_scheduler_is_refused_by_the_lock(tmp_path):
    import scheduler
    path = str(tmp_path / "s.lock")
    assert scheduler._acquire_scheduler_lock(path) is True
    assert scheduler._acquire_scheduler_lock(path) is False      # another holder: decline
    scheduler._scheduler_lock_fh.close()                          # release for other tests
    scheduler._scheduler_lock_fh = None


def test_quality_gate_does_not_refetch_the_same_candidates(monkeypatch):
    from engine import screener
    from engine.cache import TTLCache
    import mcp_clients.yfinance_mcp as ym

    calls = []
    infos = {"GOOD": {"regularMarketPrice": 50.0, "averageVolume": 5_000_000, "bid": 49.99, "ask": 50.01, "sector": "Tech"},
             "THIN": {"regularMarketPrice": 50.0, "averageVolume": 10_000, "bid": 49.99, "ask": 50.01}}

    class FakeYF:
        def get_ticker_info(self, t):
            calls.append(t)
            return infos[t]

    monkeypatch.setattr(ym, "YFinanceMCP", FakeYF)
    monkeypatch.setattr(screener, "cache", TTLCache(None))
    mk = lambda t: screener.ScreenerCandidate(ticker=t, source="rs_gainers", reason="x", priority=1)
    cfg = {"screener": {"quality_gate": {"min_price": 10, "max_price": 1000, "min_avg_volume": 1_000_000}}}
    first = screener._apply_quality_gate([mk("GOOD"), mk("THIN")], cfg, "swing")
    assert [c.ticker for c in first] == ["GOOD"] and sorted(calls) == ["GOOD", "THIN"]
    second = screener._apply_quality_gate([mk("GOOD"), mk("THIN")], cfg, "swing")
    assert [c.ticker for c in second] == ["GOOD"]
    assert sorted(calls) == ["GOOD", "THIN"]                  # nothing re-fetched on the next cycle


def test_only_the_trade_count_cap_pauses_entries_instead_of_ending_the_cycle():
    import scheduler
    assert scheduler._is_trade_count_cap("30/30 paper trades today")
    assert scheduler._is_trade_count_cap("32/30 live trades today")
    assert not scheduler._is_trade_count_cap("Kill switch is ON")
    assert not scheduler._is_trade_count_cap("paper daily loss $5.00 breached -$2.00")
    assert not scheduler._is_trade_count_cap("paper running drawdown 16% >= 15%")


def test_entry_veto_matches_the_earnings_exit_rule():
    from rules import hard_vetoes
    cfg = {"sell_rules": {"rules": {"earnings_approaching": {"enabled": True, "days_before": 2}}}}
    base = {"price": 50.0, "days_to_earnings": 0}
    for d in (0, 1, 2):
        r = hard_vetoes.check("X", dict(base, days_to_earnings=d), {}, cfg)
        assert r.vetoed and r.veto_code == "EARNINGS_IMMINENT", d
    r = hard_vetoes.check("X", dict(base, days_to_earnings=5), {}, cfg)
    assert r.veto_code != "EARNINGS_IMMINENT"
    off = {"sell_rules": {"rules": {"earnings_approaching": {"enabled": False, "days_before": 2}}}}
    assert hard_vetoes.check("X", dict(base, days_to_earnings=0), {}, off).veto_code != "EARNINGS_IMMINENT"


class _FakeProc:
    def __init__(self, finishes_after=None):
        self.calls, self.finishes_after = 0, finishes_after

    def wait(self, timeout=None):
        import subprocess
        self.calls += 1
        if self.finishes_after is not None and self.calls >= self.finishes_after:
            return 0
        raise subprocess.TimeoutExpired("x", timeout)


def _clock(monkeypatch, wall_step, mono_step):
    from engine import cycle_supervisor as cs
    state = {"w": 1000.0, "m": 50.0}

    def tw():
        state["w"] += wall_step(); return state["w"]

    def tm():
        state["m"] += mono_step(); return state["m"]
    monkeypatch.setattr(cs.time, "time", tw)
    monkeypatch.setattr(cs.time, "monotonic", tm)
    return cs


def test_supervisor_abandons_a_cycle_that_straddled_a_machine_sleep(monkeypatch):
    steps = iter([5, 5, 5, 3600, 5])                         # the wall clock leaps an hour while monotonic does not
    cs = _clock(monkeypatch, lambda: next(steps, 5), lambda: 5)
    assert cs._wait_for_child(_FakeProc(), 900, poll=0) == "slept"


def test_supervisor_still_enforces_the_hard_limit_without_sleep(monkeypatch):
    cs = _clock(monkeypatch, lambda: 5, lambda: 5)
    assert cs._wait_for_child(_FakeProc(), 60, poll=0) == "timeout"


def test_supervisor_returns_exited_when_the_child_finishes(monkeypatch):
    cs = _clock(monkeypatch, lambda: 5, lambda: 5)
    assert cs._wait_for_child(_FakeProc(finishes_after=3), 900, poll=0) == "exited"
