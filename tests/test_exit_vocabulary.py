"""§50/§51/§54 (Phase 2.5): the exit vocabulary, the pattern<->excursion join,
and the absence of the retired risk surface.

These three ship together because they share one property: each is a place
where the system was recording something that could not afterwards be counted.
exit_reason could not be grouped, mae_mfe_data could not be joined without
mixing tickers, and rules/risk_rules.py held two answers to the same limit.
"""
import json

import pytest

from rules.common import EXIT_KINDS, classify_exit

# Declared here rather than imported from tests/test_paper_trading.py. There is
# no tests/__init__.py, so `from tests.test_paper_trading import CFG` raises
# ModuleNotFoundError under the suite's rootdir - and adding one to make a
# cross-import work would change how pytest resolves every module in this
# directory, which is a large change to make for a shared constant.
#
# Limits are wide open on purpose: these tests are about the exit vocabulary
# and the excursion join, so a buy blocked by a risk limit would be a silent
# false pass. The limits themselves are tested in test_daily_budget.py and
# test_drawdown.py.
CFG = {
    "trading": {"watch_execute": "WATCH", "trade_size_usd": 100, "max_positions": 3},
    "paper_trading": {"starting_cash": 500.0},
    "risk": {
        "kill_switch_triggered": False,
        "max_trades_per_day": 1000,
        "max_daily_loss_usd": 1_000_000,
        "max_daily_loss_pct": 0,
        "max_intraday_drawdown_pct": 0,
        "max_running_drawdown_pct": 0,
    },
}


# ── §50: the exit vocabulary ────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expected", [
    # Namespaced tokens generated FROM a structured value. scheduler.py's price
    # watch builds these as f"price_watch:{reason.split(' ')[0]}" off
    # check_exit_triggers()'s fixed vocabulary, so reading them back is reading
    # a token, not parsing prose.
    ("paper_price_watch:stop_loss", "stop_loss"),
    ("paper_price_watch:trailing_stop", "trailing_stop"),
    ("paper_price_watch:take_profit", "take_profit"),
    ("live_price_watch:stop_loss", "stop_loss"),
    # Fixed literals passed by their one call site each.
    ("time_based_close", "time_stop"),
    ("manual_fill_confirmed", "manual"),
    ("paper_rotation: MAN health 31 vs candidate 78", "rotation"),
])
def test_structured_reasons_classify(reason, expected):
    assert classify_exit(reason) == expected


@pytest.mark.parametrize("reason", [
    # THE IMPORTANT CASES. Every one of these is genuinely a stop, and every
    # one is prose assembled per-trade with the price interpolated into it.
    # Prefix-matching "Dynamic stop hit" here would work today and would
    # silently stop working the first time anyone rewords that message, which
    # is the failure mode the whole column exists to avoid. None is the honest
    # answer until rules/sell_rules.py emits a structured code at the point of
    # decision (Phase 3).
    "paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $82.56 <= stop $83.15",
    "paper_sell_rules:Earnings in 0 days",
    "live_sell_rules:Thesis broken",
    "loop_b_urgent:RISK CONTROL — KILL SWITCH",
    "seeded_from_real_portfolio",
    "",
    None,
])
def test_prose_reasons_stay_unclassified(reason):
    assert classify_exit(reason) is None


def test_every_classified_value_is_in_the_closed_set():
    """The column is only worth having if its domain is closed. A classifier
    that can emit a value outside EXIT_KINDS reintroduces the ungroupable
    column it was written to replace."""
    samples = ["paper_price_watch:stop_loss", "time_based_close",
               "manual_fill_confirmed", "paper_rotation: X", "sell_rules:whatever",
               "price_watch:something_invented"]
    for s in samples:
        kind = classify_exit(s)
        assert kind is None or kind in EXIT_KINDS


def test_paper_stop_close_writes_both_columns(db):
    """The reason keeps its sentence; the kind becomes countable."""
    from engine import paper_trader
    from learning.pattern_database import PatternDatabase

    paper_trader.ensure_seeded(db, CFG)
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    paper_trader.execute_buy(db, CFG, "FIX", 40.0, pattern_id=pid)
    paper_trader.execute_sell(db, "FIX", 38.0, reason="price_watch:stop_loss",
                              pattern_db=pdb, cfg=CFG)

    row = db.get_patterns(mode="SWING", ticker="FIX", closed_only=True)[0]
    assert row["exit_reason"] == "paper_price_watch:stop_loss"   # unchanged
    assert row["exit_kind"] == "stop_loss"                       # new, countable


