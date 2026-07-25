"""§2 + §6 - the validation receipt gate and the runtime posture banner.

The evaluation found all three live-execution gates open on a strategy with no
validated edge. §2 adds a fourth that cannot be satisfied by flipping a switch:
a validation receipt, written by run_backtest.py only when a run clears the
pre-committed go/no-go bar. §6 then makes the resolved posture the thing the
operator reads, so the banner can never disagree with behaviour the way the
five prose claims did.

    python3 -m pytest tests/test_live_arm_gate.py -v
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

lt = pytest.importorskip("engine.live_trader",
                         reason="requires the live-execution import chain")
banner = pytest.importorskip("storage.banner")

ARMED_CFG = {
    "trading": {"live_execution_enabled": True, "watch_execute": "EXECUTE",
                "auto_trade": True},
    "risk_level": "TURBO",
    "risk": {"kill_switch_triggered": False},
}


@pytest.fixture()
def receipt(tmp_path, monkeypatch):
    """Redirects the receipt path and clears TP_FORCE_PAPER, so these tests
    describe the gate rather than the environment they happen to run in."""
    monkeypatch.delenv("TP_FORCE_PAPER", raising=False)
    path = tmp_path / "live_arm_receipt.json"
    monkeypatch.setattr(lt, "_validation_receipt_path", lambda: str(path))

    def write(passed=True, age_days=0, **extra):
        body = {"passed": passed,
                "generated_at": (datetime.utcnow() - timedelta(days=age_days)).isoformat(),
                "summary": "expectancy +0.4% over 12 months", **extra}
        path.write_text(json.dumps(body))
        return path

    write.path = path
    return write


# ── the gate ────────────────────────────────────────────────────────────────

def test_no_receipt_blocks_live(receipt):
    """The default state of a system that has never validated. This is the one
    that matters most: it is the state the platform is in TODAY."""
    assert lt._validation_current()[0] is False
    assert lt.is_live_mode(ARMED_CFG) is False


def test_failed_receipt_blocks_live(receipt):
    receipt(passed=False, reason="expectancy -0.75%")
    ok, why = lt._validation_current()
    assert ok is False
    assert "FAILED" in why
    assert lt.is_live_mode(ARMED_CFG) is False


def test_stale_receipt_blocks_live(receipt):
    receipt(passed=True, age_days=45)
    ok, why = lt._validation_current()
    assert ok is False
    assert "old" in why
    assert lt.is_live_mode(ARMED_CFG) is False


def test_corrupt_receipt_blocks_live(receipt):
    """Fail closed. An unreadable receipt is not a receipt, and must never be
    treated as an absent CHECK rather than an absent PASS."""
    receipt.path.write_text("{ this is not json")
    ok, why = lt._validation_current()
    assert ok is False
    assert "unreadable" in why
    assert lt.is_live_mode(ARMED_CFG) is False


def test_current_receipt_permits_live(receipt):
    """CONTROL. With a fresh passing receipt and all three original gates
    open, is_live_mode must be True - otherwise the four tests above would
    pass even if the gate were simply hardwired shut."""
    receipt(passed=True, age_days=1)
    assert lt._validation_current()[0] is True
    assert lt.is_live_mode(ARMED_CFG) is True


def test_receipt_does_not_override_the_other_gates(receipt):
    """A valid receipt is necessary, never sufficient."""
    receipt(passed=True)
    for closed in ({"live_execution_enabled": False},
                   {"watch_execute": "WATCH"},
                   {"auto_trade": False}):
        cfg = {**ARMED_CFG, "trading": {**ARMED_CFG["trading"], **closed}}
        assert lt.is_live_mode(cfg) is False


def test_force_paper_env_vetoes_everything(receipt, monkeypatch):
    receipt(passed=True)
    monkeypatch.setenv("TP_FORCE_PAPER", "1")
    assert lt.is_live_execution_enabled(ARMED_CFG) is False
    assert lt.is_live_mode(ARMED_CFG) is False


# ── the banner ──────────────────────────────────────────────────────────────

def test_banner_reports_paper_when_blocked(receipt):
    p = banner.execution_posture(ARMED_CFG)
    assert p["mode"].startswith("ARMED but not trading")
    assert "no validation receipt" in p["validation"]


def test_banner_reports_live_when_armed(receipt):
    receipt(passed=True)
    p = banner.execution_posture(ARMED_CFG)
    assert p["mode"].startswith("LIVE")
    assert p["colour"] == "bold red"


def test_banner_reports_paper_with_switch_off(receipt):
    cfg = {**ARMED_CFG, "trading": {**ARMED_CFG["trading"], "live_execution_enabled": False}}
    p = banner.execution_posture(cfg)
    assert p["mode"].startswith("PAPER")
    assert p["master_switch"] is False


def test_banner_never_raises_on_a_broken_config():
    """The banner describes the process; it must not be able to kill it."""
    for cfg in (None, {}, {"trading": None}, {"risk": "nonsense"}):
        p = banner.execution_posture(cfg)
        assert "mode" in p and "validation" in p


def test_banner_agrees_with_the_execution_path(receipt):
    """The whole point of §6: the banner is DERIVED from the same gate
    functions the order path calls, so the two cannot disagree."""
    for passed in (True, False):
        receipt(passed=passed)
        p = banner.execution_posture(ARMED_CFG)
        assert p["mode"].startswith("LIVE") is lt.is_live_mode(ARMED_CFG)
