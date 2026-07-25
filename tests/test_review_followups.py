"""§C1/§C2/§C3/§D - the second 2026-07-25 external review's follow-ups.

That review read `main` at v1.2.0 and reported all seven of its findings as
unfixed; they had landed three commits later in b08bced. What survived
adjudication were four things Phase 2.5 genuinely had not done:

  §C1  mae_mfe_data had no FK to positions and a uuid4 TEXT primary key.
  §C2  robinhood_sync.py's seed-paper called reset_paper_account() with no
       backup and no confirmation, one statement after printing what it was
       about to destroy.
  §C3  the packet reported the high-volatility count without saying which
       quantity it counted - and §53 changed that quantity.
  §D   classify_exit() refuses to classify `sell_rules:` prose, which is the
       most common exit path, so exit_kind stayed NULL there.

Most of these are tested WITHOUT a database on purpose. The vocabulary
mapping, the sell-rule token and the packet label are pure functions of their
inputs, and a test that needs Postgres to check a string is a test that gets
skipped on the machine where someone is actually editing the string.
"""
import inspect
import io
import re
import tokenize

import pytest

from rules.common import (EXIT_KINDS, classify_exit, exit_kind_for_loop_b_label,
                          exit_kind_for_stop_state)


def code_only(src: str) -> str:
    """Python source with comments and string literals removed.

    Several tests below assert that a construct is ABSENT from a file. Every
    one of them tripped on its own explanation the first time it ran - the
    comment saying "db._lock is gone" contains the string "db._lock", and the
    docstring saying "was a uuid4 string" contains "uuid". Asserting on prose
    is how a test starts failing because someone documented the fix well.
    """
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return "\n".join(out)


def strip_comments(src: str) -> str:
    """Python source with COMMENTS removed but string literals intact - for the
    cases where the thing being looked for IS a string (an SQL statement in a
    conn.execute, say)."""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return "\n".join(out)


def sql_code_only(src: str) -> str:
    """Same idea for SQL: drop -- comment lines. migrations/012 spends forty
    lines explaining why ON DELETE CASCADE would be wrong, which is not the
    same as using it."""
    return "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("--"))


# ── §D: the stop state carries the distinction ──────────────────────────────

@pytest.mark.parametrize("state,expected", [
    # Entry-anchored: the stop has never moved off initial risk, so hitting it
    # is capping a loss.
    ("INITIAL_RISK", "stop_loss"),
    ("TRADE_CONFIRMING", "stop_loss"),
    # Ratcheted above entry: hitting it is giving back a gain. Folding these
    # in with the two above is how p_stop_loss would count winners as
    # stop-outs and come out biased high.
    ("BREAKEVEN", "trailing_stop"),
    ("PROFIT_PROTECT", "trailing_stop"),
    ("TREND_FOLLOWING", "trailing_stop"),
    # Fires on the thesis failing, not on price reaching a risk limit.
    # stop_state_machine.py already treats it as outside the ordinary
    # progression - it is absent from that file's state ranking.
    ("THESIS_BROKEN", "rule_exit"),
])
def test_stop_state_maps_to_its_own_kind(state, expected):
    assert exit_kind_for_stop_state(state) == expected
    assert expected in EXIT_KINDS


@pytest.mark.parametrize("state", [None, "", "SOMETHING_NEW", "  "])
def test_unknown_stop_state_falls_back_to_stop_loss_not_none(state):
    """Deliberately asymmetric with classify_exit(), which returns None when it
    cannot tell. Here we already KNOW a stop was hit - that came from the
    trigger, not from parsing a sentence - so the only open question is which
    flavour. It is also the right answer for the fallback path in sell_rules,
    where the stop machine has not run yet and current_stop_price is 0."""
    assert exit_kind_for_stop_state(state) == "stop_loss"


