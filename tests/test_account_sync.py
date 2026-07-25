"""engine/account_sync.py - the import/diff logic against the local real
book (the network-facing fetch is exercised only in production; this covers
apply_remote_positions, which owns every book mutation).

    python3 -m pytest tests/test_account_sync.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from storage.database import Database
from engine.account_sync import apply_remote_positions


@pytest.fixture()
def db(tmp_path):
    return Database(path=str(tmp_path / "test_sync.db"))


def test_imports_missing_positions(db):
    result = apply_remote_positions(db, [
        {"ticker": "NVDA", "shares": 2.0, "avg_cost": 150.0},
        {"ticker": "MU", "shares": 1.5, "avg_cost": 80.0},
    ])
    assert sorted(result["imported"]) == ["MU", "NVDA"]
    positions = {p["ticker"]: p for p in db.get_all_positions(simulated=False)}
    assert positions["NVDA"]["entry_price"] == 150.0
    assert positions["NVDA"]["shares"] == 2.0
    assert positions["NVDA"]["trade_mode"] == "SYNC"
    assert positions["MU"]["dollar_amount"] == pytest.approx(120.0)


def test_existing_positions_untouched(db):
    db.open_position("NVDA", 140.0, 3.0, 420.0, simulated=False)
    result = apply_remote_positions(db, [
        {"ticker": "NVDA", "shares": 2.0, "avg_cost": 150.0},
    ])
    assert result["imported"] == []
    pos = db.get_open_position("NVDA", simulated=False)
    assert pos["entry_price"] == 140.0 and pos["shares"] == 3.0  # unchanged


def test_never_auto_closes_local_only(db):
    db.open_position("ORCL", 120.0, 1.0, 120.0, simulated=False)
    result = apply_remote_positions(db, [])  # account is flat
    assert result["missing_remotely"] == ["ORCL"]
    assert db.get_open_position("ORCL", simulated=False) is not None  # still open


def test_paper_book_isolated(db):
    db.open_position("VRT", 90.0, 1.0, 90.0, simulated=True)  # paper only
    result = apply_remote_positions(db, [
        {"ticker": "VRT", "shares": 2.0, "avg_cost": 95.0},
    ])
    # Paper VRT is a different book - the real one gets imported.
    assert result["imported"] == ["VRT"]
    assert db.get_open_position("VRT", simulated=False)["entry_price"] == 95.0
    assert db.get_open_position("VRT", simulated=True)["entry_price"] == 90.0


def test_zero_cost_not_imported(db):
    result = apply_remote_positions(db, [
        {"ticker": "BAD", "shares": 1.0, "avg_cost": 0.0},
    ])
    assert result["imported"] == []
    assert db.get_open_position("BAD", simulated=False) is None
