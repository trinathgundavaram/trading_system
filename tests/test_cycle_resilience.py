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