def test_prose_close_leaves_exit_kind_null(db):
    """NULL means "not determinable", and that has to survive the round trip -
    a close that writes 'rule_exit' by default would fill the column with a
    bucket nobody measured."""
    from engine import paper_trader
    from learning.pattern_database import PatternDatabase

    paper_trader.ensure_seeded(db, CFG)
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    paper_trader.execute_buy(db, CFG, "FIX", 40.0, pattern_id=pid)
    paper_trader.execute_sell(db, "FIX", 44.0, reason="sell_rules:Earnings in 0 days",
                              pattern_db=pdb, cfg=CFG)

    row = db.get_patterns(mode="SWING", ticker="FIX", closed_only=True)[0]
    assert row["exit_reason"] == "paper_sell_rules:Earnings in 0 days"
    assert row["exit_kind"] is None


def test_explicit_exit_kind_beats_the_derivation(db):
    from learning.pattern_database import PatternDatabase
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    # A reason classify_exit() would return None for, closed by a caller that
    # knows better - scheduler.py's time-based close does exactly this.
    pdb.close_trade(pid, 1.0, 5.0, exit_reason="something bespoke",
                    exit_kind="time_stop")
    assert db.get_patterns(ticker="FIX", closed_only=True)[0]["exit_kind"] == "time_stop"


def test_invalid_exit_kind_is_refused_not_stored(db):
    """One typo'd value that never appears again makes the column uncountable
    again, so the write is dropped rather than trusted. The reason - the part a
    human reads - is unaffected."""
    from learning.pattern_database import PatternDatabase
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    pdb.close_trade(pid, 1.0, 5.0, exit_reason="paper_price_watch:stop_loss",
                    exit_kind="stopped_out")     # not in EXIT_KINDS
    row = db.get_patterns(ticker="FIX", closed_only=True)[0]
    assert row["exit_kind"] is None
    assert row["exit_reason"] == "paper_price_watch:stop_loss"
    assert row["is_closed"] == 1                 # the close itself still happened


# ── §51: the pattern <-> excursion join ─────────────────────────────────────

def _closed_pattern_with_excursion(db, ticker, trade_id, mae, mfe,
                                    mae_ticker=None):
    """Write a closed pattern and an excursion row that claims `trade_id`.

    `mae_ticker` defaults to `ticker`; pass a different one to reproduce the
    collision found in the 2026-07-25 data, where mae_mfe_data.trade_id = '1'
    was claimed by five different symbols at once.
    """
    with db._conn() as conn:
        row = conn.execute(
            """INSERT INTO pattern_database
                 (trade_id, ticker, mode, recorded_at, features, outcome_pct,
                  hold_hours, exit_reason, exit_kind, is_closed)
               VALUES (?,?,?,?,?,?,?,?,?,1) RETURNING id""",
            (str(trade_id), ticker, "SWING", "2026-07-25T12:00:00",
             json.dumps({"_entry_price": 10.0}), 2.0, 5.0,
             "paper_price_watch:stop_loss", "stop_loss")).fetchone()
        pid = row["id"] if hasattr(row, "keys") else row[0]
        conn.execute(
            """INSERT INTO mae_mfe_data
                 (id, trade_id, ticker, setup_type, regime, mae_pct, mfe_pct,
                  outcome_pct, hold_hours, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"{ticker}-{trade_id}-{mae}", str(trade_id), mae_ticker or ticker,
             "breakout", "BULL", mae, mfe, 2.0, 5.0, "2026-07-25T12:00:00"))
    return pid


def test_excursion_join_matches_each_pattern_to_its_own_row(db):
    _closed_pattern_with_excursion(db, "USB", 23, mae=0.25, mfe=0.99)
    _closed_pattern_with_excursion(db, "SHEL", 31, mae=0.23, mfe=0.39)

    got = {r["ticker"]: r for r in db.get_pattern_excursions()}
    assert set(got) == {"USB", "SHEL"}
    assert got["USB"]["mae_pct"] == pytest.approx(0.25)
    assert got["SHEL"]["mae_pct"] == pytest.approx(0.23)


def test_colliding_trade_id_does_not_attach_to_another_ticker(db):
    """The 2026-07-25 shape, reproduced: an NVDA excursion row claiming the
    same trade_id as an ADPT trade. The transitive join through positions
    returned 37 rows for 23 patterns because of exactly this. A row that claims
    a trade belonging to a different symbol is a collision, not a near miss."""
    _closed_pattern_with_excursion(db, "ADPT", 1, mae=4.14, mfe=1.26)
    # Same trade_id, different symbol - the junk row.
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO mae_mfe_data
                 (id, trade_id, ticker, setup_type, regime, mae_pct, mfe_pct,
                  outcome_pct, hold_hours, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("nvda-collision", "1", "NVDA", "breakout", "BULL",
             0.0, 0.0, 6.67, 0.1, "2026-07-25T12:00:00"))

    rows = db.get_pattern_excursions()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ADPT"
    assert rows[0]["mae_pct"] == pytest.approx(4.14)   # not NVDA's 0.0


def test_patterns_without_a_link_are_absent_not_zero(db):
    """A pattern recorded before §51 has trade_id NULL. It must not appear with
    mae_pct = 0 - that is a measurement claim nobody made."""
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO pattern_database
                 (trade_id, ticker, mode, recorded_at, features, outcome_pct,
                  hold_hours, exit_reason, is_closed)
               VALUES (NULL,?,?,?,?,?,?,?,1)""",
            ("OLD", "SWING", "2026-07-01T12:00:00",
             json.dumps({"_entry_price": 10.0}), 2.0, 5.0, "paper_sell_rules:x"))
    assert db.get_pattern_excursions() == []


