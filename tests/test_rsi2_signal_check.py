"""Unit tests for scripts/rsi2_signal_check.py's own logic (state file
read/write, action -> notify/state wiring), independent of live network
access. mcp_clients.market_data can't even be imported in this sandbox
(it depends on the separate `mcp` SDK package, which isn't installed here -
a pre-existing environment gap unrelated to this work, also seen with
psycopg2 elsewhere in this repo's test suite) - so a fake module is
injected into sys.modules before importing the script under test, the same
way the real module would provide MarketDataRouter.get_daily_bars() on
Trinath's own machine, where the real dependency IS installed.
"""
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest


def _fake_bars(closes: list, start_date: str = "2016-01-04") -> list:
    dates = pd.date_range(start_date, periods=len(closes), freq="D")
    return [{"date": str(d.date()), "open": c - 0.2, "high": c + 0.3, "low": c - 0.3, "close": c, "volume": 1000}
            for d, c in zip(dates, closes)]


@pytest.fixture
def script_module(monkeypatch, tmp_path):
    """Injects a fake mcp_clients.market_data module (see module docstring),
    then imports scripts.rsi2_signal_check fresh so it binds to the fake.
    Also redirects STATE_PATH into a tmp dir so tests never touch the real
    repo's state/ directory, and stubs notify() to record calls instead of
    hitting any real transport."""
    fake_market_data = types.ModuleType("mcp_clients.market_data")

    class _FakeRouter:
        bars_to_return = None  # set per-test

        def get_daily_bars(self, ticker):
            if _FakeRouter.bars_to_return is None:
                return None
            return _FakeRouter.bars_to_return, "fake_provider"

    fake_market_data.MarketDataRouter = _FakeRouter
    fake_mcp_clients_pkg = sys.modules.get("mcp_clients") or types.ModuleType("mcp_clients")
    monkeypatch.setitem(sys.modules, "mcp_clients", fake_mcp_clients_pkg)
    monkeypatch.setitem(sys.modules, "mcp_clients.market_data", fake_market_data)

    sys.modules.pop("scripts.rsi2_signal_check", None)
    import scripts.rsi2_signal_check as mod
    import importlib
    importlib.reload(mod)  # rebind MarketDataRouter to the fake now that it's in sys.modules

    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "rsi2_spy_signal_state.json")
    monkeypatch.setattr(mod, "load_config_dict", lambda: {})

    notify_calls = []
    monkeypatch.setattr(mod, "notify", lambda cfg, **kw: notify_calls.append(kw) or True)
    mod._notify_calls = notify_calls  # stash for assertions

    # Real production defaults need 201 bars minimum - use the small-period
    # override so tests can run on a short, easy-to-reason-about series,
    # same technique as test_rsi2_spy_swing.py.
    from rules import rsi2_spy_swing
    monkeypatch.setattr(rsi2_spy_swing, "SMA_TREND_PERIOD", 10)
    monkeypatch.setattr(rsi2_spy_swing, "SMA_EXIT_PERIOD", 3)
    monkeypatch.setattr(rsi2_spy_swing, "RSI_PERIOD", 2)
    monkeypatch.setattr(rsi2_spy_swing, "RSI_THRESHOLD", 25)
    monkeypatch.setattr(rsi2_spy_swing, "EXIT_MODE", "rsi70")
    monkeypatch.setattr(rsi2_spy_swing, "MAX_HOLD_DAYS", 5)

    return mod


DIP_AND_RECOVER_CLOSES = [
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
    110, 109, 107.5, 108.2, 111, 113,
]


def test_state_round_trip(script_module, tmp_path):
    mod = script_module
    assert mod.load_state() == {"in_position": False, "entry_date": None, "entry_price": None}
    mod.save_state({"in_position": True, "entry_date": "2020-01-14", "entry_price": 107.9})
    reloaded = mod.load_state()
    assert reloaded["in_position"] is True
    assert reloaded["entry_date"] == "2020-01-14"
    assert "last_updated" in reloaded


def test_enter_signal_notifies_and_saves_position(script_module, monkeypatch):
    mod = script_module
    from mcp_clients.market_data import MarketDataRouter
    MarketDataRouter.bars_to_return = _fake_bars(DIP_AND_RECOVER_CLOSES[:13])  # through index 12, the signal bar
    monkeypatch.setattr(sys, "argv", ["rsi2_signal_check.py"])
    mod.main()
    assert len(mod._notify_calls) == 1
    assert "BUY" in mod._notify_calls[0]["title"]
    state = mod.load_state()
    assert state["in_position"] is True


def test_exit_signal_notifies_and_clears_position(script_module, monkeypatch):
    mod = script_module
    mod.save_state({"in_position": True, "entry_date": str(pd.date_range("2016-01-04", periods=14, freq="D")[13].date()),
                     "entry_price": 108.0})
    from mcp_clients.market_data import MarketDataRouter
    MarketDataRouter.bars_to_return = _fake_bars(DIP_AND_RECOVER_CLOSES[:15])  # through index 14, rsi70 exit fires
    monkeypatch.setattr(sys, "argv", ["rsi2_signal_check.py"])
    mod.main()
    assert len(mod._notify_calls) == 1
    assert "EXIT" in mod._notify_calls[0]["title"]
    state = mod.load_state()
    assert state["in_position"] is False


def test_hold_sends_no_notification(script_module, monkeypatch):
    mod = script_module
    entry_date = str(pd.date_range("2016-01-04", periods=14, freq="D")[13].date())
    mod.save_state({"in_position": True, "entry_date": entry_date, "entry_price": 108.0})
    from mcp_clients.market_data import MarketDataRouter
    # index 13 only - HOLD per test_rsi2_spy_swing.py's own verified fixture behavior
    MarketDataRouter.bars_to_return = _fake_bars(DIP_AND_RECOVER_CLOSES[:14])
    monkeypatch.setattr(sys, "argv", ["rsi2_signal_check.py"])
    mod.main()
    assert mod._notify_calls == []
    assert mod.load_state()["in_position"] is True  # unchanged


def test_dry_run_does_not_touch_state_or_notify(script_module, monkeypatch):
    mod = script_module
    from mcp_clients.market_data import MarketDataRouter
    MarketDataRouter.bars_to_return = _fake_bars(DIP_AND_RECOVER_CLOSES[:13])
    monkeypatch.setattr(sys, "argv", ["rsi2_signal_check.py", "--dry-run"])
    mod.main()
    assert mod._notify_calls == []
    assert not mod.STATE_PATH.exists()


def test_no_provider_data_raises(script_module, monkeypatch):
    mod = script_module
    from mcp_clients.market_data import MarketDataRouter
    MarketDataRouter.bars_to_return = None
    monkeypatch.setattr(sys, "argv", ["rsi2_signal_check.py"])
    with pytest.raises(RuntimeError, match="no configured provider"):
        mod.main()
