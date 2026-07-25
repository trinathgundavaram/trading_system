"""§5 - SYNC/SEED positions must never be exited by an automated path.

This is the test the remediation plan requires to exist before live execution
is ever re-armed. As of the 2026-07-24 audit the quarantined rows totalled
roughly $42,000 of real holdings - including 200 shares of NFLX and 22.6 of
HCA - and they carried LIVE stop machinery. An ATR stop computed for a $100
engine-sized entry could have liquidated an $8,500 holding.

Three layers are tested independently, because a single guard on that exposure
would be a single point of failure:

  layer 1  storage/database.py     the query never returns them
  layer 2  rules/sell_rules.py     the sell rules refuse to evaluate them
  layer 3  engine/live_trader.py   execution refuses the order

The CONTROL tests matter as much as the guard tests. Without them, a broken
fixture makes every guard test pass for the wrong reason - which is precisely
the failure mode that lets a quarantine look watertight while doing nothing.

    python3 -m pytest tests/test_sync_quarantine.py -v
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from rules.sell_rules import UNMANAGED_TRADE_MODES, SellRulesEngine

CFG = {
    "risk_level": "TURBO",
    "risk": {"TURBO": {"stop_loss_swing_pct": 8, "stop_loss_day_pct": 4}},
    "sell_rules": {
        "rules": {
            "stop_loss": {"enabled": True, "pct": 5.0},
            "take_profit": {"enabled": True, "pct": 10.0, "r_multiple": 3.0},
            "trailing_stop": {"enabled": True, "pct": 3.0},
            "earnings_approaching": {"enabled": False, "days_before": 2},
            "vix_spike": {"enabled": False, "threshold": 28},
        },
        "custom_rules": [],
    },
}


def _td(price=300.0):
    return types.SimpleNamespace(price=price, days_to_earnings=999)


def _mkt(vix=15.0):
    return types.SimpleNamespace(vix_level=vix)


def _pos(mode, **kw):
    """A position whose stop is set impossibly high, so ANY evaluation that
    reaches the stop check MUST fire. If evaluate() returns should_sell=False
    for this row, it can only be because the quarantine caught it first."""
    base = dict(ticker="HCA", entry_price=379.23, shares=22.55, trade_mode=mode,
                current_stop_price=999_999.0, stop_state="INITIAL_RISK",
                trail_high=379.23, risk_per_share=0.0)
    base.update(kw)
    return base


# ── layer 2: the sell rules ─────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["SYNC", "sync", "SyNc", "SEED", "seed"])
def test_sell_rules_never_exit_unmanaged(mode):
    r = SellRulesEngine().evaluate(_td(), _pos(mode), _mkt(), CFG)
    assert r.should_sell is False
    assert "unmanaged" in r.reason


def test_sell_rules_do_exit_managed():
    """CONTROL. The same impossible stop on a SWING row MUST fire - this is
    what proves the harness itself works and the tests above are meaningful."""
    r = SellRulesEngine().evaluate(_td(), _pos("SWING"), _mkt(), CFG)
    assert r.should_sell is True


def test_sell_rules_do_exit_null_trade_mode():
    """CONTROL. A NULL/absent trade_mode is a legacy engine row, not an
    imported holding - it must stay managed. Getting this backwards would
    silently stop the engine exiting its own oldest positions."""
    r = SellRulesEngine().evaluate(_td(), _pos(None), _mkt(), CFG)
    assert r.should_sell is True


def test_sell_rules_quarantine_precedes_every_other_check():
    """The guard must sit before the config lookup, not after. A SYNC row is
    refused even when cfg is missing the sell_rules section entirely - if the
    guard were placed later this would raise KeyError instead."""
    r = SellRulesEngine().evaluate(_td(), _pos("SYNC"), _mkt(), {})
    assert r.should_sell is False


# ── the mirrored constants must not drift ───────────────────────────────────

def test_unmanaged_mode_constants_agree():
    """rules/sell_rules.py and engine/live_trader.py each keep their own
    literal copy of the excluded modes so they stay importable without the
    Postgres driver. That is a deliberate trade, and this test is the price
    of it: the copies must never diverge from storage/database.py's canonical
    tuple."""
    psycopg2 = pytest.importorskip("psycopg2", reason="canonical constant lives in storage.database")
    from storage.database import MANAGED_EXCLUDED_MODES, is_unmanaged_mode

    assert set(UNMANAGED_TRADE_MODES) == set(MANAGED_EXCLUDED_MODES)
    assert is_unmanaged_mode("sync") is True
    assert is_unmanaged_mode("SEED") is True
    assert is_unmanaged_mode(None) is False
    assert is_unmanaged_mode("SWING") is False


def test_live_trader_constant_agrees():
    lt = pytest.importorskip("engine.live_trader",
                             reason="requires the live-execution import chain")
    assert set(lt.UNMANAGED_TRADE_MODES) == set(UNMANAGED_TRADE_MODES)


class _FakeDB:
    """Minimal stand-in - execute_sell_live only needs get_open_position and
    log_ui_event before the refusal point."""

    def __init__(self, pos):
        self._pos = pos
        self.events = []

    def get_open_position(self, ticker, simulated=False):
        return self._pos

    def log_ui_event(self, kind, payload):
        self.events.append((kind, payload))


@pytest.fixture()
def live_trader(monkeypatch):
    lt = pytest.importorskip("engine.live_trader",
                             reason="requires the live-execution import chain")
    # Open every gate ABOVE the quarantine check, so that a failure here can
    # only mean the quarantine itself did not hold.
    monkeypatch.setattr(lt, "is_live_mode", lambda cfg: True)
    monkeypatch.setattr(lt, "is_live_execution_enabled", lambda cfg: True)
    monkeypatch.setattr(lt.breaker, "available", lambda: True)
    monkeypatch.setattr(lt, "_login", lambda: pytest.fail(
        "reached _login() - an unmanaged sell got past the layer-3 refusal"))
    return lt


# ── layer 1: the query never returns them ───────────────────────────────────
# Needs a real Postgres, and therefore an OPT-IN scratch database. It skips by
# default. Phase 2 §12 replaces this with a proper per-test-database fixture.
#
#     createdb trading_platform_test
#     TP_TEST_POSTGRES_DB=trading_platform_test python3 -m pytest \
#         tests/test_sync_quarantine.py -v
#
# The opt-in is not ceremony. storage/database.py's own __init__ comment
# records a real 2026-07-20 incident in which a "supposedly-isolated"
# integration test wrote a live TEST buy and sell into the production
# database, including a real hit to paper_account's realized P&L. Defaulting
# to Database() here would recreate exactly that, so this refuses to run
# against anything but a database the operator named explicitly - and refuses
# outright if that name looks like the production one.

@pytest.fixture()
def pgdb(monkeypatch):
    psycopg2 = pytest.importorskip("psycopg2")
    target = os.getenv("TP_TEST_POSTGRES_DB")
    if not target:
        pytest.skip("set TP_TEST_POSTGRES_DB=<scratch db> to run the layer-1 "
                    "database tests (see the comment above this fixture)")
    if target == os.getenv("POSTGRES_DB", "trading_platform"):
        pytest.fail("TP_TEST_POSTGRES_DB points at the live database - refusing "
                    "to write test rows into it")

    monkeypatch.setenv("POSTGRES_DB", target)
    import storage.database as database
    monkeypatch.setattr(database, "PG_DB", target)
    monkeypatch.setattr(database, "_POOL", None)   # force a new pool on the scratch db
    try:
        d = database.Database()
        d.init_db()
    except psycopg2.OperationalError as e:
        pytest.skip(f"scratch database {target!r} not reachable: {e}")

    if any(p["ticker"].startswith("ZZQTEST") for p in d.get_all_positions()):
        pytest.fail("leftover ZZQTEST rows present - clean them before re-running")
    yield d
    with d._conn() as conn:
        conn.execute("DELETE FROM positions WHERE ticker LIKE 'ZZQTEST%'")


def test_get_managed_positions_excludes_unmanaged(pgdb):
    pgdb.open_position("ZZQTESTA", 100.0, 1.0, 100.0, simulated=True, trade_mode="SWING")
    pgdb.open_position("ZZQTESTB", 100.0, 1.0, 100.0, simulated=True, trade_mode="SEED")
    pgdb.open_position("ZZQTESTC", 100.0, 1.0, 100.0, simulated=True, trade_mode="sync")
    pgdb.open_position("ZZQTESTD", 100.0, 1.0, 100.0, simulated=True, trade_mode=None)

    managed = {p["ticker"] for p in pgdb.get_managed_positions(simulated=True)
               if p["ticker"].startswith("ZZQTEST")}
    every = {p["ticker"] for p in pgdb.get_all_positions(simulated=True)
             if p["ticker"].startswith("ZZQTEST")}

    # CONTROL: get_all_positions still sees all four, so an empty `managed`
    # cannot be mistaken for a working filter.
    assert every == {"ZZQTESTA", "ZZQTESTB", "ZZQTESTC", "ZZQTESTD"}
    assert managed == {"ZZQTESTA", "ZZQTESTD"}


def test_is_managed(pgdb):
    pgdb.open_position("ZZQTESTE", 100.0, 1.0, 100.0, simulated=True, trade_mode="SWING")
    pgdb.open_position("ZZQTESTF", 100.0, 1.0, 100.0, simulated=True, trade_mode="SYNC")
    assert pgdb.is_managed("ZZQTESTE", simulated=True) is True
    assert pgdb.is_managed("ZZQTESTF", simulated=True) is False
    # 'not managed' must never be read as 'not held' - a ticker with no
    # position at all also returns False, so callers must not use this to
    # decide whether it is safe to BUY.
    assert pgdb.is_managed("ZZQTESTNONE", simulated=True) is False


# ── layer 3: execution refuses the order ────────────────────────────────────

@pytest.mark.parametrize("mode", ["SYNC", "seed"])
def test_execute_sell_live_refuses_automated_unmanaged(live_trader, mode):
    db = _FakeDB(_pos(mode))
    out = live_trader.execute_sell_live(db, CFG, "HCA", reason="stop",
                                        require_auto_trade=True)
    assert out == {}
    assert db.events and db.events[0][0] == "unmanaged_sell_blocked"


def test_execute_sell_live_allows_manual_unmanaged(live_trader, monkeypatch):
    """A human clicking Sell for one named ticker (token + re-typed ticker at
    the API layer) must still work. Losing the ability to manually exit a real
    position would itself be a risk, so require_auto_trade=False skips the
    refusal - proven here by reaching _login(), the next step."""
    reached = {"login": False}

    def _login():
        reached["login"] = True
        return False   # stop the call here; we only care that we got past the guard

    monkeypatch.setattr(live_trader, "_login", _login)
    db = _FakeDB(_pos("SYNC"))
    live_trader.execute_sell_live(db, CFG, "HCA", reason="manual_ui",
                                  require_auto_trade=False)
    assert reached["login"] is True
    assert not db.events
