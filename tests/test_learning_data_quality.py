"""§15 - the learning tables are quarantined, and P&L is computed once.

mae_mfe_data held rows that are not trades: NVDA at +6.67% held for 12
milliseconds, MU at +10.00% for 10ms, a ticker literally named "AAA", all
three with MAE and MFE of exactly 0.0. Separately, ADPT appeared as -1.88%
over 6.34h in paper_trades and -3.20% over 5.0h in mae_mfe_data - the same
trade, two answers, because close_position() computed the figures for the
ledger and the MAE/MFE path recomputed them from a re-fetched row.

The classifier tests run anywhere. The rest need Postgres.

    python3 -m pytest tests/test_learning_data_quality.py -v
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.mae_mfe_engine import _classify_quality


# ── The write-time classifier ───────────────────────────────────────────────

def test_millisecond_hold_is_synthetic():
    """NVDA at +6.67% held for 0.0000034 hours. Nothing real closes in 12ms;
    the fastest configured cycle is minutes apart."""
    assert _classify_quality(0.0, 0.0, 6.67, 0.0000034) == "synthetic"


def test_zero_excursion_with_a_nonzero_outcome_is_synthetic():
    """Arithmetically impossible: the outcome IS an excursion, so a trade that
    moved cannot have had zero MAE and zero MFE."""
    assert _classify_quality(0.0, 0.0, -3.2, 5.0) == "synthetic"


def test_zero_excursion_with_a_zero_outcome_is_ok():
    """CONTROL. A genuine scratch trade - flat outcome, no excursion recorded -
    is unusual but not impossible, and must not be swept up."""
    assert _classify_quality(0.0, 0.0, 0.0, 5.0) == "ok"


def test_a_real_trade_is_ok():
    assert _classify_quality(1.4, 3.1, 2.2, 6.34) == "ok"


def test_unparseable_numbers_are_synthetic():
    assert _classify_quality("x", None, 5.0, 1.0) == "synthetic"


def test_a_short_but_possible_hold_is_ok():
    """0.01h is 36 seconds. A 20-minute day trade is real and must survive."""
    assert _classify_quality(0.5, 0.8, 1.1, 0.33) == "ok"


# ── One authoritative outcome (needs a database) ────────────────────────────

def test_close_position_returns_hold_hours(db):
    entry = (datetime.utcnow() - timedelta(hours=6, minutes=20)).isoformat()
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True, entry_time=entry)
    closed = db.close_position("AAPL", 110.0, simulated=True)
    assert closed["hold_hours"] == pytest.approx(6.33, abs=0.05)


def test_hold_hours_survives_an_unparseable_entry_time(db):
    """Recorded as 0 with a warning rather than raising. A sell must not fail
    because a timestamp is malformed - that would leave the position open."""
    db.open_position("AAPL", 100.0, 1.0, 100.0, simulated=True, entry_time="not-a-date")
    closed = db.close_position("AAPL", 110.0, simulated=True)
    assert closed["hold_hours"] == 0.0
    assert closed["pnl_pct"] == pytest.approx(10.0)


def test_one_trade_one_answer(db):
    """THE test - the ADPT case. The ledger and the learning table must agree,
    because they are now reading the same computation rather than each doing
    their own."""
    from engine import paper_trader
    cfg = {"trading": {"watch_execute": "WATCH", "max_positions": 10,
                       "trade_size_usd": 100, "mode": "SWING"},
           "paper_trading": {"starting_cash": 1000.0},
           "risk": {"kill_switch_triggered": False, "max_trades_per_day": 100,
                    "max_daily_loss_usd": 10000, "max_daily_loss_pct": 0}}
    paper_trader.ensure_seeded(db, cfg)
    entry = (datetime.utcnow() - timedelta(hours=6, minutes=20)).isoformat()
    db.open_position("ADPT", 100.0, 1.0, 100.0, simulated=True, entry_time=entry)
    db.adjust_paper_cash(-100.0)

    paper_trader.execute_sell(db, "ADPT", 98.12, reason="stop", cfg=cfg)

    ledger = [t for t in db.get_paper_trades(limit=50)
              if t["ticker"] == "ADPT" and t["side"] == "sell"][0]
    learned = db.get_recent_mae_mfe(limit=50, include_quarantined=True)
    learned = [m for m in learned if m["ticker"] == "ADPT"][0]

    assert learned["outcome_pct"] == pytest.approx(ledger["pnl_pct"], abs=1e-6)
    assert learned["hold_hours"] == pytest.approx(6.33, abs=0.05)


# ── The quarantine filter ───────────────────────────────────────────────────

def _mae_row(db, ticker, quality, outcome=5.0, setup="pullback", regime="bull"):
    """§C1: no explicit id. It was `f"id-{ticker}-{quality}"`, which is a TEXT
    value, and mae_mfe_data.id is a BIGINT identity column as of
    migrations/012 - nothing referenced the old uuid4 strings, so the column
    stopped being the caller's to supply. trade_id is left NULL here on
    purpose: these tests are about the data_quality filter, and none of them
    joins back to a pattern."""
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO mae_mfe_data
               (ticker, setup_type, regime, mae_pct, mfe_pct, outcome_pct,
                hold_hours, recorded_at, data_quality)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ticker, setup, regime, 1.0, 2.0, outcome,
             5.0, datetime.utcnow().isoformat(), quality))


def test_quarantined_rows_do_not_train(db):
    _mae_row(db, "GOOD", "ok")
    _mae_row(db, "FAKE", "synthetic")
    _mae_row(db, "OLD", "pre_stop_fix")
    assert len(db.query_mae_winners("pullback", "bull")) == 1


def test_forensics_can_still_see_them(db):
    """Marked, not deleted - so the evidence of how the contamination happened
    survives. A reader that cannot see them defeats the point of keeping them."""
    _mae_row(db, "FAKE", "synthetic")
    assert db.get_recent_mae_mfe(limit=50) == []
    assert len(db.get_recent_mae_mfe(limit=50, include_quarantined=True)) == 1


def test_record_completed_marks_a_synthetic_row_on_the_way_in(db):
    """Migration 007 cleans up what is there; this stops the same shape coming
    back. A cleanup that only runs once has to be run again."""
    from engine.mae_mfe_engine import record_completed
    record_completed({"ticker": "AAA", "pnl_pct": 10.0, "hold_hours": 0.000003,
                      "max_adverse_excursion_pct": 0, "max_favorable_excursion_pct": 0})
    assert db.get_recent_mae_mfe(limit=50) == []
    assert len(db.get_recent_mae_mfe(limit=50, include_quarantined=True)) == 1


def test_patterns_filter_quarantined_rows(db):
    import json
    with db._conn() as conn:
        for i, q in enumerate(("ok", "pre_stop_fix", "synthetic")):
            conn.execute(
                """INSERT INTO pattern_database
                   (ticker, mode, recorded_at, features, outcome_pct, hold_hours,
                    is_closed, data_quality)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f"T{i}", "SWING", datetime.utcnow().isoformat(), json.dumps({}),
                 2.0, 5.0, 1, q))
    assert len(db.get_patterns(closed_only=True)) == 1
    assert len(db.get_patterns(closed_only=True, include_quarantined=True)) == 3
