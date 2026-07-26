"""Phase 4 (§19-§21) - the harness, and mostly its refusals.

The arithmetic here is the easy part. What earns this file its place is that
the script must decline to produce a number it cannot support: a weight fitted
to a constant placeholder feature, a tier derived from six excursion rows, or
a passing validation receipt nobody signed. Each of those would look like
progress and be worse than an empty output.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "phase4_recalibrate", str(REPO / "scripts" / "phase4_recalibrate.py"))
    spec = importlib.util.spec_from_loader("phase4_recalibrate", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


p4 = _load()


class TestStatistics:
    def test_rank_correlation_is_monotone_not_linear(self):
        """Rank, not Pearson, on purpose: several features are bounded or
        heavily skewed, and what a scoring weight needs is monotone
        discriminative power."""
        xs = [1, 2, 3, 4, 5]
        assert p4.spearman(xs, [1, 4, 9, 16, 25]) == pytest.approx(1.0)
        assert p4.spearman(xs, [25, 16, 9, 4, 1]) == pytest.approx(-1.0)

    def test_a_constant_feature_has_no_correlation(self):
        """This is the placeholder case. ADX, CMF, market breadth and the rest
        are 0.0 defaults throughout the codebase; a weight derived for them
        would be a weight derived from nothing."""
        assert p4.spearman([0.0] * 20, list(range(20))) is None

    def test_ties_do_not_break_the_correlation(self):
        rho = p4.spearman([1, 1, 2, 2, 3], [1, 2, 2, 3, 4])
        assert rho is not None and 0 < rho < 1

    def test_percentile_interpolates(self):
        assert p4.percentile([1, 2, 3, 4, 5], 50) == 3
        assert p4.percentile([1, 2, 3, 4, 5], 90) == pytest.approx(4.6)
        assert p4.percentile([7], 50) == 7

    def test_percentile_of_nothing_raises(self):
        """Rather than returning 0.0, which would silently become a threshold."""
        with pytest.raises(ValueError):
            p4.percentile([], 50)


class TestSampleFloors:
    def test_the_floor_matches_the_bayesian_gate(self):
        """One answer to 'how much history is enough', not two that drift.
        learning.min_trades_before_bayesian is 150 in config.yaml."""
        import yaml

        cfg = yaml.safe_load((REPO / "config.yaml").read_text())
        assert p4.MIN_CLOSED_PATTERNS == cfg["learning"]["min_trades_before_bayesian"]

    def test_span_needs_more_than_one_regime(self):
        assert p4.MIN_SPAN_DAYS >= 90

    def test_span_days_of_a_thin_sample_is_zero_not_an_error(self):
        assert p4.span_days([]) == 0
        assert p4.span_days([{"recorded_at": "2026-01-01T00:00:00"}]) == 0
        assert p4.span_days([{"recorded_at": "2026-01-01T00:00:00"},
                             {"recorded_at": "2026-04-01T00:00:00"}]) == 90


class TestReceipt:
    """live_trader.py has looked for this file since Phase 1 and never found
    one, so `validation receipt gate blocks arming` has been passing for the
    least interesting possible reason."""

    def _args(self, tmp_path, **kw):
        results = tmp_path / "results.json"
        results.write_text(json.dumps({
            "summary": {"n_trades": kw.pop("n_trades", 50),
                        "win_rate": 55.0, "profit_factor": 1.6}}))
        ns = type("NS", (), {})()
        ns.backtest_results = str(results)
        ns.comparison = kw.pop("comparison", None)
        ns.signed_off_by = kw.pop("signed_off_by", "trinath")
        ns.min_trades = kw.pop("min_trades", 30)
        return ns

    def test_a_signed_receipt_with_enough_trades_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(p4, "REPO", REPO)
        import storage.paths as paths

        monkeypatch.setattr(paths, "validation_receipt_path",
                            lambda: tmp_path / "receipt.json")
        assert p4.cmd_receipt(self._args(tmp_path)) == 0
        r = json.loads((tmp_path / "receipt.json").read_text())
        assert r["passed"] is True and r["signed_off_by"] == "trinath"

    def test_an_unsigned_receipt_fails(self, tmp_path, monkeypatch):
        """A receipt records that a person looked at the numbers. An
        unattributed one records nothing."""
        import storage.paths as paths

        monkeypatch.setattr(paths, "validation_receipt_path",
                            lambda: tmp_path / "receipt.json")
        assert p4.cmd_receipt(self._args(tmp_path, signed_off_by=None)) == 1
        r = json.loads((tmp_path / "receipt.json").read_text())
        assert r["passed"] is False and "signed-off-by" in r["reason"]

    def test_a_thin_backtest_fails(self, tmp_path, monkeypatch):
        import storage.paths as paths

        monkeypatch.setattr(paths, "validation_receipt_path",
                            lambda: tmp_path / "receipt.json")
        assert p4.cmd_receipt(self._args(tmp_path, n_trades=3)) == 1

    def test_a_failing_receipt_is_still_written(self, tmp_path, monkeypatch):
        """Deliberately, not skipped: live_trader.py distinguishes 'last
        validation FAILED' from 'no receipt', and the first is more useful to
        read than the absence of a file."""
        import storage.paths as paths

        monkeypatch.setattr(paths, "validation_receipt_path",
                            lambda: tmp_path / "receipt.json")
        p4.cmd_receipt(self._args(tmp_path, n_trades=0, signed_off_by=None))
        assert (tmp_path / "receipt.json").exists()

    def test_a_named_comparison_that_does_not_exist_fails(self, tmp_path, monkeypatch):
        import storage.paths as paths

        monkeypatch.setattr(paths, "validation_receipt_path",
                            lambda: tmp_path / "receipt.json")
        args = self._args(tmp_path, comparison=str(tmp_path / "nope.txt"))
        assert p4.cmd_receipt(args) == 1


class TestItAppliesNothing:
    def test_the_script_never_writes_config_yaml(self):
        """A recalibration that silently rewrote the running configuration
        would change the decision function with no release, no declared
        fingerprint change, and no §35 boundary - which is the one thing the
        whole versioning scheme exists to keep visible."""
        src = (REPO / "scripts" / "phase4_recalibrate.py").read_text()
        for forbidden in ("config.yaml\").write_text", "yaml.dump", "yaml.safe_dump"):
            assert forbidden not in src, f"phase4 script contains {forbidden!r}"

    def test_the_proposal_records_that_it_was_not_applied(self):
        src = (REPO / "scripts" / "phase4_recalibrate.py").read_text()
        assert '"APPLIED": False' in src
