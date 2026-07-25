"""§11 - drawdown is computed, persisted, and binding.

daily_stats.max_drawdown was declared in the schema, defaulted to 0, read by
the UI, and written by nothing. Expectancy tells you whether an edge exists;
drawdown tells you whether you can survive long enough to realise it, and it
was the one risk statistic this platform was not collecting.

Two layers, on purpose:

  The GATE tests use a fake database and no Postgres, because they are about a
  decision (halt / do not halt) and should run in the pre-commit hook.

  The ARITHMETIC tests need a real database, because the high-water-mark
  behaviour lives in an ON CONFLICT clause and a test that faked it would be
  asserting against a reimplementation rather than against the SQL that runs.

    python3 -m pytest tests/test_drawdown.py -v
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from rules.risk_rules import RiskEngine, drawdown_breach


class FakeDB:
    """Only what the drawdown path reads."""

    def __init__(self, **stats):
        self.stats = {"max_drawdown": 0.0, "running_drawdown": 0.0,
                      "paper_max_drawdown": 0.0, "paper_running_drawdown": 0.0}
        self.stats.update(stats)
        self.raise_on_stats = False

    def get_daily_stats(self) -> dict:
        if self.raise_on_stats:
            raise RuntimeError("daily_stats unreadable")
        return dict(self.stats)

    # RiskEngine reaches these before the drawdown gate.
    def trades_placed_today(self, simulated: bool) -> int:
        return 0

    def realized_pnl_today(self, simulated: bool = False) -> float:
        return 0.0

    def get_paper_account(self):
        return {"cash": 1000.0}

    def get_all_positions(self, simulated=None):
        return []


def _cfg(**risk):
    base = {"kill_switch_triggered": False, "max_trades_per_day": 10,
            "max_daily_loss_usd": 500, "max_daily_loss_pct": 0,
            "max_intraday_drawdown_pct": 3.0, "max_running_drawdown_pct": 15.0}
    base.update(risk)
    return {"trading": {"watch_execute": "WATCH"}, "risk": base}


# ── The gate ────────────────────────────────────────────────────────────────

def test_intraday_breach_blocks():
    db = FakeDB(paper_max_drawdown=3.5)
    assert RiskEngine(db, _cfg(), simulated=True).check()["can_trade"] is False


def test_intraday_at_the_cap_blocks():
    """>=, not >. A cap of 3.0 that permits a trade at exactly 3.0 is a cap of
    'slightly more than 3.0', and nobody reading config.yaml would know."""
    db = FakeDB(paper_max_drawdown=3.0)
    assert RiskEngine(db, _cfg(), simulated=True).check()["can_trade"] is False


def test_just_under_the_cap_allows():
    """CONTROL. Without this, a gate that blocked unconditionally would pass
    every other test in this section."""
    db = FakeDB(paper_max_drawdown=2.99)
    assert RiskEngine(db, _cfg(), simulated=True).check()["can_trade"] is True


def test_running_breach_blocks_and_says_review():
    db = FakeDB(paper_running_drawdown=15.2)
    result = RiskEngine(db, _cfg(), simulated=True).check()
    assert result["can_trade"] is False
    assert "human review" in result["reason"]


def test_running_is_named_first_when_both_breach():
    """The more serious condition should be the one in the halt reason -
    'halt for the day' and 'halt entirely' are different instructions to
    whoever reads the log."""
    db = FakeDB(paper_max_drawdown=9.0, paper_running_drawdown=20.0)
    assert "running drawdown" in RiskEngine(db, _cfg(), simulated=True).check()["reason"]


def test_gate_reads_the_book_it_is_trading():
    """THE test. A live drawdown must not halt a paper session, or the reverse.
    Same property §7 established for the counters - the books stay separate."""
    db = FakeDB(max_drawdown=50.0, running_drawdown=50.0)   # LIVE columns
    assert RiskEngine(db, _cfg(), simulated=True).check()["can_trade"] is True

    db = FakeDB(paper_max_drawdown=50.0)                    # PAPER column
    assert RiskEngine(db, _cfg(), simulated=False).check()["can_trade"] is True


def test_caps_default_off():
    """An unset cap must not become a cap of zero, which would halt on the
    first tick of any drawdown at all."""
    db = FakeDB(paper_max_drawdown=40.0)
    cfg = _cfg(max_intraday_drawdown_pct=0, max_running_drawdown_pct=0)
    assert RiskEngine(db, cfg, simulated=True).check()["can_trade"] is True


def test_unreadable_stats_fails_open():
    """Deliberately the opposite of daily_loss_limit()'s default. There,
    failing open widens a limit that is already known; here, failing closed
    would halt the session on the strength of a number nothing has written."""
    db = FakeDB(paper_max_drawdown=40.0)
    db.raise_on_stats = True
    assert drawdown_breach(db, _cfg(), simulated=True) == ""


def test_missing_columns_do_not_crash():
    """A database that has not run migration 005 yet must still trade."""
    db = FakeDB()
    db.stats = {}
    assert drawdown_breach(db, _cfg(), simulated=True) == ""


# ── Escalation: which halt outlives the day ─────────────────────────────────

@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """A throwaway config.yaml, so a tripped switch cannot write the real one."""
    import yaml

    import rules.risk_rules as rr
    path = tmp_path / "config.yaml"

    def write(cfg):
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return cfg

    monkeypatch.setattr(rr, "CONFIG_PATH", path)
    write.read = lambda: yaml.safe_load(path.read_text())
    return write


def _trippable(db):
    """FakeDB extended with what the kill-switch path records."""
    db.kill_switch = None
    db.events = []
    db.set_kill_switch = lambda on, reason=None: setattr(db, "kill_switch", (on, reason))
    db.log_ui_event = lambda kind, payload: db.events.append((kind, payload))
    return db


def test_running_breach_trips_the_kill_switch(cfg_file):
    """'Human review required' has to be enforced by something, or it is a
    string in a log line. The kill switch is the only control here that a
    recovery cannot silently clear."""
    from rules.risk_rules import trip_kill_switch_if_needed
    cfg = cfg_file(_cfg())
    db = _trippable(FakeDB(paper_running_drawdown=20.0))
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is True
    assert cfg_file.read()["risk"]["kill_switch_triggered"] is True
    assert "running drawdown" in cfg_file.read()["risk"]["kill_switch_reason"]


def test_intraday_breach_does_not_trip_the_kill_switch(cfg_file):
    """THE asymmetry. An intraday breach is a statement about TODAY, and
    drawdown_breach() already blocks new entries for the rest of it.
    Escalating it to a switch a human must clear would halt tomorrow morning
    for something that happened this afternoon."""
    from rules.risk_rules import trip_kill_switch_if_needed
    cfg = cfg_file(_cfg())
    db = _trippable(FakeDB(paper_max_drawdown=9.0))
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is False
    assert cfg_file.read()["risk"]["kill_switch_triggered"] is False
    # ...but the gate is still shut for the day.
    assert RiskEngine(db, cfg, simulated=True).check()["can_trade"] is False


def test_running_breach_on_the_other_book_does_not_trip(cfg_file):
    from rules.risk_rules import trip_kill_switch_if_needed
    cfg = cfg_file(_cfg())
    db = _trippable(FakeDB(running_drawdown=40.0))     # LIVE column
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is False


# ── The arithmetic (needs a real database) ──────────────────────────────────

def _equity(db, points, day_offset=0):
    """Write equity points onto a given local day, oldest first."""
    local_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    offset = datetime.utcnow() - datetime.now()
    with db._conn() as conn:
        for i, value in enumerate(points):
            ts = (local_midnight + timedelta(days=day_offset, hours=10, minutes=i)
                  + offset).isoformat()
            conn.execute(
                "INSERT INTO paper_equity_history (timestamp, total_value) VALUES (?, ?)",
                (ts, float(value)))


def test_intraday_drawdown_is_peak_to_trough(db):
    _equity(db, [1000, 1050, 1000, 1030])       # peak 1050 -> trough 1000
    db.update_drawdown(simulated=True)
    stats = db.get_daily_stats()
    assert stats["paper_max_drawdown"] == pytest.approx(50 / 1050 * 100, abs=1e-3)


def test_max_drawdown_is_a_high_water_mark(db):
    """THE test for the ON CONFLICT clause. A 4% dip at 10am is a fact about
    today that is still true at 3pm. A plain assignment would erase it the
    moment equity recovered - which is precisely the number you most want to
    keep, because it is the risk that was actually taken."""
    _equity(db, [1000, 900])                    # 10% dip
    db.update_drawdown(simulated=True)
    dipped = db.get_daily_stats()["paper_max_drawdown"]
    assert dipped == pytest.approx(10.0, abs=1e-3)

    _equity(db, [1000, 1000], day_offset=0)     # full recovery, later today
    db.update_drawdown(simulated=True)
    assert db.get_daily_stats()["paper_max_drawdown"] == pytest.approx(dipped, abs=1e-3)


def test_running_drawdown_is_assigned_not_ratcheted(db):
    """The complement of the test above, and the reason the asymmetry is
    deliberate: running drawdown is a CURRENT distance from the all-time high,
    so recovering really does reduce it."""
    _equity(db, [1000, 800])
    db.update_drawdown(simulated=True)
    assert db.get_daily_stats()["paper_running_drawdown"] == pytest.approx(20.0, abs=1e-3)

    _equity(db, [900, 1000])
    db.update_drawdown(simulated=True)
    assert db.get_daily_stats()["paper_running_drawdown"] == pytest.approx(0.0, abs=1e-3)


def test_single_point_writes_nothing(db):
    """One point is a level, not a curve. Writing 0 would be a claim ('no
    drawdown today') that a single sample cannot support."""
    _equity(db, [1000])
    assert db.update_drawdown(simulated=True) == {}
    assert db.get_daily_stats()["paper_max_drawdown"] == pytest.approx(0.0)


def test_live_book_writes_nothing(db):
    """There is no live equity curve, so the live columns must stay honestly
    zero rather than being fed the paper figure."""
    _equity(db, [1000, 900])
    assert db.update_drawdown(simulated=False) == {}
    db.update_drawdown(simulated=True)
    assert float(db.get_daily_stats().get("max_drawdown") or 0) == pytest.approx(0.0)


def test_recorded_equity_updates_drawdown(db):
    """The metric must stay current without a separate job that can silently
    stop running - so the equity writer computes it."""
    db.init_paper_account(1000.0)
    db.record_paper_equity({"total_value": 1000.0, "cash": 1000.0, "n_open": 0})
    db.record_paper_equity({"total_value": 950.0, "cash": 1000.0, "n_open": 0})
    assert db.get_daily_stats()["paper_max_drawdown"] == pytest.approx(5.0, abs=1e-3)


def test_backfill_uses_the_peak_as_of_each_day(db):
    """A backfill that measured against TODAY's peak would report drawdowns
    the account had no way of having experienced at the time."""
    _equity(db, [1000, 1000], day_offset=-2)
    _equity(db, [1000, 1200], day_offset=-1)    # new high on the middle day
    _equity(db, [1200, 1100], day_offset=0)
    assert db.backfill_drawdown() == 3

    with db._conn() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = {r["date"]: dict(r) for r in conn.execute(
            "SELECT date, paper_running_drawdown FROM daily_stats").fetchall()}
    first = (datetime.now() - timedelta(days=2)).date().isoformat()
    # Flat at the all-time high on day one: no running drawdown yet, even
    # though the account is well below its eventual peak of 1200.
    assert rows[first]["paper_running_drawdown"] == pytest.approx(0.0, abs=1e-3)


def test_unpriced_cycle_does_not_manufacture_a_drawdown(db):
    """The §8 lesson, applied here. snapshot() carries unpriced positions at
    COST, so a cycle with no quotes must not read as a portfolio that lost all
    its market value - which would be a 100% drawdown and an instant halt for
    entirely the wrong reason."""
    db.init_paper_account(1000.0)
    db.adjust_paper_cash(-900.0)
    db.open_position("AAPL", 100.0, 9.0, 900.0, simulated=True)

    from engine import paper_trader
    priced = paper_trader.snapshot(db, prices={"AAPL": 100.0})
    unpriced = paper_trader.snapshot(db, prices={})          # no quotes at all
    db.record_paper_equity(priced)
    db.record_paper_equity(unpriced)

    assert unpriced["total_value"] == pytest.approx(priced["total_value"], abs=0.01)
    assert db.get_daily_stats()["paper_max_drawdown"] == pytest.approx(0.0, abs=1e-3)
