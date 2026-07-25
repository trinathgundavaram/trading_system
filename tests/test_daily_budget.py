"""§7/§8/§9 - the daily budget, the loss limit, and the breaker (R-1/R-2/R-3).

These three findings are one chain. daily_stats was a live-book table:
`trades_placed` was incremented only by live_trader.py and confirm_fill.py,
and `realized_pnl` only when close_position(simulated=False). On a paper-only
deployment both read zero forever, so RiskEngine answered can_trade: True to
every question it was ever asked - 31 buys across seven days against a 10/day
cap, "0 trades placed" reported every day.

The kill switch that should have caught the loss side had zero call sites and
would have raised AttributeError on its first statement if anything had called
it, because db.realized_pnl_today() did not exist.

Pure-logic tests with a fake database, so they run with no Postgres. The
book-separation test is the one that matters most: it asserts the property the
original design intended and the implementation quietly dropped.

    python3 -m pytest tests/test_daily_budget.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

from rules.risk_rules import RiskEngine, daily_loss_limit, trip_kill_switch_if_needed


class FakeDB:
    """Counters and P&L only - what the risk path actually reads."""

    def __init__(self, cash=1000.0):
        self.counters = {"trades_placed": 0, "paper_trades_placed": 0}
        self.pnl = {"live": 0.0, "paper": 0.0}
        self.cash = cash
        self.positions = []
        self.kill_switch = None
        self.events = []
        self.logs = []

    # ---- the §7 counter API ----
    def record_trade_placed(self, simulated: bool):
        self.counters["paper_trades_placed" if simulated else "trades_placed"] += 1

    def trades_placed_today(self, simulated: bool) -> int:
        return self.counters["paper_trades_placed" if simulated else "trades_placed"]

    # ---- the §8 P&L API ----
    def realized_pnl_today(self, simulated: bool = False) -> float:
        return self.pnl["paper" if simulated else "live"]

    def get_paper_account(self):
        return {"cash": self.cash}

    # ---- the §11 drawdown API ----
    # Present so RiskEngine's drawdown gate reads a real (zero) row here
    # rather than falling into drawdown_breach()'s exception path, which would
    # make every test in this file pass for the wrong reason.
    def get_daily_stats(self) -> dict:
        return {"max_drawdown": 0.0, "running_drawdown": 0.0,
                "paper_max_drawdown": 0.0, "paper_running_drawdown": 0.0}

    def get_all_positions(self, simulated=None):
        return list(self.positions)

    # ---- the §9 recording API ----
    def set_kill_switch(self, on, reason=None):
        self.kill_switch = (on, reason)

    def log_ui_event(self, kind, payload):
        self.events.append((kind, payload))

    def log(self, level, msg):
        self.logs.append((level, msg))


def _cfg(**risk):
    base = {"kill_switch_triggered": False, "max_trades_per_day": 10,
            "max_daily_loss_usd": 500, "max_daily_loss_pct": 0}
    base.update(risk)
    return {"trading": {"watch_execute": "WATCH"}, "risk": base}


# ── §7: the budget binds, per book ──────────────────────────────────────────

def test_paper_counter_increments():
    db = FakeDB()
    db.record_trade_placed(simulated=True)
    assert db.trades_placed_today(simulated=True) == 1


def test_paper_counter_does_not_touch_live():
    """THE test. The books must stay separate - a paper session and a live
    session must not consume each other's budget. This is the property the
    original design intended and the implementation dropped."""
    db = FakeDB()
    for _ in range(5):
        db.record_trade_placed(simulated=True)
    assert db.trades_placed_today(simulated=False) == 0


def test_budget_blocks_the_eleventh_trade():
    db, cfg = FakeDB(), _cfg(max_trades_per_day=10)
    for _ in range(10):
        db.record_trade_placed(simulated=True)
    assert RiskEngine(db, cfg, simulated=True).check()["can_trade"] is False


def test_budget_allows_the_tenth():
    """CONTROL - an off-by-one here would silently cost a trade a day."""
    db, cfg = FakeDB(), _cfg(max_trades_per_day=10)
    for _ in range(9):
        db.record_trade_placed(simulated=True)
    assert RiskEngine(db, cfg, simulated=True).check()["can_trade"] is True


def test_engine_defaults_to_the_book_actually_trading():
    """WATCH means paper is placing the trades, so paper is the budget that
    matters. Defaulting to the live counters during a paper session is how
    the limits came to read zero forever."""
    db = FakeDB()
    for _ in range(10):
        db.record_trade_placed(simulated=True)
    watch = {"trading": {"watch_execute": "WATCH"}, "risk": _cfg()["risk"]}
    assert RiskEngine(db, watch).simulated is True
    assert RiskEngine(db, watch).check()["can_trade"] is False

    execute = {"trading": {"watch_execute": "EXECUTE"}, "risk": _cfg()["risk"]}
    assert RiskEngine(db, execute).simulated is False
    assert RiskEngine(db, execute).check()["can_trade"] is True  # live book is clean


# ── §8: the loss limit has a real input ─────────────────────────────────────

def test_paper_loss_is_visible_to_the_risk_engine():
    db, cfg = FakeDB(), _cfg(max_daily_loss_usd=30)
    db.pnl["paper"] = -40.0
    r = RiskEngine(db, cfg, simulated=True).check()
    assert r["can_trade"] is False
    assert "daily loss" in r["reason"]


def test_paper_loss_does_not_block_the_live_book():
    db, cfg = FakeDB(), _cfg(max_daily_loss_usd=30)
    db.pnl["paper"] = -400.0
    assert RiskEngine(db, cfg, simulated=False).check()["can_trade"] is True


def test_percentage_limit_is_tighter_than_the_absolute_cap():
    """$500 against a $1,000 account is not a limit. 2% is $20."""
    db = FakeDB(cash=1000.0)
    cfg = _cfg(max_daily_loss_usd=500, max_daily_loss_pct=2.0)
    assert daily_loss_limit(db, cfg, simulated=True) == pytest.approx(20.0)


def test_absolute_cap_wins_when_it_is_tighter():
    db = FakeDB(cash=1_000_000.0)
    cfg = _cfg(max_daily_loss_usd=500, max_daily_loss_pct=2.0)
    assert daily_loss_limit(db, cfg, simulated=True) == pytest.approx(500.0)


def test_equity_includes_open_positions_not_just_cash():
    """A fully-invested account must not read as near-zero equity.

    Valuing at cost rather than market is deliberate: market value needs a
    live quote per position, this runs on every buy, and an unpriced position
    contributing 0 would TIGHTEN the limit. A $1,000 account with $100 cash
    and $900 deployed would resolve to a $2 daily stop and halt the session
    for entirely the wrong reason."""
    db = FakeDB(cash=100.0)
    db.positions = [{"dollar_amount": 400.0}, {"dollar_amount": 500.0}]
    cfg = _cfg(max_daily_loss_usd=500, max_daily_loss_pct=2.0)
    # 100 cash + 900 deployed = 1000 equity -> 2% = 20, not 2.
    assert daily_loss_limit(db, cfg, simulated=True) == pytest.approx(20.0)


def test_zero_percent_falls_back_to_the_absolute_cap():
    db = FakeDB(cash=1000.0)
    assert daily_loss_limit(db, _cfg(max_daily_loss_pct=0), True) == pytest.approx(500.0)


def test_unreadable_equity_never_widens_the_limit():
    """Fail safe: if equity cannot be determined, fall back to the absolute
    cap rather than to no limit at all."""
    class Broken(FakeDB):
        def get_paper_account(self):
            raise RuntimeError("no purse")

    cfg = _cfg(max_daily_loss_usd=500, max_daily_loss_pct=2.0)
    assert daily_loss_limit(Broken(), cfg, simulated=True) == pytest.approx(500.0)


# ── §9: the breaker ─────────────────────────────────────────────────────────

@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    """A throwaway config.yaml that risk_rules will actually write to."""
    import rules.risk_rules as rr
    path = tmp_path / "config.yaml"

    def write(cfg):
        path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        return cfg

    monkeypatch.setattr(rr, "CONFIG_PATH", path)
    write.path = path
    write.read = lambda: yaml.safe_load(path.read_text())
    return write


def test_kill_switch_trips_on_breach(cfg_file):
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is True
    assert cfg_file.read()["risk"]["kill_switch_triggered"] is True
    assert db.kill_switch[0] is True
    assert any(k == "kill_switch_auto" for k, _ in db.events)


def test_kill_switch_does_not_trip_below_the_limit(cfg_file):
    cfg = cfg_file(_cfg(max_daily_loss_usd=100))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is False
    assert cfg_file.read()["risk"]["kill_switch_triggered"] is False


def test_kill_switch_is_idempotent(cfg_file):
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is True
    before = cfg_file.path.read_text()
    db.events.clear()
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is False
    assert cfg_file.path.read_text() == before   # no rewrite
    assert db.events == []                        # no duplicate alert


def test_kill_switch_never_auto_clears(cfg_file):
    """The single most dangerous failure mode for an automatic breaker is one
    that resets itself. A profitable day after a tripped one must NOT clear
    it - only a human editing config.yaml may."""
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    trip_kill_switch_if_needed(db, cfg, simulated=True)

    db.pnl["paper"] = +500.0                      # a great day follows
    trip_kill_switch_if_needed(db, cfg, simulated=True)
    assert cfg_file.read()["risk"]["kill_switch_triggered"] is True
    assert cfg["risk"]["kill_switch_triggered"] is True


def test_kill_switch_updates_the_callers_config_immediately(cfg_file):
    """The current cycle must stop now, not at the next config reload."""
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    trip_kill_switch_if_needed(db, cfg, simulated=True)
    assert cfg["risk"]["kill_switch_triggered"] is True
    assert RiskEngine(db, cfg, simulated=True).check()["can_trade"] is False


def test_kill_switch_records_which_book_breached(cfg_file):
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    db = FakeDB()
    db.pnl["paper"] = -50.0
    trip_kill_switch_if_needed(db, cfg, simulated=True)
    assert "paper" in cfg_file.read()["risk"]["kill_switch_reason"]


# ── §10: the gate binds per trade, and never blocks an exit ─────────────────

def test_engine_blocks_entry_but_the_sell_path_has_no_gate():
    """§10's asymmetry, asserted structurally.

    execute_buy() must consult RiskEngine; execute_sell() must not. Being
    unable to close a losing position because you already hit the daily trade
    count is how a small loss becomes a large one - the limit would convert
    itself from a risk control into a risk.

    Read from the source rather than executed, so this needs no database: the
    property is "does this code path consult the gate", which is exactly what
    a future well-meaning edit would change."""
    import inspect

    from engine import paper_trader
    buy = inspect.getsource(paper_trader.execute_buy)
    sell = inspect.getsource(paper_trader.execute_sell)

    assert "RiskEngine" in buy, "execute_buy lost its per-trade risk check"
    assert "RiskEngine" not in sell, (
        "execute_sell has acquired a risk check - exits must NEVER be blocked "
        "by a daily limit; see §10 and this function's docstring")


def test_sell_still_records_against_the_budget():
    """An exit is not blocked by the budget, but it does consume it - the next
    ENTRY should see the cost of the churn."""
    import inspect

    from engine import paper_trader
    sell = inspect.getsource(paper_trader.execute_sell)
    assert "record_trade_placed" in sell


def test_cycle_level_block_is_recorded():
    """A cycle halted by a risk limit used to return without logging, so it
    vanished from the cycles table and the Journal tab: 'why did nothing
    happen for three hours?' had no recorded answer."""
    # Read the file rather than importing it: scheduler pulls the whole
    # analysis stack (scipy, pandas_ta) and this assertion is about a dozen
    # lines of text. A structural test that needs the numerical stack
    # installed is a structural test that gets skipped.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "scheduler.py")).read()
    gate = src.split("risk_check = RiskEngine")[1][:700]
    assert "log_cycle" in gate, "the risk-limit block is not recorded"
    assert "simulated=watch_mode" in gate, "cycle-level check is not book-aware"