def test_stop_state_enum_agrees_with_the_table():
    """engine/stop_state_machine.py exposes .exit_kind off the enum, and
    rules/common.py holds the table. Two copies of a closed set is how a closed
    set stops being closed, so the enum must not grow a member the table has
    never heard of."""
    from engine.stop_state_machine import StopState
    from rules.common import STOP_STATE_EXIT_KINDS
    for member in StopState:
        assert member.value in STOP_STATE_EXIT_KINDS, (
            f"StopState.{member.name} has no entry in STOP_STATE_EXIT_KINDS. "
            f"Add one - the fallback would silently record it as stop_loss.")
        assert member.exit_kind in EXIT_KINDS


@pytest.mark.parametrize("label,expected", [
    ("EOD FLATTEN — DAY POSITION", "eod_flatten"),
    ("THESIS BROKEN — URGENT EXIT", "rule_exit"),
    ("EXIT", "rule_exit"),
    ("REDUCE POSITION (50%)", "rule_exit"),
    ("RISK CONTROL — KILL SWITCH", "rule_exit"),
    ("", "rule_exit"),
    (None, "rule_exit"),
])
def test_loop_b_labels_collapse_to_two_kinds(label, expected):
    """Only the end-of-day flatten is a distinct kind - it is a clock event and
    nothing to do with the position's merits. Everything else is the unified
    Exit Score acting. Giving each label its own kind would produce six buckets
    of a handful of rows each, which then have to be re-merged before anyone
    can ask the question the column exists for."""
    assert exit_kind_for_loop_b_label(label) == expected
    assert expected in EXIT_KINDS


# ── §D: sell_rules emits the token at the trigger ───────────────────────────

class _TD:
    def __init__(self, price, days_to_earnings=99):
        self.price = price
        self.days_to_earnings = days_to_earnings


class _MKT:
    def __init__(self, vix_level=15.0):
        self.vix_level = vix_level


SELL_CFG = {
    "risk_level": "MODERATE",
    "risk": {"MODERATE": {"stop_loss_swing_pct": 5, "stop_loss_day_pct": 2.5}},
    "sell_rules": {"rules": {
        "stop_loss": {"enabled": True, "pct": 5.0},
        "trailing_stop": {"enabled": True, "pct": 3.0},
        "take_profit": {"enabled": True, "r_multiple": 3.0, "pct": 10.0},
        "earnings_approaching": {"enabled": True, "days_before": 2},
        "vix_spike": {"enabled": True, "threshold": 40.0},
    }},
}


def _evaluate(position, price, **kw):
    from rules.sell_rules import SellRulesEngine
    return SellRulesEngine().evaluate(
        _TD(price, kw.pop("days_to_earnings", 99)),
        position, _MKT(kw.pop("vix_level", 15.0)), SELL_CFG)


def test_dynamic_stop_in_initial_risk_is_a_stop_loss():
    pos = {"entry_price": 100.0, "current_stop_price": 95.0,
           "stop_state": "INITIAL_RISK", "trail_high": 100.0}
    r = _evaluate(pos, 94.0)
    assert r.should_sell and r.exit_kind == "stop_loss"


def test_the_same_mechanism_in_trend_following_is_a_trailing_stop():
    """The bug this prevents, concretely: identical trigger, identical reason
    string shape, but one is a loss being capped and the other is a winner
    giving some back. Only stop_state tells them apart, and by the time
    close_pattern() sees the sentence the state is inside a parenthesis."""
    pos = {"entry_price": 100.0, "current_stop_price": 118.0,
           "stop_state": "TREND_FOLLOWING", "trail_high": 125.0}
    r = _evaluate(pos, 117.0)
    assert r.should_sell and r.exit_kind == "trailing_stop"


def test_fallback_path_still_distinguishes_stop_from_trail():
    """No dynamic stop yet (first cycle after entry), so there is no state to
    consult - but stop_rule_name already carries the distinction."""
    pos = {"entry_price": 100.0, "current_stop_price": 0, "trail_high": 100.0}
    assert _evaluate(pos, 94.0).exit_kind == "stop_loss"


