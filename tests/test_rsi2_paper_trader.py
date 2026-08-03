"""RSI(2) SPY sleeve - PAPER trading (engine/rsi2_paper_trader.py, storage/
database.py's rsi2_account/rsi2_positions). Mirrors tests/test_paper_
trading.py's structure and its `db` fixture (tests/conftest.py - real
ephemeral Postgres, clean schema per test). Runs against a throwaway
schema, zero broker/MCP calls.

    python3 -m pytest tests/test_rsi2_paper_trader.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import rsi2_paper_trader

CFG = {"rsi2_sleeve": {"paper_starting_cash": 1000.0}}


def test_ensure_seeded_creates_account_once(db):
    account = rsi2_paper_trader.ensure_seeded(db, CFG)
    assert account["cash"] == 1000.0
    assert account["starting_cash"] == 1000.0
    assert account["realized_pnl"] == 0

    db.adjust_rsi2_cash(-100.0)  # simulate some activity
    account2 = rsi2_paper_trader.ensure_seeded(db, CFG)  # must NOT reset it
    assert account2["cash"] == 900.0


def test_ensure_seeded_isolated_from_paper_account(db):
    """The whole point of the dedicated table - seeding this sleeve must
    not touch (or be confused with) paper_trading's own paper_account."""
    rsi2_paper_trader.ensure_seeded(db, CFG)
    assert db.get_paper_account() is None  # untouched, never seeded


def test_execute_buy_debits_full_cash_and_opens_position(db):
    rsi2_paper_trader.ensure_seeded(db, CFG)
    trade = rsi2_paper_trader.execute_buy(db, CFG, price=100.0, entry_date="2026-08-03")
    assert trade["shares"] == 10.0
    assert trade["dollar_amount"] == 1000.0

    account = db.get_rsi2_account()
    assert account["cash"] == 0.0  # 100% allocated, per the sleeve's design

    position = db.get_open_rsi2_position()
    assert position["ticker"] == "SPY"
    assert position["entry_price"] == 100.0
    assert position["shares"] == 10.0
    assert position["status"] == "open"


def test_execute_buy_skips_when_already_in_position(db):
    rsi2_paper_trader.ensure_seeded(db, CFG)
    rsi2_paper_trader.execute_buy(db, CFG, price=100.0, entry_date="2026-08-03")
    second = rsi2_paper_trader.execute_buy(db, CFG, price=105.0, entry_date="2026-08-04")
    assert second == {}
    # cash/position from the FIRST buy must be untouched by the skipped second call
    assert db.get_rsi2_account()["cash"] == 0.0
    assert db.get_open_rsi2_position()["entry_price"] == 100.0


def test_execute_buy_skips_when_no_cash(db):
    rsi2_paper_trader.ensure_seeded(db, {"rsi2_sleeve": {"paper_starting_cash": 0.0}})
    trade = rsi2_paper_trader.execute_buy(db, CFG, price=100.0)
    assert trade == {}
    assert db.get_open_rsi2_position() is None


def test_execute_sell_credits_cash_and_pnl_and_closes_position(db):
    rsi2_paper_trader.ensure_seeded(db, CFG)
    rsi2_paper_trader.execute_buy(db, CFG, price=100.0, entry_date="2026-08-03")
    trade = rsi2_paper_trader.execute_sell(db, CFG, price=110.0, reason="rsi_overbought", exit_date="2026-08-05")

    assert trade["proceeds"] == 1100.0
    assert trade["realized_pnl"] == 100.0

    account = db.get_rsi2_account()
    assert account["cash"] == 1100.0
    assert account["realized_pnl"] == 100.0
    assert db.get_open_rsi2_position() is None  # closed

    # the CLOSED row itself should be findable via a plain query, not lost
    with db._conn() as conn:
        import sqlite3
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rsi2_positions WHERE status = 'closed'").fetchone()
        assert row["exit_reason"] == "rsi_overbought"
        assert row["realized_pnl"] == 100.0


def test_execute_sell_with_nothing_open_is_a_noop(db):
    rsi2_paper_trader.ensure_seeded(db, CFG)
    trade = rsi2_paper_trader.execute_sell(db, CFG, price=100.0, reason="time_stop")
    assert trade == {}
    assert db.get_rsi2_account()["cash"] == 1000.0  # unchanged


def test_snapshot_marks_open_position_to_market(db):
    rsi2_paper_trader.ensure_seeded(db, CFG)
    rsi2_paper_trader.execute_buy(db, CFG, price=100.0, entry_date="2026-08-03")
    snap = rsi2_paper_trader.snapshot(db, current_price=120.0)
    assert snap["cash"] == 0.0
    assert snap["market_value"] == 10.0 * 120.0
    assert snap["total_value"] == 1200.0
    assert snap["total_return_pct"] == 20.0


def test_snapshot_before_seeding(db):
    snap = rsi2_paper_trader.snapshot(db)
    assert snap == {"seeded": False}


def test_round_trip_buy_then_sell_realistic_sequence_matches_manual_math(db):
    """A small, hand-verified two-trade sequence - confirms the ledger
    arithmetic end to end, not just each method in isolation."""
    rsi2_paper_trader.ensure_seeded(db, CFG)  # $1000 starting cash

    rsi2_paper_trader.execute_buy(db, CFG, price=100.0, entry_date="2026-08-03")   # 10 shares, cash -> $0
    rsi2_paper_trader.execute_sell(db, CFG, price=110.0, reason="rsi_overbought",
                                    exit_date="2026-08-05")                         # proceeds $1100, pnl +$100

    account = db.get_rsi2_account()
    assert account["cash"] == 1100.0
    assert account["realized_pnl"] == 100.0

    # second round trip, this time a loss
    rsi2_paper_trader.execute_buy(db, CFG, price=110.0, entry_date="2026-08-06")   # 10 shares, cash -> $0
    rsi2_paper_trader.execute_sell(db, CFG, price=99.0, reason="time_stop",
                                    exit_date="2026-08-10")                         # proceeds $990, pnl -$110

    account = db.get_rsi2_account()
    assert account["cash"] == pytest_approx(990.0)
    assert account["realized_pnl"] == pytest_approx(-10.0)  # net across both trades: +100 - 110


def pytest_approx(x):
    import pytest
    return pytest.approx(x, abs=1e-6)
