"""§14 - the position-opening race.

execute_buy() read get_open_position() near the top and called open_position()
about a hundred lines later: two auto-committed transactions with six
ThreadPoolExecutor workers between them. The max_positions count and the cash
check had the same shape. Nothing in the schema protected positions - the only
CREATE UNIQUE in storage/database.py was on news_items(ticker, headline).

Every test here needs a real database. There is no useful fake for this
section: the whole finding is that application-level checks cannot hold, so a
test that faked the storage layer would be asserting against the very thing
that does not work. If these skip, §14 is unverified on this machine.

    python3 -m pytest tests/test_concurrency.py -v
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine import paper_trader


def _cfg(**trading):
    base = {"watch_execute": "WATCH", "max_positions": 10,
            "trade_size_usd": 100, "mode": "SWING"}
    base.update(trading)
    return {
        "trading": base,
        "paper_trading": {"starting_cash": 10000.0},
        "risk": {"kill_switch_triggered": False, "max_trades_per_day": 1000,
                 "max_daily_loss_usd": 100000, "max_daily_loss_pct": 0,
                 "max_intraday_drawdown_pct": 0, "max_running_drawdown_pct": 0},
    }


def _parallel(fn, items, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ── The invariant, at the storage layer ─────────────────────────────────────

def test_duplicate_open_is_rejected_by_the_index(db):
    """Structural, not procedural. open_position() takes no lock, so this is
    the index alone doing the work."""
    assert db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    assert db.open_position("AAPL", 101.0, 1.0, 100.0, simulated=True) is None
    rows = [p for p in db.get_all_positions(simulated=True) if p["ticker"] == "AAPL"]
    assert len(rows) == 1
    assert rows[0]["entry_price"] == pytest.approx(100.0)   # the first one survives


def test_duplicate_open_does_not_raise(db):
    """Deliberate, and the reason is where the live callers sit. live_trader
    calls open_position AFTER a real order has filled; an exception there
    would mean a fill that happened and was never recorded - the account
    holding shares the system has no row for. That is strictly worse than the
    duplicate the index prevents, so the collision is survivable and loud."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=False)
    db.open_position("AAPL", 101.0, 1.0, 100.0, simulated=False)   # must not raise


def test_the_two_books_are_not_the_same_position(db):
    """One open AAPL per BOOK, not one across both. The paper mirror of a real
    holding is a legitimate second row."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=False)
    assert db.get_open_position("AAPL", simulated=True)
    assert db.get_open_position("AAPL", simulated=False)


def test_a_closed_position_does_not_block_reentry(db):
    """The index is partial for exactly this reason: it constrains what is
    held now, not what was ever held. Re-entering a name you traded last week
    has to keep working."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    db.close_position("AAPL", 110.0, simulated=True)
    db.open_position("AAPL", 108.0, 1.0, 100.0, simulated=True)
    assert db.get_open_position("AAPL", simulated=True)["entry_price"] == pytest.approx(108.0)


def test_try_open_position_returns_none_instead_of_raising(db):
    """The loser of a race must get an empty result, not an exception -
    otherwise the normal path lives inside an error handler."""
    assert db.try_open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    assert db.try_open_position("AAPL", 100.0, 1.0, 100.0, simulated=True) is None


def test_try_open_position_respects_the_cap(db):
    for i in range(3):
        assert db.try_open_position(f"T{i}", 10.0, 1.0, 10.0, simulated=True,
                                    max_positions=3)
    assert db.try_open_position("T9", 10.0, 1.0, 10.0, simulated=True,
                                max_positions=3) is None


def test_seed_rows_do_not_consume_the_cap(db):
    """SEED is an informational mirror of the real account. Counting it would
    starve out genuine signals just because the real book holds a lot of
    names (2026-07-23)."""
    for i in range(5):
        db.open_position(f"S{i}", 10.0, 1.0, 10.0, simulated=True, trade_mode="SEED")
    assert db.try_open_position("NEW", 10.0, 1.0, 10.0, simulated=True,
                                max_positions=3)