def test_take_profit_and_event_exits_get_their_kinds():
    pos = {"entry_price": 100.0, "current_stop_price": 0, "trail_high": 100.0,
           "risk_per_share": 2.0}
    assert _evaluate(pos, 107.0).exit_kind == "take_profit"      # 3R = 106

    calm = {"entry_price": 100.0, "current_stop_price": 0, "trail_high": 100.0}
    # Event avoidance is neither a stop nor a target. "rule_exit" rather than
    # an invented "earnings" kind, which would give one config flag its own row
    # in every future GROUP BY.
    assert _evaluate(calm, 100.0, days_to_earnings=1).exit_kind == "rule_exit"
    assert _evaluate(calm, 100.0, vix_level=45.0).exit_kind == "rule_exit"


def test_no_sell_means_no_kind():
    pos = {"entry_price": 100.0, "current_stop_price": 90.0,
           "stop_state": "INITIAL_RISK", "trail_high": 100.0}
    r = _evaluate(pos, 100.0)
    assert not r.should_sell and r.exit_kind == ""


def test_every_sell_result_kind_is_in_the_vocabulary():
    """close_pattern() rejects an unrecognised exit_kind and stores NULL. A
    sell rule emitting one would therefore go BACKWARDS - it would look like it
    was recording something while writing the same NULL §D exists to remove."""
    cases = [
        ({"entry_price": 100.0, "current_stop_price": 95.0,
          "stop_state": "INITIAL_RISK", "trail_high": 100.0}, 94.0, {}),
        ({"entry_price": 100.0, "current_stop_price": 118.0,
          "stop_state": "TREND_FOLLOWING", "trail_high": 125.0}, 117.0, {}),
        ({"entry_price": 100.0, "current_stop_price": 0,
          "trail_high": 100.0, "risk_per_share": 2.0}, 107.0, {}),
        ({"entry_price": 100.0, "current_stop_price": 0,
          "trail_high": 100.0}, 100.0, {"days_to_earnings": 1}),
        ({"entry_price": 100.0, "current_stop_price": 0,
          "trail_high": 100.0}, 100.0, {"vix_level": 45.0}),
    ]
    for pos, price, kw in cases:
        r = _evaluate(pos, price, **kw)
        assert r.should_sell, (pos, price, kw)
        assert r.exit_kind in EXIT_KINDS, f"{r.exit_kind!r} not in EXIT_KINDS"


def test_unmanaged_positions_still_return_no_kind():
    """§5: SYNC/SEED are not this engine's to close. That must not have grown
    an exit_kind as a side effect of threading one through."""
    for mode in ("SYNC", "SEED"):
        r = _evaluate({"entry_price": 100.0, "trade_mode": mode}, 1.0)
        assert not r.should_sell and r.exit_kind == ""


# ── §D: the token survives the journey to close_pattern ─────────────────────

@pytest.mark.parametrize("fn_path,param", [
    ("engine.paper_trader:execute_sell", "exit_kind"),
    ("engine.live_trader:execute_sell_live", "exit_kind"),
])
def test_close_paths_accept_an_exit_kind(fn_path, param):
    """The token is decided in sell_rules and consumed in close_pattern. Every
    hop between them has to carry it or the decision is lost in transit - which
    is precisely how it was being lost before §D."""
    mod_name, fn_name = fn_path.split(":")
    # live_trader pulls in mcp_clients, which needs the `mcp` package. It is a
    # real requirement, so this runs everywhere the platform actually runs -
    # but a bare checkout should skip rather than fail, the same way the 93
    # Postgres-gated tests do.
    mod = pytest.importorskip(mod_name)
    sig = inspect.signature(getattr(mod, fn_name))
    assert param in sig.parameters, (
        f"{fn_path} dropped {param}. sell_rules decides the exit kind at the "
        f"trigger; a hop that does not pass it through silently reverts these "
        f"rows to NULL.")
    assert sig.parameters[param].default is None


