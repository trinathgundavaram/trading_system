"""A non-finite float in a response payload must not become an HTTP 500.

Starlette's JSONResponse renders with ``json.dumps(..., allow_nan=False)``.
That strictness is right - ``Infinity``/``NaN`` are not valid JSON and
``JSON.parse`` rejects them - but it fails at RENDER time, after the handler
has already returned successfully. So a route that computed a correct answer
answers 500, and the traceback points at json.dumps rather than at whatever
produced the value.

That is not hypothetical. ``/api/analytics/performance`` did exactly this for
any book with no losing trade: ``profit_factor()`` returned ``float("inf")``,
the response never serialised, and the Performance tab - which had no error
handling at all - sat on "Loading..." forever showing nothing but a console
error. Two things are covered here, because either alone leaves the hole open:
the sentinel is gone at its source, AND the response layer can no longer be
taken down by one that is not.
"""
import json
import math
import sys
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from analytics.performance import profit_factor, sharpe_ratio  # noqa: E402


def _safe_response_class():
    """Load SafeJSONResponse without importing all of server.py.

    server.py pulls in the Postgres pool at import, which is not available in
    every environment the suite runs in - and this behaviour is a property of
    the class, not of the app.
    """
    src = (REPO / "server.py").read_text()
    start = src.index("class SafeJSONResponse")
    end = src.index("# default_response_class applies")
    import logging

    ns = {"JSONResponse": JSONResponse, "logging": logging}
    exec(compile(src[start:end], "server.py:SafeJSONResponse", "exec"), ns)
    return ns["SafeJSONResponse"]


class TestProfitFactorSentinel:
    """The origin. A known sentinel belongs where it is produced, not patched
    up downstream."""

    def test_no_losses_is_none_not_infinity(self):
        assert profit_factor([5.0, 3.0, 2.0]) is None

    def test_a_single_break_even_trade_also_has_no_denominator(self):
        """gross_loss sums `o <= 0`, so a break-even trade among winners leaves
        nothing to divide by. Easy to miss when reasoning about 'no losses'."""
        assert profit_factor([5.0, 0.0]) is None

    def test_a_real_ratio_is_still_a_number(self):
        assert profit_factor([5.0, -2.0]) == pytest.approx(2.5)

    def test_all_losers_is_zero_not_none(self):
        """Zero profit factor is a computed result and must stay distinguishable
        from 'not computable'."""
        assert profit_factor([-5.0, -2.0]) == 0.0

    def test_no_trades_is_zero(self):
        assert profit_factor([]) == 0.0

    def test_the_result_is_always_json_serialisable(self):
        """The property that actually matters, stated directly."""
        for outcomes in ([5.0, 3.0], [5.0, 0.0], [5.0, -2.0], [-1.0], []):
            json.dumps({"pf": profit_factor(outcomes)}, allow_nan=False)

    def test_sharpe_never_returns_a_non_finite_value(self):
        """Same payload, same failure if it ever divided by a zero stdev."""
        for outcomes in ([], [1.0], [2.0, 2.0], [1.0, -1.0], [0.0, 0.0]):
            v = sharpe_ratio(outcomes)
            assert math.isfinite(v), (outcomes, v)


class TestSafeJSONResponse:
    """The backstop, for sources nobody has hit yet. engine/ta_fallback.py
    divides by `.replace(0, np.nan)` in seven places; any NaN that reached a
    payload would 500 the route the same way."""

    def test_plain_jsonresponse_really_does_reject_infinity(self):
        """Guards the premise. If Starlette ever became lenient, the rest of
        this file would be testing nothing."""
        with pytest.raises(ValueError):
            JSONResponse({"x": float("inf")}).body

    def test_infinity_becomes_null(self):
        body = _safe_response_class()({"x": float("inf")}).body.decode()
        assert json.loads(body) == {"x": None}

    def test_nan_and_negative_infinity_become_null(self):
        body = _safe_response_class()(
            {"a": float("nan"), "b": float("-inf")}).body.decode()
        assert json.loads(body) == {"a": None, "b": None}

    def test_nested_structures_are_cleaned(self):
        payload = {"outer": {"inner": [1.0, float("inf"), {"deep": float("nan")}]}}
        body = _safe_response_class()(payload).body.decode()
        assert json.loads(body) == {
            "outer": {"inner": [1.0, None, {"deep": None}]}}

    def test_finite_values_are_untouched(self):
        """A sanitiser that rounds or reformats good data would be worse than
        the bug - these are money and percentages."""
        payload = {"a": 1.5, "b": 0.0, "c": -2.25, "d": None,
                   "e": "text", "f": True, "g": 7}
        assert json.loads(_safe_response_class()(payload).body.decode()) == payload

    def test_output_is_always_parseable_json(self):
        body = _safe_response_class()(
            {"x": float("inf"), "y": [float("nan")]}).body.decode()
        assert "Infinity" not in body and "NaN" not in body
        json.loads(body)

    def test_sanitising_is_logged_not_silent(self, caplog):
        """A NaN reaching the response layer is still a bug. This must not be
        the reason nobody ever finds it."""
        import logging

        with caplog.at_level(logging.WARNING, logger="trading"):
            _safe_response_class()({"pf": float("inf")}).body
        assert any("non-finite" in r.message for r in caplog.records), caplog.records

    def test_the_exact_payload_that_used_to_500(self):
        """Regression, in the shape /api/analytics/performance produced."""
        payload = {"n_closed_patterns": 3, "profit_factor": float("inf"),
                   "sharpe_ratio": 0.0, "win_rate_by_regime": {}}
        got = json.loads(_safe_response_class()(payload).body.decode())
        assert got["profit_factor"] is None
        assert got["n_closed_patterns"] == 3