def test_persist_preserves_config_comments(tmp_path, monkeypatch):
    """config.yaml is where the reasoning behind every risk threshold is
    recorded. A yaml round-trip strips all of it - and the moment the breaker
    trips is precisely when someone opens that file to understand why. So the
    write is a targeted single-line substitution, and this test is what keeps
    it that way."""
    import rules.risk_rules as rr
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.yaml")
    path = tmp_path / "config.yaml"
    path.write_text(open(real).read())
    monkeypatch.setattr(rr, "CONFIG_PATH", path)

    def comments(p):
        return sum(1 for l in p.read_text().splitlines() if l.strip().startswith("#"))

    before = comments(path)
    assert before > 50, "fixture is not the real annotated config"

    # Repeat: the second call must not fall through to a reserialising branch
    # just because there is no longer a `false` to flip.
    for i in range(3):
        rr._persist_kill_switch(f"AUTO call {i}")
        assert comments(path) == before, f"comments lost on call {i}"

    text = path.read_text()
    assert text.count("kill_switch_reason:") == 1, "reason line duplicated"
    parsed = yaml.safe_load(text)
    assert parsed["risk"]["kill_switch_triggered"] is True
    assert "weights" in parsed, "unrelated config sections must survive"


def test_kill_switch_survives_an_unwritable_config(cfg_file, monkeypatch):
    """A failed persist must not swallow the event - the in-memory flag, the
    database row and the alert still fire."""
    import rules.risk_rules as rr
    cfg = cfg_file(_cfg(max_daily_loss_usd=30))
    monkeypatch.setattr(rr, "CONFIG_PATH", tmp := cfg_file.path.parent / "nope" / "config.yaml")
    db = FakeDB()
    db.pnl["paper"] = -50.0
    assert trip_kill_switch_if_needed(db, cfg, simulated=True) is True
    assert cfg["risk"]["kill_switch_triggered"] is True
    assert db.kill_switch[0] is True