# ── The purse ───────────────────────────────────────────────────────────────

def test_conditional_debit_refuses_to_overdraw(db):
    db.init_paper_account(100.0)
    assert db.try_debit_paper_cash(60.0) is True
    assert db.try_debit_paper_cash(60.0) is False
    assert db.get_paper_account()["cash"] == pytest.approx(40.0)


def test_failed_open_does_not_charge_the_purse(db):
    """The debit and the insert are one transaction. A position that was never
    opened must not have been paid for."""
    db.init_paper_account(1000.0)
    db.try_open_position("AAPL", 100.0, 1.0, 100.0, simulated=True,
                         debit_paper_cash=100.0)
    before = db.get_paper_account()["cash"]
    assert db.try_open_position("AAPL", 100.0, 1.0, 100.0, simulated=True,
                                debit_paper_cash=100.0) is None
    assert db.get_paper_account()["cash"] == pytest.approx(before)


def test_insufficient_cash_does_not_open_a_position(db):
    """And the converse: a position that could not be paid for must not exist."""
    db.init_paper_account(50.0)
    assert db.try_open_position("AAPL", 100.0, 1.0, 100.0, simulated=True,
                                debit_paper_cash=100.0) is None
    assert db.get_open_position("AAPL", simulated=True) is None


# ── The race itself ─────────────────────────────────────────────────────────

def test_parallel_buys_same_ticker_open_once(db):
    """THE test. Six workers, one ticker, one position. This is the exact
    shape of a cycle where the same name qualifies twice."""
    cfg = _cfg()
    paper_trader.ensure_seeded(db, cfg)
    _parallel(lambda _: paper_trader.execute_buy(db, cfg, "AAPL", 100.0,
                                                 pattern_id=None), range(6))
    rows = [p for p in db.get_all_positions(simulated=True) if p["ticker"] == "AAPL"]
    assert len(rows) == 1


def test_parallel_buys_respect_max_positions(db):
    """Six workers each reading 'open_count = 2 of 3' and all inserting is the
    variant a unique index cannot catch, because 'too many rows' is not
    something uniqueness can express."""
    cfg = _cfg(max_positions=3)
    paper_trader.ensure_seeded(db, cfg)
    _parallel(lambda t: paper_trader.execute_buy(db, cfg, t, 10.0),
              [f"T{i}" for i in range(12)])
    assert len(db.get_managed_positions(simulated=True)) == 3


def test_parallel_buys_cannot_overdraw_the_purse(db):
    """Two concurrent buys reading $100 and both spending it is the same bug
    in a different table."""
    cfg = _cfg(trade_size_usd=40)
    db.init_paper_account(100.0)
    _parallel(lambda t: paper_trader.execute_buy(db, cfg, t, 10.0),
              [f"T{i}" for i in range(6)])
    assert db.get_paper_account()["cash"] >= 0


def test_purse_matches_the_ledger_after_a_parallel_cycle(db):
    """Not just non-negative - exactly right. A purse that stays positive
    while disagreeing with paper_trades by $40 is the silent accounting drift
    §15's reconciliation exists to catch, and it would pass the test above."""
    cfg = _cfg(trade_size_usd=40)
    db.init_paper_account(100.0)
    _parallel(lambda t: paper_trader.execute_buy(db, cfg, t, 10.0),
              [f"T{i}" for i in range(6)])
    acct = db.get_paper_account()
    spent = sum(float(t["dollar_amount"] or 0) for t in db.get_paper_trades(limit=1000)
                if t["side"] == "buy")
    assert acct["cash"] == pytest.approx(acct["starting_cash"] - spent, abs=0.01)


def test_budget_is_not_burned_by_a_lost_race(db):
    """§7's counter is incremented after the fill is recorded, so a worker
    that lost the race must not have consumed a slot in the daily budget."""
    cfg = _cfg()
    paper_trader.ensure_seeded(db, cfg)
    _parallel(lambda _: paper_trader.execute_buy(db, cfg, "AAPL", 100.0), range(6))
    assert db.trades_placed_today(simulated=True) == 1
