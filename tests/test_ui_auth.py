"""§4 - the UI auth surface.

Four compounding weaknesses were found: a 5-character token; stored in
cleartext in a versioned file; served over plain HTTP on 0.0.0.0; and compared
with a plain `!=` in nine separate endpoints with no rate limiting. That token
gates the kill switch, config mutation, arming live execution, and manual
real-money sells.

These tests exercise require_token directly rather than through a live server,
so they need no port, no database and no event loop.

    python3 -m pytest tests/test_ui_auth.py -v
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

fastapi = pytest.importorskip("fastapi")
server = pytest.importorskip("server", reason="requires the FastAPI import chain")

from fastapi import HTTPException  # noqa: E402

TOKEN = "K3f9pQ2xVn8sLm4wZr7tYb1cJd5hGa0e"


def _request(host="1.2.3.4"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """A known token and an empty lockout table for every test - the throttle
    is process-global by design, which makes leakage between tests the most
    likely way these could pass for the wrong reason."""
    monkeypatch.setattr(server, "_auth_token", lambda: TOKEN)
    server._fail_counts.clear()
    yield
    server._fail_counts.clear()


def test_correct_token_passes():
    assert server.require_token(_request(), TOKEN) is True


@pytest.mark.parametrize("bad", ["", None, "wrong", TOKEN[:-1], TOKEN + "x", TOKEN.upper()])
def test_wrong_token_is_403(bad):
    with pytest.raises(HTTPException) as e:
        server.require_token(_request(), bad)
    assert e.value.status_code == 403


def test_placeholder_token_is_rejected():
    """Phase 0 step 0.2 left config.yaml's ui.auth_token as the literal string
    '${UI_AUTH_TOKEN}' - server.py reads that YAML raw, without config_loader's
    expansion, so the old check was comparing every request against the
    placeholder. Anyone sending that string as a header would have been let
    in. The token now comes from storage/secrets.py instead; this test is the
    regression guard for the specific hole."""
    with pytest.raises(HTTPException) as e:
        server.require_token(_request(), "${UI_AUTH_TOKEN}")
    assert e.value.status_code == 403


def test_lockout_after_five_failures():
    req = _request("10.0.0.9")
    for _ in range(5):
        with pytest.raises(HTTPException) as e:
            server.require_token(req, "wrong")
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        server.require_token(req, "wrong")
    assert e.value.status_code == 429


def test_lockout_blocks_even_the_correct_token():
    """A brute-forcer who gets there on attempt six must not be rewarded for
    it. The throttle runs BEFORE the comparison, deliberately."""
    req = _request("10.0.0.10")
    for _ in range(5):
        with pytest.raises(HTTPException):
            server.require_token(req, "wrong")
    with pytest.raises(HTTPException) as e:
        server.require_token(req, TOKEN)
    assert e.value.status_code == 429


def test_lockout_is_per_client():
    attacker = _request("10.0.0.11")
    for _ in range(5):
        with pytest.raises(HTTPException):
            server.require_token(attacker, "wrong")
    assert server.require_token(_request("10.0.0.12"), TOKEN) is True


def test_unconfigured_token_fails_closed(monkeypatch):
    """A missing UI_AUTH_TOKEN must 503, not authenticate. An expected token
    of '' would compare equal to a blank header and silently unauthenticate
    every write endpoint in the process."""
    def _boom():
        raise RuntimeError("Secret 'UI_AUTH_TOKEN' not found")

    monkeypatch.setattr(server, "_auth_token", _boom)
    with pytest.raises(HTTPException) as e:
        server.require_token(_request(), "anything")
    assert e.value.status_code == 503


def test_every_write_route_is_guarded():
    """The structural test, and the reason §4 uses a dependency at all: an
    inline `if` can be forgotten on a new route, and that is exactly how a
    tenth write endpoint ships unauthenticated. Any route added to the list
    below must carry the dependency.

    As of v1.1.1 this is EVERY write route, not a chosen subset. The eight
    that were left open through v1.1.0 (run_now, cancel, validate,
    evaluate_now, backtest/run, alerts/resolve, prompt/copy,
    threshold_regret/run) are now guarded too, and the UI sends the header via
    authFetch().

    Deliberately written as "no write route lacks the dependency" rather than
    a fixed allow-list: a list has to be remembered, which is the same failure
    mode as an inline check."""
    unguarded = []
    write_routes = []
    for route in server.app.routes:
        methods = (getattr(route, "methods", set()) or set())
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        write_routes.append(route.path)
        names = [d.call.__name__ for d in route.dependant.dependencies
                 if getattr(d, "call", None)]
        if "require_token" not in names:
            unguarded.append(route.path)

    assert not unguarded, f"write route(s) with no auth dependency: {unguarded}"
    # Sanity floor: if the app ever stops registering routes, the assertion
    # above would pass vacuously.
    assert len(write_routes) >= 15, f"only {len(write_routes)} write routes found"


def test_no_inline_token_comparisons_remain():
    """The nine inline `x_auth_token != _auth_token()` checks are gone. A
    reintroduced one would be a plain string compare - which short-circuits on
    the first differing byte and leaks the token one character at a time to
    anyone who can measure response latency."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "server.py")).read()
    assert "!= _auth_token()" not in src
    assert "hmac.compare_digest" in src
