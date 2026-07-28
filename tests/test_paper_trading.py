"""WATCH-mode paper trading (engine/paper_trader.py) - purse accounting,
real-book cloning, buy/sell mimicry, book isolation, and the learning-loop
pattern linkage. Runs against a throwaway SQLite file, zero MCP calls.

    python3 -m pytest tests/test_paper_trading.py -v
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from learning.pattern_database import PatternDatabase
from engine import paper_trader


CFG = {
    "trading": {"watch_execute": "WATCH", "trade_size_usd": 100, "max_positions": 3},
    "paper_trading": {"starting_cash": 500.0},
    # §10 put the risk gate INSIDE execute_buy, so every test that exercises a
    # paper buy now runs through RiskEngine and has to declare the risk posture
    # it is testing under. Stated explicitly rather than left to defaults: a
    # test asserting purse arithmetic should fail if a limit blocks the buy,
    # not quietly pass because the limit was generous.
    #
    # Limits are wide open here on purpose - these tests are about accounting
    # and position mechanics. The limits themselves are tested in
    # test_daily_budget.py and test_drawdown.py.
    "risk": {
        "kill_switch_triggered": False,
        "max_trades_per_day": 1000,
        "max_daily_loss_usd": 1_000_000,
        "max_daily_loss_pct": 0,
        "max_intraday_drawdown_pct": 0,
        "max_running_drawdown_pct": 0,
    },
}


# The `db` fixture now comes from tests/conftest.py: a real ephemeral
# Postgres with a clean schema per test (§12). This module used to define its
# own `db` fixture as `Database(path=tmp_path / "...")`, which LOOKED isolated
# and was not - `path` has been a dead parameter since the 2026-07-21 Postgres
# migration, so every one of these tests ran against the production database.
# See tests/conftest.py for the incident that made this urgent.


def test_is_watch_mode():
    assert paper_trader.is_watch_mode(CFG)
    assert not paper_trader.is_watch_mode({"trading": {"watch_execute": "EXECUTE"}})
    assert paper_trader.is_watch_mode({})  # defaults to WATCH


def test_seed_creates_purse_without_cloning_real_book(db):
    """2026-07-27: ensure_seeded() used to clone every open real position
    into the paper book (trade_mode='SEED') on first creation, logging a
    "buy" to paper_trades but never debiting paper_account.cash for it -
    SEED positions sat on top of the purse for free (a live $240.11
    reconcile.py drift traced straight back to this). Paper mode now starts
    as a clean cash-only purse; robinhood_sync.py's seed-paper CLI
    (unchanged) is the only path that mirrors the real book into paper, and
    it does so with correct cash accounting (see that script)."""
    db.open_position("NVDA", 150.0, 2.0, 300.0, simulated=False)
    account = paper_trader.ensure_seeded(db, CFG)
    assert account["cash"] == 500.0
    assert db.get_all_positions(simulated=True) == []
    # idempotent - second call must not touch cash or create anything
    paper_trader.ensure_seeded(db, CFG)
    assert db.get_all_positions(simulated=True) == []
    assert db.get_paper_account()["cash"] == 500.0


def test_buy_debits_purse_and_sell_credits(db):
    paper_trader.ensure_seeded(db, CFG)
    result = paper_trader.execute_buy(db, CFG, "MU", 50.0)  # falls back to $100
    assert result["shares"] == pytest.approx(2.0)
    assert db.get_paper_account()["cash"] == pytest.approx(400.0)

    closed = paper_trader.execute_sell(db, "MU", 55.0, reason="test_exit")
    assert closed["pnl"] == pytest.approx(10.0)          # 2 sh * $5
    assert closed["pnl_pct"] == pytest.approx(10.0)
    acct = db.get_paper_account()
    assert acct["cash"] == pytest.approx(510.0)          # 400 + 2*55
    assert acct["realized_pnl"] == pytest.approx(10.0)
    assert db.get_open_position("MU", simulated=True) is None
    sides = [t["side"] for t in db.get_paper_trades()]
    assert sides == ["sell", "buy"]


def test_buy_respects_cash_and_max_positions(db):
    paper_trader.ensure_seeded(db, CFG)
    for i, t in enumerate(["AAA", "BBB", "CCC"]):
        assert paper_trader.execute_buy(db, CFG, t, 10.0)
    # 4th blocked by max_positions=3
    assert paper_trader.execute_buy(db, CFG, "DDD", 10.0) == {}
    # duplicate blocked
    assert paper_trader.execute_buy(db, CFG, "AAA", 10.0) == {}
    # drain purse: 500-300=200 left; a $10k suggestion is unaffordable
    class Size:
        applicable = True
        suggested_dollar_amount = 10_000.0
    paper_trader.execute_sell(db, "AAA", 10.0, reason="free_slot")
    assert paper_trader.execute_buy(db, CFG, "EEE", 10.0, position_size=Size()) == {}


def test_position_sizing_amount_used(db):
    paper_trader.ensure_seeded(db, CFG)
    class Size:
        applicable = True
        suggested_dollar_amount = 250.0
    result = paper_trader.execute_buy(db, CFG, "VRT", 125.0, position_size=Size())
    assert result["dollar_amount"] == 250.0 and result["shares"] == pytest.approx(2.0)
    assert db.get_paper_account()["cash"] == pytest.approx(250.0)


def test_books_are_isolated(db):
    """A real close must never touch a simulated position on the same
    ticker and vice versa - the exact confirm_fill.py-vs-paper_trader
    hazard the simulated flag exists to prevent.

    Previously exercised via ensure_seeded()'s real-book clone; that
    cloning was removed 2026-07-27 (see
    test_seed_creates_purse_without_cloning_real_book), so the sim-side
    ORCL row is opened directly here instead - the isolation property
    under test was never about how the sim row got there."""
    db.open_position("ORCL", 100.0, 1.0, 100.0, simulated=False)
    paper_trader.ensure_seeded(db, CFG)  # purse only, no clone
    db.open_position("ORCL", 100.0, 1.0, 100.0, simulated=True)
    # real sell (confirm_fill path, default simulated=False)
    closed_real = db.close_position("ORCL", 110.0)
    assert closed_real["pnl"] == pytest.approx(10.0)
    assert db.get_open_position("ORCL", simulated=True) is not None   # clone survives
    assert db.get_open_position("ORCL", simulated=False) is None
    # paper sell closes only the sim row
    paper_trader.execute_sell(db, "ORCL", 120.0, reason="test")
    assert db.get_open_position("ORCL") is None


def test_sell_closes_linked_pattern_for_learning(db):
    paper_trader.ensure_seeded(db, CFG)
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    paper_trader.execute_buy(db, CFG, "FIX", 40.0, pattern_id=pid)
    # while the paper position is open, the pattern must be reachable so
    # scheduler._close_due_patterns() skips the time-based close
    assert db.get_open_position_by_pattern(pid, simulated=True) is not None
    paper_trader.execute_sell(db, "FIX", 44.0, reason="sell_rules:trail_stop", pattern_db=pdb)
    patterns = db.get_patterns(mode="SWING", ticker="FIX", closed_only=True)
    assert len(patterns) == 1
    assert patterns[0]["outcome_pct"] == pytest.approx(10.0)
    assert patterns[0]["exit_reason"] == "paper_sell_rules:trail_stop"


def test_snapshot_accounting(db):
    paper_trader.ensure_seeded(db, CFG)
    paper_trader.execute_buy(db, CFG, "ASTS", 20.0)   # $100 -> 5 sh
    snap = paper_trader.snapshot(db, prices={"ASTS": 24.0})
    assert snap["cash"] == pytest.approx(400.0)
    assert snap["invested_cost"] == pytest.approx(100.0)
    assert snap["market_value"] == pytest.approx(120.0)
    assert snap["unrealized_pnl"] == pytest.approx(20.0)
    assert snap["total_value"] == pytest.approx(520.0)
    assert snap["total_return_pct"] == pytest.approx(4.0)
    # no price available -> carried at cost, total value doesn't drop
    snap2 = paper_trader.snapshot(db)
    assert snap2["total_value"] == pytest.approx(500.0)


def test_trade_mode_attribution(db):
    """Every buy is stamped with the trading mode it was bought under
    (SWING/DAY/HYBRID), a SEED-tagged position keeps SEED through the
    ledger, and sells inherit the mode of the position they close.

    SEED positions are no longer created by ensure_seeded() (removed
    2026-07-27 - see test_seed_creates_purse_without_cloning_real_book);
    robinhood_sync.py's seed-paper CLI is the only remaining path that
    creates them, via the same open_position()/log_paper_trade() pair
    reproduced directly here rather than pulling in that whole script."""
    paper_trader.ensure_seeded(db, CFG)
    db.open_position("NVDA", 150.0, 1.0, 150.0, simulated=True, trade_mode="SEED")
    db.log_paper_trade("NVDA", "buy", 150.0, 1.0, 150.0,
                        reason="seeded_from_robinhood", trade_mode="SEED")
    seed_pos = db.get_open_position("NVDA", simulated=True)
    assert seed_pos["trade_mode"] == "SEED"

    cfg_hybrid = {**CFG, "trading": {**CFG["trading"], "mode": "HYBRID"}}
    paper_trader.execute_buy(db, cfg_hybrid, "MU", 50.0, trade_mode="hybrid")
    pos = db.get_open_position("MU", simulated=True)
    assert pos["trade_mode"] == "HYBRID"
    # falls back to config mode when not passed explicitly
    paper_trader.execute_buy(db, cfg_hybrid, "VRT", 100.0)
    assert db.get_open_position("VRT", simulated=True)["trade_mode"] == "HYBRID"

    closed = paper_trader.execute_sell(db, "MU", 55.0, reason="test")
    assert closed["trade_mode"] == "HYBRID"
    trades = db.get_paper_trades()
    by = {(t["ticker"], t["side"]): t for t in trades}
    assert by[("MU", "buy")]["trade_mode"] == "HYBRID"
    assert by[("MU", "sell")]["trade_mode"] == "HYBRID"
    assert by[("NVDA", "buy")]["trade_mode"] == "SEED"


def test_check_exit_triggers():
    """Intra-cycle price watch decisions: stop/target/trailing crosses fire,
    normal drift doesn't."""
    cfg = {"sell_rules": {"rules": {
        "stop_loss": {"enabled": True, "pct": 5.0},
        "take_profit": {"enabled": True, "pct": 10.0, "r_multiple": 3.0},
        "trailing_stop": {"enabled": True, "pct": 3.0},
    }}}
    pos = {"entry_price": 100.0, "trail_high": 100.0}
    assert paper_trader.check_exit_triggers(pos, 96.0, cfg) is None            # -4%: hold
    assert "stop_loss" in paper_trader.check_exit_triggers(pos, 94.9, cfg)     # -5.1%: stop
    assert "take_profit" in paper_trader.check_exit_triggers(pos, 110.5, cfg)  # +10.5%: target
    # trailing: ran to 108, fell 3%+ from the high (104.7) but above stop/entry
    pos_trail = {"entry_price": 100.0, "trail_high": 108.0}
    assert "trailing_stop" in paper_trader.check_exit_triggers(pos_trail, 104.5, cfg)
    assert paper_trader.check_exit_triggers(pos_trail, 105.5, cfg) is None     # within 3% of high
    # dynamic stop (Loop B) takes precedence over the flat %
    pos_dyn = {"entry_price": 100.0, "trail_high": 100.0, "current_stop_price": 98.0}
    assert "stop_loss" in paper_trader.check_exit_triggers(pos_dyn, 97.9, cfg)
    # no price -> no decision
    assert paper_trader.check_exit_triggers(pos, None, cfg) is None


def test_reset_wipes_only_sim_book(db):
    db.open_position("NVDA", 150.0, 1.0, 150.0, simulated=False)
    paper_trader.ensure_seeded(db, CFG)
    paper_trader.execute_buy(db, CFG, "MU", 50.0)
    db.reset_paper_account()
    assert db.get_paper_account() is None
    assert db.get_all_positions(simulated=True) == []
    assert db.get_paper_trades() == []
    assert len(db.get_all_positions(simulated=False)) == 1  # real book untouched