def test_manual_ui_sells_are_recorded_as_manual():
    """server.py's Sell button passed reason='manual_ui', which classify_exit()
    never recognised - only confirm_fill.py's 'manual_fill_confirmed'. So the
    one exit whose kind is least ambiguous (a human pressed a button) was
    landing as NULL."""
    import re
    src = open("server.py").read()
    for m in re.finditer(r'reason="manual_ui"', src):
        window = src[max(0, m.start() - 400):m.start() + 400]
        assert 'exit_kind="manual"' in window, (
            "a manual_ui sell path is not passing exit_kind='manual'")
    # classify_exit still does not know the string - that is fine and expected,
    # because the call site states it explicitly. Pinned so that a future
    # "let's just parse it" change has to confront this comment.
    assert classify_exit("manual_ui") is None


def test_rotation_victims_are_recorded_as_rotation():
    """A rotated-out position is not closed on its own merits at all. Recording
    it as anything else puts a forced exit in the same bucket as a decided
    one."""
    for path in ("engine/paper_trader.py", "engine/live_trader.py"):
        src = open(path).read()
        assert 'exit_kind="rotation"' in src, f"{path} lost the rotation kind"


# ── §C3: the packet says which quantity it counted ──────────────────────────

class _PR:
    """Minimal stand-in for PortfolioRiskResult - the packet formatter reads
    attributes, not a type."""
    def __init__(self, high_vol=2, proxy=0):
        self.sector = "Tech"
        self.themes = []
        self.sector_exposure_pct = 10.0
        self.theme_exposure_pct = 0.0
        self.portfolio_beta = 1.0
        self.max_pairwise_correlation = 0.3
        self.high_vol_position_count = high_vol
        self.high_vol_proxy_count = proxy
        self.size_multiplier = 1.0
        self.allowed = True
        self.reasons = []
        self.warnings = []


def _packet_text(pr):
    """The label is built by packet_builder.high_vol_line(), which was pulled
    out of build_ticker_packet for exactly this reason - the wording IS the
    deliverable, and reaching it through the full packet builder would mean
    constructing a TickerData, a BuyResult and a MarketContext to assert on
    one string."""
    from engine.packet_builder import high_vol_line
    return high_vol_line(pr)


def test_high_vol_line_names_its_unit():
    """Pre-§53 this count meant stop distance; post-§53 it means entry ATR%.
    Same label, different quantity, and the old one read systematically low -
    so an operator comparing two packets across the boundary was comparing
    numbers that are not comparable."""
    text = _packet_text(_PR(high_vol=2, proxy=0))
    assert "High-vol positions open (by entry ATR%): 2" in text


def test_proxy_share_is_shown_when_the_count_is_a_mixture():
    """While any position predates migrations/011 the count mixes measured and
    estimated rows. Reported as a plain integer, the number looks measured
    whichever way it was arrived at."""
    text = _packet_text(_PR(high_vol=3, proxy=1))
    assert "by entry ATR%" in text
    assert "est. from stop distance" in text
    assert "reads LOW" in text


def test_no_proxy_note_once_the_book_has_turned_over():
    text = _packet_text(_PR(high_vol=3, proxy=0))
    assert "est. from stop distance" not in text


def test_portfolio_risk_result_defaults_proxy_count_to_zero():
    """The field is new; every existing construction site must keep working."""
    from engine.portfolio_risk import PortfolioRiskResult
    r = PortfolioRiskResult(
        allowed=True, size_multiplier=1.0, sector="Tech", themes=[],
        sector_exposure_pct=0.0, theme_exposure_pct=0.0, portfolio_beta=1.0,
        max_pairwise_correlation=0.0, high_vol_position_count=0)
    assert r.high_vol_proxy_count == 0


