"""migrations/012's ON DELETE SET NULL, exercised rather than read.

WHAT WAS ALREADY TESTED, AND WHY IT WAS NOT ENOUGH.

`tests/test_review_followups.py` asserts that the migration FILE contains the
string "ON DELETE SET NULL" and does not contain "ON DELETE CASCADE", and that
`storage/database.py`'s DDL says the same. Both are worth having: they catch
someone editing the policy by hand. Neither observes the policy taking effect.

The distinction matters more than usual here because the two things that could
go wrong are invisible to a text check:

  1. The migration is correct and simply has not been APPLIED to this database.
     The file still says SET NULL. Every existing test passes. The FK does not
     exist, and an orphaned trade_id is writable again.
  2. The FK exists under a different name with a different delete rule -
     `mae_mfe_data_trade_id_fkey` from a schema born post-012 versus
     `fk_mae_mfe_trade` added by the migration. The file is not consulted at
     runtime; the catalog is.

So these tests do the destructive operations the policy exists to survive -
a bare DELETE, and `reset_paper_account()`, which deletes every simulated
position by design - and assert what the excursion rows look like afterwards.
That is the difference between "correct in theory" and "provably correct
across destructive ops".

WHY SET NULL AND NOT CASCADE, restated because it is the substance of the
test: an MAE of -4.2% is a fact about a trade that happened, and it stays true
after the position row is deleted. What stops being true is that we can say
WHICH position it was. CASCADE would make `reset_paper_account()` silently
destroy the excursion history its own docstring promises to keep, and nobody
would notice until an MAE average came back thin - a number that is wrong in a
way nothing about it looks wrong.

These require Postgres (tests/conftest.py's `db` fixture). They skip where it
is unavailable, which is the same posture as every other DB-touching module
here.
"""
import pytest


def _position(db, ticker="AAPL", simulated=True):
    pid = db.open_position(ticker=ticker, entry_price=100.0, shares=1.0,
                           dollar_amount=100.0, simulated=simulated)
    assert pid, "fixture could not open a position"
    return pid


def _excursion(db, position_id, ticker="AAPL", mae=-4.2, mfe=6.1):
    db.insert_mae_mfe({
        "trade_id": position_id, "ticker": ticker, "setup_type": "breakout",
        "regime": "bull", "mae_pct": mae, "mfe_pct": mfe,
        "outcome_pct": 3.0, "hold_hours": 5.0, "data_quality": "ok",
    })


def _rows(db, ticker=None):
    sql = "SELECT trade_id, ticker, mae_pct FROM mae_mfe_data"
    params = ()
    if ticker:
        sql += " WHERE ticker = ?"
        params = (ticker,)
    with db._conn() as conn:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]


# ── the catalog, not the file ───────────────────────────────────────────────

def test_the_fk_exists_and_its_delete_rule_is_set_null(db):
    """Reads pg_constraint, which is what actually governs a DELETE. A schema
    born post-012 names this constraint differently from one the migration
    added, so the assertion is on the RULE and the referenced table, never on
    the constraint name."""
    with db._conn() as conn:
        row = conn.execute("""
            SELECT c.conname, c.confdeltype, t.relname
              FROM pg_constraint c
              JOIN unnest(c.conkey) k(attnum) ON TRUE
              JOIN pg_attribute a
                ON a.attrelid = c.conrelid AND a.attnum = k.attnum
              JOIN pg_class t ON t.oid = c.confrelid
             WHERE c.conrelid = 'mae_mfe_data'::regclass
               AND c.contype = 'f' AND a.attname = 'trade_id'
             LIMIT 1""").fetchone()
    assert row is not None, (
        "no FK on mae_mfe_data.trade_id - migrations/012 has not been applied "
        "to this database, or storage/database.py's DDL lost it")
    assert row[2] == "positions"
    assert row[1] == "n", (
        f"delete rule is {row[1]!r}, not 'n' (SET NULL). 'c' is CASCADE, which "
        f"would make reset_paper_account() destroy excursion history.")


def test_trade_id_is_an_integer_column(db):
    """The FK cannot exist against a TEXT column, so this is really a second
    reading of the same fact - but it fails with a clearer message when a
    database is still on the pre-012 shape."""
    with db._conn() as conn:
        t = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'mae_mfe_data' AND column_name = 'trade_id'"
        ).fetchone()[0]
    assert t == "integer", f"trade_id is {t!r} - pre-012 shape"


