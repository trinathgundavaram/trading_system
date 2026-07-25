"""Portfolio Rotation Engine (engine/rotation.py) - guardrail-by-guardrail
victim selection, plus the end-to-end paper-book rotation through
engine/paper_trader.execute_buy. Throwaway SQLite file, zero MCP calls.

    python3 -m pytest tests/test_rotation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from storage.database import Database
from engine import paper_trader, rotation


CFG = {
    "trading": {"watch_execute": "WATCH", "trade_size_usd": 100, "max_positions": 2},
    "paper_trading": {"starting_cash": 1000.0},
    "rotation": {
        "enabled": True,
        "min_candidate_score": 85,
        "max_victim_health_score": 55,
        "min_hold_days": 3,
        "max_rotations_per_week": 2,
    },
}


@pytest.fixture()
def db(tmp_path):
    return Database(path=str(tmp_path / "test_rotation.db"))


def _open(db, ticker, health=None, days_held=None, price=100.0):
    db.open_position(ticker, price, 1.0, price, simulated=True)
    pos = db.get_open_position(ticker, simulated=True)
    updates = {}
    if health is not None:
        updates["position_health_score"] = health
    if days_held is not None:
        updates["days_held"] = days_held
    if updates:
        db.update_position(pos["id"], updates)
    return pos


def test_disabled_by_default(db):
    cfg = {**CFG, "rotation": {}}
    _open(db, "AAA", health=20, days_held=10)
    assert rotation.find_rotation_victim(db, cfg, "NEW", 95, simulated=True) is None


def test_no_score_no_rotation(db):
    _open(db, "AAA", health=20, days_held=10)
    assert rotation.find_rotation_victim(db, CFG, "NEW", None, simulated=True) is None


def test_candidate_below_bar(db):
    _open(db, "AAA", health=20, days_held=10)
    assert rotation.find_rotation_victim(db, CFG, "NEW", 80, simulated=True) is None


def test_healthy_holdings_never_sacrificed(db):
    _open(db, "AAA", health=80, days_held=10)
    _open(db, "BBB", health=70, days_held=10)
    assert rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True) is None


def test_unscored_position_never_eligible(db):
    _open(db, "AAA", health=None, days_held=10)  # Loop B hasn't judged it yet
    assert rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True) is None


def test_min_hold_days_protects_young_positions(db):
    _open(db, "AAA", health=20, days_held=1)
    assert rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True) is None


def test_weakest_eligible_loses(db):
    _open(db, "AAA", health=50, days_held=10)
    _open(db, "BBB", health=30, days_held=10)
    _open(db, "CCC", health=90, days_held=10)
    victim = rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True)
    assert victim and victim["ticker"] == "BBB"


def test_weekly_budget_enforced(db):
    _open(db, "AAA", health=20, days_held=10)
    db.log_rotation("PAPER", "X", 95, "Y", 20, 10, "r1")
    db.log_rotation("PAPER", "X", 95, "Z", 20, 10, "r2")
    assert db.count_recent_rotations(days=7, simulated=True) == 2
    assert rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True) is None


def test_budget_is_per_book(db):
    _open(db, "AAA", health=20, days_held=10)
    db.log_rotation("LIVE", "X", 95, "Y", 20, 10, "r1")
    db.log_rotation("LIVE", "X", 95, "Z", 20, 10, "r2")
    assert db.count_recent_rotations(days=7, simulated=True) == 0
    assert rotation.find_rotation_victim(db, CFG, "NEW", 95, simulated=True) is not None


def test_paper_buy_rotates_end_to_end(db):
    paper_trader.ensure_seeded(db, CFG)
    _open(db, "AAA", health=20, days_held=10, price=100.0)
    _open(db, "BBB", health=90, days_held=10, price=100.0)  # book full (cap 2)

    result = paper_trader.execute_buy(
        db, CFG, "NEW", 50.0, buy_score=95,
        prices={"AAA": 110.0, "BBB": 100.0})

    assert result.get("ticker") == "NEW"
    tickers = {p["ticker"] for p in db.get_all_positions(simulated=True)}
    assert tickers == {"BBB", "NEW"}          # weak AAA out, healthy BBB kept
    assert db.count_recent_rotations(days=7, simulated=True) == 1
    account = db.get_paper_account()
    # 1000 start + 110 sell proceeds - 100 buy = 1010
    assert account["cash"] == pytest.approx(1010.0)


def test_paper_buy_skips_without_victim_price(db):
    paper_trader.ensure_seeded(db, CFG)
    _open(db, "AAA", health=20, days_held=10)
    _open(db, "BBB", health=90, days_held=10)

    result = paper_trader.execute_buy(db, CFG, "NEW", 50.0, buy_score=95,
                                       prices={})  # no price for the victim
    assert result == {}
    assert len(db.get_all_positions(simulated=True)) == 2
    assert db.count_recent_rotations(days=7, simulated=True) == 0


def test_paper_buy_at_cap_without_rotation_still_skips(db):
    cfg = {**CFG, "rotation": {"enabled": False}}
    paper_trader.ensure_seeded(db, cfg)
    _open(db, "AAA", health=20, days_held=10)
    _open(db, "BBB", health=90, days_held=10)
    result = paper_trader.execute_buy(db, cfg, "NEW", 50.0, buy_score=95,
                                       prices={"AAA": 110.0})
    assert result == {}
    assert len(db.get_all_positions(simulated=True)) == 2