def test_proxy_measurement_is_reported_as_such():
    from engine.portfolio_risk import _position_atr_pct_measured
    measured = {"ticker": "X", "entry_price": 100.0,
                "current_stop_price": 95.0, "entry_atr_pct": 7.0}
    assert _position_atr_pct_measured(measured) == (7.0, False)

    legacy = {"ticker": "Y", "entry_price": 100.0, "current_stop_price": 95.0}
    value, used_proxy = _position_atr_pct_measured(legacy)
    assert used_proxy is True and value == pytest.approx(5.0)


# ── §C2: the destructive path is gated ──────────────────────────────────────

def test_seed_paper_requires_a_confirmation_phrase_and_a_backup():
    src = open("robinhood_sync.py").read()
    assert 'SEED_PAPER_CONFIRM_PHRASE = "RESET PAPER ACCOUNT"' in src

    # The gate has to come BEFORE the reset, not after the print that describes
    # it - which is exactly what it used to do.
    body = src[src.index("def cmd_seed_paper"):]
    body = body[body.index("db = Database()"):]
    assert body.index("_require_backup") < body.index("db.reset_paper_account()")
    assert body.index("confirm") < body.index("db.reset_paper_account()")


def test_seed_paper_phrase_differs_from_the_live_trading_one():
    """Muscle memory from one must not satisfy the other."""
    live = pytest.importorskip("engine.live_trader")
    import robinhood_sync
    assert (robinhood_sync.SEED_PAPER_CONFIRM_PHRASE
            != live.LIVE_EXECUTION_CONFIRM_PHRASE)


def test_backup_failure_aborts_rather_than_prompting():
    """The one condition under which this must not proceed. Prompting here
    would only relocate the mistake."""
    src = open("robinhood_sync.py").read()
    guard = src[src.index("def _require_backup"):src.index("def cmd_status")]
    assert "sys.exit(1)" in guard
    assert "returncode != 0" in guard


def test_the_redundant_equity_history_delete_is_gone():
    """§48 moved this inside reset_paper_account(). Left here it was harmless
    but actively misleading - a reader would conclude the reset does NOT clear
    the curve and add a compensating delete somewhere else too."""
    raw = open("robinhood_sync.py").read()
    # The DELETE statement specifically. Reading the curve to report how many
    # points are about to be destroyed is fine and is what the gate does.
    assert "DELETE FROM paper_equity_history" not in strip_comments(raw)
    # Private API reached from a top-level script. code_only here, not
    # strip_comments: the §C2 comment explains what was removed by naming it.
    code = code_only(raw)
    assert "_lock" not in code and "_conn" not in code


def test_reset_is_not_reachable_from_the_server():
    """It is a CLI operation behind a backup and a phrase. A UI route reaching
    it would bypass both."""
    assert "reset_paper_account" not in code_only(open("server.py").read())


# ── §C1: mae_mfe_data's contract ────────────────────────────────────────────

def test_insert_mae_mfe_does_not_mint_an_id_on_a_migrated_schema():
    """id is a BIGINT identity column as of migrations/012. It was a uuid4
    string minted here for no reason beyond the table having been created that
    way - nothing in the repository ever referenced it.

    The uuid path still EXISTS, behind the legacy probe - see the test below -
    so this asserts on the column list actually sent, not on the absence of the
    word "uuid" from the function."""
    from storage.database import Database
    src = inspect.getsource(Database.insert_mae_mfe)
    # Columns are assembled in a list; id is prepended only in the legacy
    # branch, and that branch is guarded.
    assert '"trade_id", "ticker", "setup_type"' in src
    assert 'cols.insert(0, "id")' in src
    # code_only: the comment above the guard explains the uuid fallback, and
    # asserting on prose is how a test starts failing because someone
    # documented the fix well.
    body = code_only(src[src.index("cols = ["):])
    assert body.index("_mae_id_is_legacy_text") < body.index("uuid")