# ── the destructive operations ──────────────────────────────────────────────

def test_deleting_a_position_nulls_the_link_and_keeps_the_row(db):
    """The core guarantee. Not 'the row survives' alone - a CASCADE would
    delete it, but so would nothing at all if the FK were missing and the row
    had never been written. Both halves are asserted: the excursion survives,
    AND its link is gone rather than left dangling."""
    pid = _position(db)
    _excursion(db, pid)
    assert _rows(db) == [(pid, "AAPL", -4.2)]

    with db._conn() as conn:
        conn.execute("DELETE FROM positions WHERE id = ?", (pid,))

    rows = _rows(db)
    assert len(rows) == 1, "the excursion row was deleted - this is CASCADE"
    assert rows[0][0] is None, (
        f"trade_id is {rows[0][0]!r}, not NULL - the row now names a position "
        f"that does not exist, which is the orphan §49 had to purge")
    assert rows[0][2] == -4.2, "the excursion measurement itself was altered"


def test_reset_paper_account_preserves_excursions(db):
    """The operation the policy was chosen for. reset_paper_account() deletes
    every simulated position as a matter of design; its docstring promises
    mae_mfe_data is untouched. Under CASCADE that promise would be false, and
    the failure would surface much later as a thin MAE average."""
    pid = _position(db, ticker="MSFT")
    _excursion(db, pid, ticker="MSFT", mae=-3.3)

    db.reset_paper_account()

    rows = _rows(db, ticker="MSFT")
    assert len(rows) == 1, (
        "reset_paper_account() destroyed the excursion history its own "
        "docstring promises to keep")
    assert rows[0][0] is None
    assert rows[0][2] == -3.3


def test_the_real_book_is_unaffected_by_a_paper_reset(db):
    """reset_paper_account() is scoped to simulated positions. A real
    position's excursion link must still be intact afterwards - otherwise the
    SET NULL policy would be hiding a scoping bug rather than the reset being
    correctly narrow."""
    real = _position(db, ticker="NVDA", simulated=False)
    _excursion(db, real, ticker="NVDA", mae=-1.1)

    db.reset_paper_account()

    rows = _rows(db, ticker="NVDA")
    assert rows == [(real, "NVDA", -1.1)], (
        "a real position's excursion link was cleared by a PAPER reset")


# ── what the orphan means downstream ────────────────────────────────────────

def test_an_orphaned_excursion_is_excluded_from_the_pattern_join(db):
    """§51's join filters `p.trade_id IS NOT NULL`, so a post-reset orphan is
    ABSENT from excursion analysis rather than counted as zero excursion. That
    is the behaviour that makes SET NULL safe: a NULL link removes the row from
    the average instead of dragging it toward zero."""
    pid = _position(db, ticker="TSLA")
    pat = db.add_pattern("TSLA", "SWING", {"rsi14": 55.0}, trade_id=str(pid))
    db.close_pattern(pat, outcome_pct=2.0, hold_hours=4.0,
                     exit_reason="paper_price_watch:take_profit")
    _excursion(db, pid, ticker="TSLA", mae=-2.5)

    assert len(db.get_pattern_excursions()) == 1

    with db._conn() as conn:
        conn.execute("DELETE FROM positions WHERE id = ?", (pid,))

    assert db.get_pattern_excursions() == [], (
        "an orphaned excursion row is still being joined - it would contribute "
        "to MAE averages while naming no position")

    # ...and it is still readable by the accessor that does not need the link.
    assert len(_rows(db, ticker="TSLA")) == 1


def test_an_orphan_cannot_be_written_in_the_first_place(db):
    """The complement. SET NULL handles a link that BECOMES invalid; the FK is
    what stops one being created invalid. Without this, the previous tests
    would pass on a database where any integer is writable."""
    import psycopg2
    with pytest.raises(psycopg2.Error):
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO mae_mfe_data (trade_id, ticker, recorded_at, "
                "data_quality) VALUES (?, ?, ?, ?)",
                (987654321, "GHOST", "2026-07-26T00:00:00", "ok"))


def test_insert_mae_mfe_rejects_a_non_numeric_trade_id(db):
    """Caught at the call site that knows what it meant, rather than as a
    constraint violation three layers down."""
    with pytest.raises(ValueError, match="not a position id"):
        db.insert_mae_mfe({"trade_id": "not-a-number", "ticker": "X"})
