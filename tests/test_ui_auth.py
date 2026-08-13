"""§4 - the UI auth surface.

The UI_AUTH_TOKEN gate (rate-limited, constant-time-compared token check) was
removed: this server is for local single-user use, not a network service, so
require_token is now a no-op dependency. It is kept as a dependency object
(rather than deleted from every route) so a future change that DOES need to
expose the UI beyond localhost has exactly one function to fill back in.

    python3 -m pytest tests/test_ui_auth.py -v
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

fastapi = pytest.importorskip("fastapi")
server = pytest.importorskip("server", reason="requires the FastAPI import chain")


def _request(host="1.2.3.4"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


def test_require_token_always_passes():
    """No token needed - any value (including none) is accepted."""
    assert server.require_token(_request(), None) is True
    assert server.require_token(_request(), "anything") is True


def test_every_write_route_still_carries_the_dependency():
    """Structural check only: every write route still wires up
    Depends(require_token), even though the dependency itself is a no-op now.
    That keeps the plumbing in place for whoever reintroduces real auth if
    this server is ever exposed beyond 127.0.0.1."""
    unwired = []
    write_routes = []
    for route in server.app.routes:
        methods = (getattr(route, "methods", set()) or set())
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        write_routes.append(route.path)
        names = [d.call.__name__ for d in route.dependant.dependencies
                 if getattr(d, "call", None)]
        if "require_token" not in names:
            unwired.append(route.path)
    assert not unwired, f"write route(s) with no require_token dependency: {unwired}"
    assert len(write_routes) >= 14, f"only {len(write_routes)} write routes found"
