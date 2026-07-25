"""engine/live_trader.py - every test runs against a MOCKED robin_stocks
(no network, no credentials, no real orders, obviously).

2026-07-17: live execution sits behind a MASTER SWITCH
(trading.live_execution_enabled, default false, only settable through the
token + typed-phrase-protected /api/live_execution endpoint). The FIRST
tests assert the default is inert: without the master switch, NOTHING
reaches the broker even with EXECUTE + auto_trade. The remaining tests turn
the switch on in their config to verify the gates and bookkeeping.

    python3 -m pytest tests/test_live_trader.py -v
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest



# ---- mocked robin_stocks -----------------------------------------------

class FakeOrders:
    def __init__(self):
        self.placed = []
        self.fill_state = "filled"
        self.fill_price = 50.0

    def order_buy_fractional_by_price(self, ticker, amount, **kw):
        self.placed.append(("buy", ticker, amount))
        return {"id": "order-1"}

    def order_sell_fractional_by_shares(self, ticker, shares, **kw):
        self.placed.append(("sell", ticker, shares))
        return {"id": "order-2"}

    def get_stock_order_info(self, order_id):
        qty = 2.0 if order_id == "order-1" else self._sold_qty
        return {"state": self.fill_state, "average_price": str(self.fill_price),
                "cumulative_quantity": str(qty)}

    def cancel_stock_order(self, order_id):
        self.placed.append(("cancel", order_id))

    _sold_qty = 2.0


class FakeProfiles:
    buying_power = "10000.0"
    def load_account_profile(self, account_number=None, **kw):
        # account_number mirrors the real robin_stocks signature (multi-
        # account support, used for the Agentic account - 2026-07-17).
        return {"buying_power": self.buying_power}


@pytest.fixture()
def rh_mock(monkeypatch):
    fake = types.ModuleType("robin_stocks.robinhood")
    fake.orders = FakeOrders()
    fake.profiles = FakeProfiles()
    fake.login = lambda *a, **k: {"access_token": "x"}
    pkg = types.ModuleType("robin_stocks")
    pkg.robinhood = fake
    monkeypatch.setitem(sys.modules, "robin_stocks", pkg)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", fake)
    monkeypatch.setenv("ROBINHOOD_USERNAME", "test@x.com")
    monkeypatch.setenv("ROBINHOOD_PASSWORD", "pw")
    monkeypatch.delenv("ROBINHOOD_TOTP_SECRET", raising=False)
    from engine import live_trader
    live_trader._login_state.update(ok=False, checked_at=0.0, error="")
    live_trader.breaker.record(True)  # reset breaker
    live_trader._POLL_INTERVAL_ORIG = live_trader._POLL_INTERVAL
    live_trader._POLL_INTERVAL = 0.01
    yield fake
    live_trader._POLL_INTERVAL = live_trader._POLL_INTERVAL_ORIG


# The `db` fixture now comes from tests/conftest.py: a real ephemeral
# Postgres with a clean schema per test (§12). This module used to define its
# own `db` fixture as `Database(path=tmp_path / "...")`, which LOOKED isolated
# and was not - `path` has been a dead parameter since the 2026-07-21 Postgres
# migration, so every one of these tests ran against the production database.
# See tests/conftest.py for the incident that made this urgent.


# Master switch ON in this cfg - used by the gate/bookkeeping tests.
LIVE_CFG = {
    "trading": {"watch_execute": "EXECUTE", "auto_trade": True,
                 "live_execution_enabled": True,
                 "trade_size_usd": 100, "max_positions": 10, "mode": "SWING"},
    "risk": {"kill_switch_triggered": False, "max_position_size_usd": 500,
              "max_trades_per_day": 10},
}

# Same config WITHOUT the master switch - must always be inert.
UNARMED_CFG = {**LIVE_CFG,
               "trading": {**LIVE_CFG["trading"], "live_execution_enabled": False}}


@pytest.fixture()
def enabled(tmp_path, monkeypatch):
    """Arms live execution for a test.

    Arming is a config matter (live_execution_enabled inside each test's cfg)
    PLUS, since §2 (Phase 1, 2026-07-24), a current validation receipt. This
    fixture supplies a passing one so the tests below keep describing the
    gates they were written to describe. The receipt gate itself is tested
    separately and exhaustively in tests/test_live_arm_gate.py.

    TP_FORCE_PAPER is cleared for the same reason: these tests describe the
    gates, not the environment they happen to run in."""
    import json
    from datetime import datetime
    from engine import live_trader

    monkeypatch.delenv("TP_FORCE_PAPER", raising=False)
    receipt = tmp_path / "live_arm_receipt.json"
    receipt.write_text(json.dumps({"passed": True,
                                   "generated_at": datetime.utcnow().isoformat(),
                                   "summary": "test fixture"}))
    monkeypatch.setattr(live_trader, "_validation_receipt_path", lambda: str(receipt))
    yield


# ---- DEFAULT STATE: master switch off, nothing ever reaches the broker ----

def test_master_switch_default_is_off():
    from engine import live_trader
    import yaml, os
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "config.yaml")
    cfg = yaml.safe_load(open(cfg_path))
    assert cfg["trading"].get("live_execution_enabled") is False, (
        "config.yaml's live_execution_enabled must default to false - it may "
        "only be flipped via the token + typed-phrase protected UI switch")
    # even EXECUTE + auto_trade is inert without the master switch
    assert not live_trader.is_live_mode(UNARMED_CFG)
    assert not live_trader.is_live_mode({})  # missing key = off


def test_no_order_without_master_switch(db, rh_mock):
    from engine import live_trader
    assert live_trader.execute_buy_live(db, UNARMED_CFG, "NVDA", 50.0) == {}
    assert live_trader.execute_sell_live(db, UNARMED_CFG, "NVDA", reason="x") == {}
    assert rh_mock.orders.placed == []  # broker saw NOTHING


def test_confirm_phrase_required_to_enable():
    """The /api/live_execution endpoint rejects enabling without the exact
    typed phrase.

    §4 (Phase 1, 2026-07-24) moved the token check out of the handler body and
    into the require_token FastAPI dependency, so the handler no longer takes
    an x_auth_token argument at all - calling it here means the token has
    already been verified. The token half of this test now lives in
    tests/test_ui_auth.py, which also asserts structurally that this route
    still carries the dependency."""
    import asyncio
    from fastapi import HTTPException
    import server
    loop = asyncio.new_event_loop()
    with pytest.raises(HTTPException) as e:
        loop.run_until_complete(server.set_live_execution(
            {"enable": True, "confirm": "yes please"}))
    assert e.value.status_code == 400


# ---- ARMED (master switch on in cfg): gates + bookkeeping stay valid ----

def test_is_live_mode_without_a_validation_receipt():
    """§2 (Phase 1): the fourth gate. Deliberately does NOT take the `enabled`
    fixture, so no receipt exists - which is the platform's state today, and
    must stay inert even with all three original gates open."""
    from engine import live_trader
    assert live_trader.is_live_mode(LIVE_CFG) is False


def test_is_live_mode_gates(enabled):
    from engine import live_trader
    assert live_trader.is_live_mode(LIVE_CFG)
    assert not live_trader.is_live_mode(
        {"trading": {"live_execution_enabled": True, "watch_execute": "WATCH", "auto_trade": True}})
    assert not live_trader.is_live_mode(
        {"trading": {"live_execution_enabled": True, "watch_execute": "EXECUTE", "auto_trade": False}})
    assert not live_trader.is_live_mode({})  # defaults are always safe


def test_no_order_unless_armed(db, rh_mock, enabled):
    from engine import live_trader
    cfg = {"trading": {"watch_execute": "WATCH", "auto_trade": True}}
    assert live_trader.execute_buy_live(db, cfg, "NVDA", 50.0) == {}
    cfg = {"trading": {"watch_execute": "EXECUTE", "auto_trade": False}}
    assert live_trader.execute_buy_live(db, cfg, "NVDA", 50.0) == {}
    assert rh_mock.orders.placed == []  # NOTHING was sent to the broker


def test_kill_switch_blocks_buys_not_sells(db, rh_mock, enabled):
    from engine import live_trader
    cfg = {**LIVE_CFG, "risk": {**LIVE_CFG["risk"], "kill_switch_triggered": True}}
    assert live_trader.execute_buy_live(db, cfg, "NVDA", 50.0) == {}
    assert rh_mock.orders.placed == []
    # sells still allowed: open a position first (normal cfg), then sell under kill switch
    live_trader.execute_buy_live(db, LIVE_CFG, "NVDA", 50.0)
    closed = live_trader.execute_sell_live(db, cfg, "NVDA", reason="test")
    assert closed and closed["ticker"] == "NVDA"


def test_buy_records_real_fill(db, rh_mock, enabled):
    from engine import live_trader
    out = live_trader.execute_buy_live(db, LIVE_CFG, "NVDA", 49.0, trade_mode="day")
    assert out["order_id"] == "order-1"
    assert ("buy", "NVDA", 100.0) in rh_mock.orders.placed
    pos = db.get_open_position("NVDA", simulated=False)
    assert pos["entry_price"] == 50.0 and pos["shares"] == 2.0  # the FILL, not the signal price
    assert pos["trade_mode"] == "DAY"
    assert db.get_daily_stats()["trades_placed"] == 1
    # duplicate buy blocked
    assert live_trader.execute_buy_live(db, LIVE_CFG, "NVDA", 49.0) == {}


def test_buy_respects_size_cap_and_buying_power(db, rh_mock, enabled):
    from engine import live_trader
    class Size:
        applicable = True
        suggested_dollar_amount = 9999.0
    live_trader.execute_buy_live(db, LIVE_CFG, "MU", 50.0, position_size=Size())
    # capped at risk.max_position_size_usd (500), not 9999
    assert ("buy", "MU", 500.0) in rh_mock.orders.placed
    # insufficient buying power -> no order
    rh_mock.profiles.buying_power = "50.0"
    assert live_trader.execute_buy_live(db, LIVE_CFG, "KO", 50.0) == {}
    assert not any(t == "KO" for _, t, *_ in rh_mock.orders.placed)


def test_unfilled_order_cancelled_not_recorded(db, rh_mock, enabled):
    from engine import live_trader
    live_trader.FILL_WAIT_SECONDS_ORIG = live_trader.FILL_WAIT_SECONDS
    live_trader.FILL_WAIT_SECONDS = 0.05
    try:
        rh_mock.orders.fill_state = "queued"  # never fills
        out = live_trader.execute_buy_live(db, LIVE_CFG, "VRT", 80.0)
        assert out == {}
        assert db.get_open_position("VRT", simulated=False) is None  # book untouched
        assert ("cancel", "order-1") in rh_mock.orders.placed
    finally:
        live_trader.FILL_WAIT_SECONDS = live_trader.FILL_WAIT_SECONDS_ORIG


def test_sell_closes_and_feeds_learning(db, rh_mock, enabled):
    from engine import live_trader
    from learning.pattern_database import PatternDatabase
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("BMY", "SWING", {"_entry_price": 50.0})
    live_trader.execute_buy_live(db, LIVE_CFG, "BMY", 50.0, pattern_id=pid)
    rh_mock.orders.fill_price = 55.0
    closed = live_trader.execute_sell_live(db, LIVE_CFG, "BMY",
                                            reason="sell_rules:take_profit", pattern_db=pdb)
    assert closed["pnl"] == pytest.approx(10.0)  # 2 sh * $5
    assert db.get_open_position("BMY", simulated=False) is None
    patterns = db.get_patterns(mode="SWING", ticker="BMY", closed_only=True)
    assert patterns and patterns[0]["exit_reason"] == "live_sell_rules:take_profit"
    # real close writes daily_stats realized pnl (paper closes don't)
    assert db.get_daily_stats()["realized_pnl"] == pytest.approx(10.0)
