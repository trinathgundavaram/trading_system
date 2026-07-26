"""Is a drawdown real, or is it an accounting event? (§11 / §48)

This decides whether a human clears a kill switch. Both errors are expensive
and neither is loud: call a real 16% drawdown an artifact and you clear a halt
that was protecting you; call an artifact real and you go looking for a losing
streak that never happened while the same trap stays armed.

The first version of the detector flagged any large move in total_value not
matched by realized P&L - and immediately called an ordinary market decline
"unexplained", because unrealized losses do not touch realized_pnl. These tests
exist because that was wrong in a way that looked right.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "diagnose_drawdown", str(REPO / "scripts" / "diagnose_drawdown.py"))
    spec = importlib.util.spec_from_loader("diagnose_drawdown", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


dd = _load()


def curve(rows):
    """(cash, market_value) pairs -> the sample dicts the script reads."""
    return [{"timestamp": f"2026-07-{i + 1:02d}T00:00:00",
             "total_value": c + m, "cash": c, "market_value": m,
             "realized_pnl": 0, "n_open": 2}
            for i, (c, m) in enumerate(rows)]


def unmatched_steps(samples):
    """The detector, lifted out of main() so it can be tested directly."""
    out = []
    for a, b in zip(samples, samples[1:]):
        va = float(a["total_value"])
        d_cash = float(b["cash"]) - float(a["cash"])
        d_mkt = float(b["market_value"]) - float(a["market_value"])
        unmatched = d_cash + d_mkt
        if (abs(d_cash) >= dd.STEP_PCT / 100 * va
                and abs(unmatched) >= dd.UNMATCHED_SHARE * abs(d_cash)):
            out.append((b["timestamp"], unmatched))
    return out


class TestTheAccountingIdentity:
    """total_value = cash + market_value, so each event has a signature."""

    def test_market_movement_is_not_flagged(self):
        """Cash is flat; only market_value moves. This is the entire purpose
        of holding a position and must never look like a balance change."""
        assert unmatched_steps(curve([(400, 600), (400, 500), (400, 380)])) == []

    def test_a_buy_is_not_flagged(self):
        """cash -X, market +X. The legs cancel, however large X is."""
        assert unmatched_steps(curve([(1000, 0), (100, 900)])) == []

    def test_a_sell_is_not_flagged(self):
        assert unmatched_steps(curve([(100, 900), (1000, 0)])) == []

    def test_a_reseed_is_flagged(self):
        """Cash appears with nothing offsetting it. This is the only event
        that produces an unmatched sum, which is why the test is on the
        identity rather than on a threshold."""
        # cash +711, market_value -204 -> 507 arrives from nowhere.
        steps = unmatched_steps(curve([(400, 584), (1111, 380)]))
        assert len(steps) == 1
        assert steps[0][1] == 507.0

    def test_a_small_cash_move_is_below_the_floor(self):
        """Rounding, fees and dust must not read as a balance change."""
        assert unmatched_steps(curve([(1000, 500), (1004, 500)])) == []


class TestRebasing:
    """Measuring 'from the last jump' handles a permanent re-seed and misses
    the worse case entirely: a transient spike, where one bad balance sample
    becomes the all-time peak and every day after is measured against a number
    the account held for one sample."""

    def _rebased_dd(self, samples):
        steps = dict(unmatched_steps(samples))
        adjusted, shift = [], 0.0
        for r in samples:
            shift += steps.get(r["timestamp"], 0.0)
            adjusted.append(float(r["total_value"]) - shift)
        peak = max(adjusted)
        return (peak - adjusted[-1]) / peak * 100

    def _raw_dd(self, samples):
        vals = [float(r["total_value"]) for r in samples]
        peak = max(vals)
        return (peak - vals[-1]) / peak * 100

    def test_a_transient_spike_collapses_once_rebased(self):
        rows = [(400, 584)] * 8 + [(1111, 380)] + [(400, 584 - i * 2) for i in range(10)]
        samples = curve(rows)
        raw = self._raw_dd(samples)
        rebased = self._rebased_dd(samples)
        assert raw > 30, "the spike should dominate the unrebased figure"
        assert rebased < 5, "and vanish once the series is made continuous"

    def test_genuine_losses_survive_rebasing(self):
        """No balance change anywhere, so rebasing is a no-op and the drawdown
        must come through untouched. If this ever softens, the script has
        become a way to explain away real losses."""
        samples = curve([(400, 600 - i * 22) for i in range(19)])
        assert unmatched_steps(samples) == []
        assert self._rebased_dd(samples) == self._raw_dd(samples)
        assert self._rebased_dd(samples) > 30

    def test_a_permanent_reseed_leaves_the_later_decline_visible(self):
        """Rebasing removes the injected cash, not the losses that followed
        it - a real decline after a re-seed is still a real decline."""
        rows = [(400, 584)] * 8 + [(1111, 380)] + [(1111, 359 - i * 11) for i in range(10)]
        rebased = self._rebased_dd(curve(rows))
        assert rebased > 5


class TestItWritesNothing:
    def test_the_script_is_read_only(self):
        """It runs against the live database while a halt is in force. It must
        not be able to change anything, including the switch it is reporting
        on."""
        import re

        src = (REPO / "scripts" / "diagnose_drawdown.py").read_text()
        # SQL write statements, not bare words - `sys.path.insert` is not a
        # database write, and a test that cannot tell the difference is a test
        # that gets deleted the first time it cries wolf.
        writes = re.compile(
            r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE"
            r"|DROP\s+(TABLE|INDEX)|TRUNCATE)\b", re.IGNORECASE)
        found = writes.findall(src)
        assert not found, f"diagnose_drawdown.py can write to the database: {found}"
        for forbidden in ("write_text(", "trip_kill_switch", "set_kill_switch"):
            assert forbidden not in src, f"contains {forbidden!r}"
