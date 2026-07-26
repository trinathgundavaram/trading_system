"""§49's purge predicates - what gets deleted, and what must not.

These decide whether measurement data survives a cleanup, and a false positive
is permanent. The predicates used to conflate two situations under one name,
`ticker_collision`:

    trade_id resolves to a position of a DIFFERENT ticker   -> corrupt
    trade_id resolves to NO position at all                 -> orphaned

and deleted both. Positions are deleted routinely and by design -
reset_paper_account() removes every simulated one - so the second case is the
NORMAL fate of a real excursion row, not evidence of anything. On the release
machine that predicate matched all 30 rows, of which 7 carried real excursion
values that §21's sizing tiers are derived from.

migrations/012 had already decided what an orphan means, and chose ON DELETE
SET NULL over CASCADE for exactly this reason: "The maximum adverse excursion
of a trade that happened is a fact about that trade, and it stays true after
the position row is gone."
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "repair_test_damage", str(REPO / "scripts" / "repair_test_damage.py"))
    spec = importlib.util.spec_from_loader("repair_test_damage", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rtd = _load()


def _names(pairs):
    return [p[0] for p in pairs]


class TestTheSplit:
    def test_orphans_are_not_in_the_delete_list(self):
        """The regression itself. If `orphan_trade_id` ever appears among the
        delete predicates again, real excursions are being destroyed."""
        assert "orphan_trade_id" not in _names(rtd.PURGE_MAE_PREDICATES)
        assert "orphan_trade_id" in _names(rtd.NULL_MAE_PREDICATES)

    def test_the_conflated_predicate_is_gone(self):
        assert "ticker_collision" not in _names(rtd.PURGE_MAE_PREDICATES)
        assert "ticker_mismatch" in _names(rtd.PURGE_MAE_PREDICATES)

    def test_mismatch_requires_the_position_to_exist(self):
        """A mismatch is 'points at the WRONG trade', which cannot be true of a
        trade_id that points at nothing. Without the EXISTS clause the two
        predicates overlap and the delete wins."""
        sql = dict((n, p) for n, p, _ in rtd.PURGE_MAE_PREDICATES)["ticker_mismatch"]
        normalised = re.sub(r"\s+", " ", sql)
        assert "AND EXISTS (" in normalised
        assert "AND NOT EXISTS (" in normalised

    def test_orphan_requires_the_position_to_be_absent(self):
        sql = dict((n, p) for n, p, _ in rtd.NULL_MAE_PREDICATES)["orphan_trade_id"]
        normalised = re.sub(r"\s+", " ", sql)
        assert "NOT EXISTS (" in normalised
        assert "UPPER(p.ticker)" not in normalised, \
            "an orphan is defined by the position being gone, not by its ticker"

    def test_the_two_predicates_are_disjoint(self):
        """One says the position exists, the other says it does not. If they
        ever overlap, a row's fate depends on statement order."""
        mismatch = dict((n, p) for n, p, _ in rtd.PURGE_MAE_PREDICATES)["ticker_mismatch"]
        orphan = dict((n, p) for n, p, _ in rtd.NULL_MAE_PREDICATES)["orphan_trade_id"]
        assert "AND EXISTS (" in re.sub(r"\s+", " ", mismatch)
        assert "AND EXISTS (" not in re.sub(r"\s+", " ", orphan)


class TestWhatIsStillDeleted:
    """Narrowing the collision predicate must not soften the rest."""

    def test_synthetic_tickers_still_go(self):
        assert "ticker_synthetic" in _names(rtd.PURGE_MAE_PREDICATES)

    def test_flat_excursions_still_go(self):
        """mae = mfe = 0.0 exactly means update_live() never ran, so these are
        fixtures however their trade_id resolves."""
        assert "flat_excursion" in _names(rtd.PURGE_MAE_PREDICATES)

    def test_only_obviously_synthetic_tickers_are_named(self):
        """ORCL, NVDA, MU and BMY are real companies. Deleting on the wider
        TEST_TICKERS list would destroy real excursions that merely share a
        name with a fixture."""
        for real in ("ORCL", "NVDA", "MU", "BMY"):
            assert real not in rtd.OBVIOUSLY_SYNTHETIC


class TestApplyOrder:
    def test_deletes_run_before_unlinks(self):
        """A row that is BOTH junk and orphaned must be deleted, not kept with
        a NULL link. That follows from order, so the order is asserted rather
        than left to whichever loop someone writes first."""
        src = (REPO / "scripts" / "repair_test_damage.py").read_text()
        delete_at = src.index("DELETE FROM mae_mfe_data WHERE {predicate}")
        unlink_at = src.index("UPDATE mae_mfe_data SET trade_id = NULL")
        assert delete_at < unlink_at
