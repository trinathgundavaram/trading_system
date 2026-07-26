"""§40 - the two-version comparison, which is two questions and not one.

The verdict logic is the whole value of this script. Getting it backwards in
either direction is expensive and quiet: a reproducibility defect filed as
"expected, we changed the scoring", or an inert recalibration declared
validated because nothing broke.
"""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "compare_versions", str(REPO / "scripts" / "compare_versions.py"))
    spec = importlib.util.spec_from_loader("compare_versions", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cv = _load()

BASE = {
    "n_scored": 5,
    "veto_counts": {"stale_indicator": 2},
    "summary": {"n_trades": 3, "win_rate": 66.7, "avg_outcome_pct": 1.4,
                "avg_win_pct": 3.1, "avg_loss_pct": -1.9,
                "profit_factor": 2.1, "avg_hold_days": 4.2},
    "trades": [
        {"ticker": "AAPL", "entry_date": "2025-02-03", "entry_price": 10.0,
         "exit_price": 11.0, "outcome_pct": 10.0, "exit_reason": "target",
         "hold_days": 4},
        {"ticker": "MSFT", "entry_date": "2025-03-10", "entry_price": 20.0,
         "exit_price": 19.0, "outcome_pct": -5.0, "exit_reason": "stop",
         "hold_days": 3},
    ],
    "config": {"config_fingerprint": "cc9a149613427f56"},
}


def _variant(**kw):
    r = copy.deepcopy(BASE)
    if "fingerprint" in kw:
        r["config"]["config_fingerprint"] = kw["fingerprint"]
    if kw.get("drift"):
        r["summary"]["avg_outcome_pct"] = 1.41
    if kw.get("extra_trade"):
        r["trades"].append({"ticker": "AMD", "entry_date": "2025-05-02",
                            "entry_price": 5.0, "exit_price": 6.0,
                            "outcome_pct": 20.0, "exit_reason": "target",
                            "hold_days": 2})
        r["summary"]["n_trades"] = 3
    return r


class TestVerdict:
    def test_same_fingerprint_identical_numbers_passes(self):
        """The Phase 3 exit criterion: same code paths, same numbers."""
        assert cv.render("A", "B", BASE, copy.deepcopy(BASE)) is True

    def test_same_fingerprint_different_numbers_is_a_failure(self):
        """The fingerprints say this was meant to be the same computation. It
        was not, so something outside the decision function moved - which is
        exactly what §13's pinning exists to prevent."""
        assert cv.render("A", "B", BASE, _variant(drift=True)) is False

    def test_different_fingerprint_different_numbers_passes(self):
        """The decision function moved and the numbers moved with it. That is
        the Phase 4 measurement, not a fault."""
        assert cv.render("A", "B", BASE,
                         _variant(fingerprint="dead", extra_trade=True)) is True

    def test_different_fingerprint_identical_numbers_is_a_failure(self):
        """A recalibration that changes nothing measurable has not been shown
        to do anything. Passing this would let an inert change be reported as
        validated."""
        assert cv.render("A", "B", BASE, _variant(fingerprint="dead")) is False

    def test_a_missing_fingerprint_is_treated_as_a_change(self):
        """Conservative direction on purpose: it forgoes a reproducibility
        failure it might have caught, rather than inventing one it did not."""
        no_fp = copy.deepcopy(BASE)
        no_fp["config"] = {}
        # Identical numbers, one fingerprint unknown -> "no measurable effect",
        # not "identical", because we cannot claim the computation matched.
        assert cv.render("A", "B", BASE, no_fp) is False


class TestTradeComparison:
    def test_summary_can_agree_while_the_trades_do_not(self):
        """The reason this script compares trades at all. Two runs with the
        same trade count, win rate and profit factor need not have taken the
        same trades."""
        swapped = copy.deepcopy(BASE)
        swapped["trades"][0]["ticker"] = "TSLA"
        td = cv.compare_trades(BASE["trades"], swapped["trades"])
        assert len(td["only_a"]) == 1 and len(td["only_b"]) == 1

    def test_same_trade_different_fill_is_reported(self):
        moved = copy.deepcopy(BASE)
        moved["trades"][0]["exit_price"] = 11.01
        td = cv.compare_trades(BASE["trades"], moved["trades"])
        assert not td["only_a"] and not td["only_b"]
        assert len(td["shared_differing"]) == 1
        key, diffs = td["shared_differing"][0]
        assert key[0] == "AAPL" and "exit_price" in diffs

    def test_trades_are_keyed_on_ticker_and_entry_date(self):
        """Not on list position: a version that takes the same trades in a
        different order has not changed anything, and must not be reported as
        though it had."""
        reordered = copy.deepcopy(BASE)
        reordered["trades"].reverse()
        td = cv.compare_trades(BASE["trades"], reordered["trades"])
        assert not (td["only_a"] or td["only_b"] or td["shared_differing"])


class TestCli:
    def test_refuses_to_compare_a_version_with_itself(self, monkeypatch):
        """It would always pass, and would prove only determinism."""
        monkeypatch.setattr("sys.argv", ["compare_versions.py", "v2.1.0", "v2.1.0"])
        with pytest.raises(SystemExit):
            cv.main()

    def test_reads_two_results_files(self, tmp_path, monkeypatch):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps(BASE))
        b.write_text(json.dumps(BASE))
        monkeypatch.setattr("sys.argv", ["compare_versions.py",
                                         "--results-a", str(a),
                                         "--results-b", str(b)])
        assert cv.main() == 0
