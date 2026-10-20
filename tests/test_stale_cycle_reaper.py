from datetime import datetime, timedelta

from engine import cycle_supervisor as cs


class FakeDb:
    def __init__(self, running=True, age_min=240, pid=None):
        self.row = {"is_running": running, "pid": pid,
                    "started_at": (datetime.utcnow() - timedelta(minutes=age_min)).isoformat()}
        self.finished = self.killed = None

    def get_cycle_status(self): return dict(self.row)
    def set_cycle_finished(self): self.finished = True; self.row["is_running"] = False
    def mark_cycle_killed(self, expected_pid, reason): self.killed = (expected_pid, reason); self.row["is_running"] = False; return True


def test_dead_pid_old_row_is_reaped(monkeypatch):
    monkeypatch.setattr(cs, "_pid_is_cycle", lambda p: False)
    db = FakeDb(pid=70573)
    assert cs.reap_if_stale(db, 900) and db.finished


def test_young_row_without_pid_is_left_alone(monkeypatch):
    monkeypatch.setattr(cs, "_pid_is_cycle", lambda p: False)
    assert not cs.reap_if_stale(FakeDb(age_min=0.5, pid=None), 900)


def test_live_cycle_within_limit_is_left_alone(monkeypatch):
    monkeypatch.setattr(cs, "_pid_is_cycle", lambda p: True)
    assert not cs.reap_if_stale(FakeDb(age_min=5, pid=1), 900)


def test_live_cycle_past_limit_is_killed(monkeypatch):
    killed = []
    monkeypatch.setattr(cs, "_pid_is_cycle", lambda p: True)
    monkeypatch.setattr(cs, "_kill_process_tree", lambda pid, reason: killed.append(pid))
    db = FakeDb(age_min=30, pid=4242)
    assert cs.reap_if_stale(db, 900) and killed == [4242] and db.killed == (4242, "timeout_reaped")


def test_not_running_is_noop():
    assert not cs.reap_if_stale(FakeDb(running=False), 900)
