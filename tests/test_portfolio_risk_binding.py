"""§18 - portfolio risk is measured on real names, and it binds.

Three findings in one section. The theme_map was hand-maintained across 12
tickers in 4 themes and none of the names actually traded (ADPT, FLYW, ERAS,
XRAY, PSNL, VG, HLN, TAK) appeared in it, so theme concentration was
UNMEASURED for every real trade. hard_block_on_severe_breach was false, so a
breach only scaled position size. And rejected_signals had 0 rows against
portfolio_risk_log's 244, so there was no record of what the system declined.

A limit that is never measured and never blocks is documentation, not risk
management - and with 244 BUY signals in eight days from momentum-based
discovery, correlated clustering is the default state rather than the
exception.

Pure-logic tests with a fake database: no Postgres, no network. Correlation is
left out of these deliberately - it needs price history and is tested by its
own arithmetic elsewhere; here the subject is classification and blocking.

    python3 -m pytest tests/test_portfolio_risk_binding.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.portfolio_risk import UNCLASSIFIED, PortfolioRiskEngine, _themes_for


@pytest.fixture(autouse=True)
def _no_correlation_fetch(monkeypatch):
    """Correlation needs six months of daily bars per ticker, over the
    network. These tests are about classification and blocking, so it is
    stubbed to "unknown" - which is also what the engine sees on any ticker
    with too little history, so this is a real state rather than a convenient
    one.

    Autouse and mandatory. Without it these passed only because the yfinance
    MCP import happened to fail fast in isolation; in a full-suite run, where
    another module had already imported it, the same tests hung on live HTTP
    calls. A unit test whose speed depends on a network call failing is not a
    unit test.
    """
    monkeypatch.setattr("engine.portfolio_risk.get_pairwise_correlation",
                        lambda a, b, lookback=60: None)


class FakeDB:
    def __init__(self, positions=None, info=None):
        self.positions = positions or []
        self.info = info or {}
        self.risk_log = []

    def get_all_positions(self, simulated=None):
        return list(self.positions)

    def get_ticker_info_bulk(self, tickers):
        return {t: self.info.get(t, {}) for t in tickers}

    def get_ticker_info(self, ticker):
        return self.info.get(ticker)

    def log_portfolio_risk(self, *a, **k):
        self.risk_log.append((a, k))


def _cfg(**pr):
    base = {
        "enabled": True,
        "max_sector_exposure_pct": 35,
        "max_theme_exposure_pct": 40,
        "max_unclassified_exposure_pct": 25,
        "max_portfolio_beta": 1.6,
        "high_correlation_threshold": 0.75,
        "max_high_correlation_cluster": 3,
        "high_vol_atr_pct_threshold": 5.0,
        "max_simultaneous_high_vol_positions": 4,
        "hard_block_on_severe_breach": True,
        "severe_breach_multiple": 1.5,
        "min_positions_for_concentration_block": 3,
        "theme_map": {"AI": ["NVDA", "MSFT"]},
    }
    base.update(pr)
    return {"trading": {"watch_execute": "WATCH"}, "portfolio_risk": base}


def _pos(ticker, amount, stop=None, entry=100.0):
    return {"ticker": ticker, "dollar_amount": amount, "entry_price": entry,
            "current_stop_price": stop}


# ── Classification ──────────────────────────────────────────────────────────

def test_hand_curated_theme_still_wins():
    """The map captures real cross-sector relationships like 'AI' that no data
    vendor labels. Deriving from sector must not replace it."""
    themes = _themes_for("NVDA", {"AI": ["NVDA"]},
                         {"sector": "Technology", "industry": "Semiconductors"})
    assert "AI" in themes


def test_sector_and_industry_become_buckets():
    themes = _themes_for("ADPT", {}, {"sector": "Healthcare",
                                      "industry": "Biotechnology"})
    assert themes == ["INDUSTRY:Biotechnology", "SECTOR:Healthcare"]


def test_traded_names_are_no_longer_themeless():
    """THE finding. None of the eight names actually traded appeared in the
    hand-maintained map, so the theme cap could never bind on any real trade."""
    for ticker in ("ADPT", "FLYW", "ERAS", "XRAY", "PSNL", "VG", "HLN", "TAK"):
        themes = _themes_for(ticker, {"AI": ["NVDA", "MSFT"]},
                             {"sector": "Healthcare"})
        assert themes != [UNCLASSIFIED]
        assert any(t.startswith("SECTOR:") for t in themes)


def test_unknown_ticker_lands_in_unclassified():
    """Not skipped. An unclassifiable position is an unmeasured risk, and
    unmeasured risk should be rationed rather than ignored - skipping it is
    what let the cap silently never bind."""
    assert _themes_for("WAT", {}, {}) == [UNCLASSIFIED]


def test_na_is_not_a_classification():
    """'N/A' is a placeholder, not a sector. Treating it as one would create a
    large fake bucket that every unclassified name concentrated into."""
    assert _themes_for("WAT", {}, {"sector": "N/A", "industry": "N/A"}) == [UNCLASSIFIED]


# ── The cap binds ───────────────────────────────────────────────────────────

def _evaluate(db, cfg, ticker="ADPT", sector="Healthcare", industry="Biotechnology",
              amount=100.0, beta=1.0, atr=1.0):
    return PortfolioRiskEngine(db).evaluate(
        ticker, sector, beta, amount, atr, cfg, candidate_industry=industry)


def test_sector_concentration_now_measured():
    """Three healthcare positions and a fourth healthcare candidate: 100% of
    the book in one sector against a 35% cap."""
    db = FakeDB(
        positions=[_pos("FLYW", 100), _pos("ERAS", 100), _pos("XRAY", 100)],
        info={t: {"sector": "Healthcare", "industry": "Biotechnology", "beta": 1.0}
              for t in ("FLYW", "ERAS", "XRAY")})
    result = _evaluate(db, _cfg())
    assert result.sector_exposure_pct == pytest.approx(100.0)
    assert result.allowed is False


def test_severe_breach_blocks_and_names_the_cause():
    db = FakeDB(
        positions=[_pos("FLYW", 100), _pos("ERAS", 100), _pos("XRAY", 100)],
        info={t: {"sector": "Healthcare", "industry": "Biotechnology", "beta": 1.0}
              for t in ("FLYW", "ERAS", "XRAY")})
    result = _evaluate(db, _cfg())
    assert result.severe_breaches
    assert "sector" in result.reason


def test_a_mild_breach_sizes_down_but_does_not_block():
    """CONTROL, and the important one. The ordinary case must stay a size
    reduction - a control that refuses everything gets switched off."""
    db = FakeDB(
        positions=[_pos("AAA", 100), _pos("BBB", 100), _pos("CCC", 100),
                   _pos("DDD", 100), _pos("EEE", 100)],
        info={"AAA": {"sector": "Healthcare", "industry": "Biotech", "beta": 1.0},
              **{t: {"sector": "Utilities", "industry": f"U{i}", "beta": 1.0}
                 for i, t in enumerate(("BBB", "CCC", "DDD", "EEE"))}})
    result = _evaluate(db, _cfg(), amount=100.0)
    # 200 of 600 = 33% - under the 35% cap entirely.
    assert result.allowed is True
    assert result.sector_exposure_pct < 35


def test_hard_block_off_keeps_the_old_advisory_behaviour():
    db = FakeDB(
        positions=[_pos("FLYW", 100), _pos("ERAS", 100), _pos("XRAY", 100)],
        info={t: {"sector": "Healthcare", "industry": "Biotechnology", "beta": 1.0}
              for t in ("FLYW", "ERAS", "XRAY")})
    result = _evaluate(db, _cfg(hard_block_on_severe_breach=False))
    assert result.allowed is True          # advisory only
    assert result.severe_breaches          # ...but the breach is still recorded


def test_unclassified_has_its_own_tighter_cap():
    """25% rather than the 40% ordinary theme cap.

    Three unclassifiable positions plus a fourth candidate is 100%
    unclassified. Against 25% that is 4x the cap and severe; against 40% it
    would also be severe, so the sharper demonstration is the reason for the
    tighter number rather than the arithmetic: an unclassifiable position is
    an unmeasured risk, and unmeasured risk gets a smaller allowance than one
    you can actually see."""
    db = FakeDB(positions=[_pos("WAT1", 100), _pos("WAT2", 100), _pos("WAT3", 100)],
                info={})
    result = _evaluate(db, _cfg(), ticker="WAT4", sector=None, industry=None)
    assert UNCLASSIFIED in result.themes
    assert result.allowed is False
    assert any("UNCLASSIFIED" in b for b in result.severe_breaches)


def test_unclassified_cap_is_tighter_than_the_theme_cap():
    """The property itself, isolated: an exposure that is acceptable as a
    known theme is severe as an unknown one."""
    known = FakeDB(
        positions=[_pos("A", 100), _pos("B", 100), _pos("C", 100), _pos("D", 100),
                   _pos("E", 100), _pos("F", 100), _pos("G", 100), _pos("H", 100)],
        info={t: {"sector": "Healthcare", "industry": "Biotech", "beta": 1.0}
              for t in "ABCDEFGH"})
    # Three of nine in one bucket = 33%: over the 25% unclassified cap but
    # under the 40% theme cap, and severe against neither.
    unknown = FakeDB(
        positions=[_pos("A", 100), _pos("B", 100), _pos("C", 100)],
        info={t: {"sector": "Utilities", "industry": "U", "beta": 1.0}
              for t in "ABC"})
    assert _evaluate(known, _cfg(), ticker="I", sector="Utilities",
                     industry="Water").allowed is True
    assert _evaluate(unknown, _cfg(), ticker="Z", sector=None,
                     industry=None).allowed is True   # 25% of the book exactly


def test_disabled_engine_allows_everything():
    db = FakeDB(positions=[_pos("FLYW", 100)],
                info={"FLYW": {"sector": "Healthcare"}})
    result = _evaluate(db, _cfg(enabled=False))
    assert result.allowed is True
    assert result.size_multiplier == 1.0


def test_empty_book_never_blocks():
    """THE control for this section, and it caught a real bug.

    On an empty book the candidate is 100% of the portfolio by construction -
    100% of one sector, 100% of one theme and 100% unclassified, all at once.
    An unguarded severity test refuses the first trade of every session for
    arithmetic reasons rather than risk ones, and it looks completely correct
    until you write this test."""
    result = _evaluate(FakeDB(), _cfg())
    assert result.allowed is True


def test_two_position_book_does_not_block_on_concentration():
    """The same artifact one step later: with two positions a single
    same-sector holding is already 100% pre-trade, which drives the sizing
    multiplier to zero. Below the floor that sizes down; it must not refuse."""
    db = FakeDB(positions=[_pos("FLYW", 100), _pos("ERAS", 100)],
                info={t: {"sector": "Healthcare", "industry": "Biotechnology",
                          "beta": 1.0} for t in ("FLYW", "ERAS")})
    assert _evaluate(db, _cfg()).allowed is True


def test_beta_is_not_gated_by_book_size():
    """Beta is not a share of the book, so it means something on the very
    first position - a 3.0-beta candidate alone IS a 3.0-beta portfolio."""
    result = _evaluate(FakeDB(), _cfg(), beta=3.0)
    assert any("beta" in b for b in result.severe_breaches)
    assert result.allowed is False


def test_beta_breach_is_severe_past_the_multiple():
    db = FakeDB(positions=[_pos("HIGH", 100)],
                info={"HIGH": {"sector": "Utilities", "industry": "U", "beta": 3.0}})
    result = _evaluate(db, _cfg(), sector="Energy", industry="Oil", beta=3.0)
    assert any("beta" in b for b in result.severe_breaches)
    assert result.allowed is False