def test_quarantined_excursions_are_excluded(db):
    """§15 marked contaminated rows instead of deleting them so the forensic
    evidence survived. A reader that ignores the mark puts the evidence back
    into the averages - and get_recent_mae_mfe() already filters this way, so
    disagreeing here would mean one table reporting two populations depending
    on which accessor you called."""
    _closed_pattern_with_excursion(db, "USB", 23, mae=0.25, mfe=0.99)
    _closed_pattern_with_excursion(db, "MU", 3, mae=9.9, mfe=0.0)
    with db._conn() as conn:
        conn.execute("UPDATE mae_mfe_data SET data_quality = 'synthetic' "
                     "WHERE UPPER(ticker) = 'MU'")

    rows = db.get_pattern_excursions()
    assert [r["ticker"] for r in rows] == ["USB"]


def test_paper_buy_links_the_pattern_to_its_position(db):
    """The whole point of §51: trade_id stops being NULL on every row."""
    from engine import paper_trader
    from learning.pattern_database import PatternDatabase

    paper_trader.ensure_seeded(db, CFG)
    pdb = PatternDatabase(db)
    pid = pdb.record_entry("FIX", "SWING", {"_entry_price": 40.0})
    assert db.get_pattern_by_id(pid)["trade_id"] is None     # signal time

    paper_trader.execute_buy(db, CFG, "FIX", 40.0, pattern_id=pid)
    pos = db.get_open_position("FIX", simulated=True)
    assert db.get_pattern_by_id(pid)["trade_id"] == str(pos["id"])


# ── §54: the retired surface stays retired ──────────────────────────────────

@pytest.mark.parametrize("name", [
    "check_kill_switch", "check_max_trades_per_day", "check_max_daily_loss",
    "check_buying_power", "check_position_limits", "check_position_size_limit",
    "LegacyRiskEngine", "RiskCheckResult",
])
def test_dead_risk_surface_is_gone(name):
    """These had zero call sites while the limits they described were enforced
    elsewhere, and they had already diverged from the live definitions -
    LegacyRiskEngine compared trades with >= where RiskEngine uses >, and read
    max_daily_loss_usd raw where the live path uses §8's equity-scaled limit.
    Re-adding one is how a future edit lands in the copy nobody runs."""
    import rules.risk_rules as rr
    assert not hasattr(rr, name), (
        f"{name} is back in rules/risk_rules.py. If a caller now needs it, "
        f"write it against RiskEngine/daily_loss_limit rather than restoring "
        f"the pre-§8 arithmetic.")


def test_catalog_defaults_match_config():
    """§54/F7: engine/rules_catalog.py is the operator-facing description of
    the system's own rules and had drifted - it documented
    max_intraday_drawdown_pct as 3.0% while config.yaml said 2.0. A catalogue
    that can disagree with the config is worse than no catalogue, because it
    is the thing someone reads INSTEAD of the config."""
    import re
    from config_loader import load_config_dict
    from engine.rules_catalog import ACCOUNT_RISK_CATALOG

    risk = load_config_dict().get("risk", {}) or {}
    blob = " ".join(c["description"] for c in ACCOUNT_RISK_CATALOG["checks"])
    for key, actual in risk.items():
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            continue
        # Only checks keys the catalogue actually quotes a default for.
        for quoted in re.findall(rf"{re.escape(key)}\s*\(default ([0-9.]+)%?\)", blob):
            assert float(quoted) == pytest.approx(float(actual)), (
                f"rules_catalog says {key} defaults to {quoted}, config.yaml "
                f"says {actual}")


def test_every_account_risk_check_names_where_it_is_enforced():
    """The catalogue attributed all eight checks to rules/risk_rules.py, where
    a dead copy of three of them lived. Naming the real site per entry is what
    stops that recurring."""
    from engine.rules_catalog import ACCOUNT_RISK_CATALOG
    for check in ACCOUNT_RISK_CATALOG["checks"]:
        assert check.get("enforced_in"), f"{check['name']} has no enforced_in"
        assert ".py" in check["enforced_in"]