def test_pre_012_databases_still_get_an_id():
    """CREATE TABLE IF NOT EXISTS is a no-op on a database that already has
    this table, so shipping this code without having run migrations/012 leaves
    `id TEXT PRIMARY KEY` - NOT NULL, no default - in place. Omitting id there
    is not a schema mismatch a test would catch; it is every MAE/MFE write
    raising at 3pm on a Tuesday.

    Supplying a uuid when the old column is still present removes the deploy
    ORDER constraint entirely: code and migration can land in either sequence.
    That matters for a system whose scheduler restarts on a timer rather than
    when someone is watching."""
    from storage.database import Database
    src = inspect.getsource(Database._mae_id_is_legacy_text)
    # Fails toward supplying an id. Supplying one the new schema does not need
    # is harmless - 012 makes the column GENERATED BY DEFAULT, not ALWAYS, so
    # an explicit value stays legal. Omitting one the old schema needs is not.
    assert "legacy = True" in src
    assert "except Exception" in src
    # And it must say so, once, loudly enough not to be forgotten.
    assert "migrations/012 has NOT been" in src
    assert "phase2_5_cutover.sh" in src


def test_identity_column_accepts_an_explicit_id():
    """The compatibility shim above depends on this: GENERATED BY DEFAULT, not
    GENERATED ALWAYS. With ALWAYS, a process that probed the schema before 012
    was applied would start failing the moment it was."""
    sql = sql_code_only(open("migrations/012_mae_mfe_fk.sql").read())
    assert "GENERATED BY DEFAULT AS IDENTITY" in sql
    assert "GENERATED ALWAYS" not in sql


def test_insert_mae_mfe_refuses_a_non_numeric_trade_id():
    """trade_id references positions(id). A caller handing us something
    non-numeric should fail where it knows what it meant, not write a row that
    can never be joined."""
    from storage.database import Database
    db = Database.__new__(Database)          # no connection needed to reach the guard
    with pytest.raises(ValueError, match="not a position id"):
        db.insert_mae_mfe({"trade_id": "not-a-number", "ticker": "X"})


def test_migration_012_sets_null_rather_than_cascading():
    """CASCADE would make reset_paper_account() destroy the excursion history
    its own docstring promises to keep - and nobody would notice until an MAE
    average came back thin."""
    sql = open("migrations/012_mae_mfe_fk.sql").read()
    assert "ON DELETE SET NULL" in sql_code_only(sql)
    assert "ON DELETE CASCADE" not in sql_code_only(sql)
    # It must refuse to run on dirty data rather than failing later with a cast
    # error that names a row instead of the problem.
    assert "RAISE EXCEPTION" in sql
    assert "repair_test_damage.py" in sql


def test_schema_and_migration_agree_about_mae_mfe():
    """A fresh install builds from database.py's DDL and never runs the
    migrations. If the two disagree, a new machine gets a different table from
    a migrated one - and every §C1 guarantee holds on only one of them."""
    schema = open("storage/database.py").read()
    block = schema[schema.index("CREATE TABLE IF NOT EXISTS mae_mfe_data"):]
    block = block[:block.index(");")]
    assert "BIGINT GENERATED BY DEFAULT AS IDENTITY" in block
    assert "trade_id INTEGER REFERENCES positions (id) ON DELETE SET NULL" in block


# ── the cutover runbook ─────────────────────────────────────────────────────

def test_cutover_is_dry_run_by_default_and_ordered():
    """B5 before B3 gets a migration that aborts; B8 before B6 calibrates
    against a curve spanning a purse re-seed. The ordering is not advisory,
    which is why this is a script and not a checklist."""
    src = open("scripts/phase2_5_cutover.sh").read()
    assert "STEPS=(B1 B2 B3 B4 B5 B6 B7 B8 B9)" in src
    assert '[ -z "$APPLY" ]' in src
    # The reset needs an explicit purse value - refusing to guess is the point.
    assert "Refusing to guess" in src
    assert "RESET_CONFIRM_PHRASE" in src
