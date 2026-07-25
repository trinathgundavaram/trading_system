"""§16 (E-9) - entry context persists, and one book never writes to the other.

The evaluation report noted that positions.risk_per_share and
entry_signal_score were NULL on all 39 rows and attributed the complete
absence of take-profit exits to a persistence failure. On closer reading that
is only half right, and the distinction matters: entry seeding was added on
2026-07-20 and every trade in the database was opened on 16-17 July, so the
NULLs are mostly explained by the feature postdating the trades. The
consequence - no R-multiple target, hence zero take-profit exits in 29 trades
- stands exactly as reported. The cause is sequencing, not a bug.

The real bug was next to it, and worse. update_position_by_ticker had no
simulated filter, so it matched an open position in EITHER book. HCA was in
the live watchlist and simultaneously an $8,553 SYNC position; a $100 paper
entry in HCA would have overwritten the real row's stop with one computed for
a hundred-dollar trade. update_trail_high had the identical defect and fires
every cycle rather than once at entry.

    python3 -m pytest tests/test_cross_book_writes.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _cfg():
    return {
        "trading": {"watch_execute": "WATCH", "max_positions": 10,
                    "trade_size_usd": 100, "mode": "SWING"},
        "paper_trading": {"starting_cash": 10000.0},
        "risk": {"kill_switch_triggered": False, "max_trades_per_day": 100,
                 "max_daily_loss_usd": 100000, "max_daily_loss_pct": 0,
                 "max_intraday_drawdown_pct": 0, "max_running_drawdown_pct": 0},
    }


# ── The guard ───────────────────────────────────────────────────────────────

def test_unscoped_update_raises(db):
    """Raising on None rather than defaulting to False is the whole design.
    A silent default would leave an unmigrated call site writing to the REAL
    book - the more dangerous direction, and an invisible one."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    with pytest.raises(ValueError, match="requires simulated"):
        db.update_position_by_ticker("AAPL", {"stop_state": "INITIAL_RISK"})


def test_unscoped_trail_high_raises(db):
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    with pytest.raises(ValueError, match="requires simulated"):
        db.update_trail_high("AAPL", 110.0)


# ── Entry seeding actually persists ─────────────────────────────────────────

def test_entry_seed_persists(db):
    """The feature has never had a live trade flow through it, so confirm it
    works before relying on it."""
    from engine import paper_trader
    cfg = _cfg()
    paper_trader.ensure_seeded(db, cfg)
    seed = {"entry_signal_score": 58.0, "entry_regime": "bull",
            "setup_type": "pullback", "risk_per_share": 1.25}
    paper_trader.execute_buy(db, cfg, "AAPL", 100.0, pattern_id=None, entry_seed=seed)

    pos = db.get_open_position("AAPL", simulated=True)
    assert pos["risk_per_share"] == pytest.approx(1.25)
    assert pos["entry_signal_score"] == pytest.approx(58.0)
    assert pos["current_stop_price"] == pytest.approx(98.75)    # 100 - 1.25
    assert pos["current_target_price"] == pytest.approx(103.75)  # 100 + 1.25*3
    assert pos["stop_state"] == "INITIAL_RISK"


def test_seed_gives_the_position_an_r_multiple_target(db):
    """The reported consequence, stated as a property: without
    current_target_price there is no take-profit level, which is why 29 trades
    produced zero take-profit exits."""
    from engine import paper_trader
    cfg = _cfg()
    paper_trader.ensure_seeded(db, cfg)
    paper_trader.execute_buy(db, cfg, "AAPL", 100.0,
                             entry_seed={"risk_per_share": 2.0})
    pos = db.get_open_position("AAPL", simulated=True)
    assert pos["current_target_price"] is not None
    r = (pos["current_target_price"] - pos["entry_price"]) / \
        (pos["entry_price"] - pos["current_stop_price"])
    assert r == pytest.approx(3.0)


# ── The one that matters ────────────────────────────────────────────────────

def test_seed_does_not_touch_the_other_book(db):
    """THE test. It is also the test that, had it existed, would have made
    this bug impossible to ship.

    HCA in miniature: a real holding at $500/share and a $100 paper entry in
    the same ticker. The paper stop must not land on the real row."""
    from engine import paper_trader
    cfg = _cfg()
    paper_trader.ensure_seeded(db, cfg)
    db.open_position("AAPL", 500.0, 10.0, 5000.0, simulated=False, trade_mode="SYNC")

    paper_trader.execute_buy(db, cfg, "AAPL", 100.0,
                             entry_seed={"risk_per_share": 1.25})

    real = db.get_open_position("AAPL", simulated=False)
    assert real["current_stop_price"] is None or \
        real["current_stop_price"] != pytest.approx(98.75)
    assert real["entry_price"] == pytest.approx(500.0)   # untouched


def test_trail_high_does_not_cross_books(db):
    """scheduler.py ratchets paper_position and position on consecutive lines
    for the same ticker. Unscoped, each write hit both rows, so the second
    silently overwrote the first with the other book's number - and trail_high
    is what the trailing stop is computed from."""
    db.open_position("HCA", 400.0, 20.0, 8000.0, simulated=False, trade_mode="SYNC")
    db.open_position("HCA", 100.0, 1.0, 100.0, simulated=True)

    db.update_trail_high("HCA", 105.0, simulated=True)

    assert db.get_open_position("HCA", simulated=True)["trail_high"] == pytest.approx(105.0)
    real = db.get_open_position("HCA", simulated=False)
    assert real["trail_high"] == pytest.approx(400.0)   # its own entry, unmoved


def test_trail_high_still_ratchets_within_a_book(db):
    """CONTROL - GREATEST means it only ever goes up. A scoped statement that
    also stopped ratcheting would pass the test above."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    db.update_trail_high("AAPL", 110.0, simulated=True)
    db.update_trail_high("AAPL", 105.0, simulated=True)
    assert db.get_open_position("AAPL", simulated=True)["trail_high"] == pytest.approx(110.0)


def test_live_seed_does_not_touch_the_paper_book(db):
    """The same bug pointing the other way: a live fill seeding the paper
    mirror instead of the position it just opened."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True)
    db.open_position("AAPL", 500.0, 10.0, 5000.0, simulated=False)

    db.update_position_by_ticker("AAPL", {"current_stop_price": 495.0},
                                 simulated=False)

    assert db.get_open_position("AAPL", simulated=True)["current_stop_price"] is None
    assert db.get_open_position("AAPL", simulated=False)["current_stop_price"] == pytest.approx(495.0)
