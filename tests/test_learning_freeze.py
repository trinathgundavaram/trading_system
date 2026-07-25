"""§17 - the Bayesian learning loop is frozen, and pattern rows carry provenance.

The gate was open on 23 closed patterns, every one produced under the stop bug
removed 2026-07-20. Nothing had moved only because require_shadow_validation
was true and bayesian_weight_history happened to be empty - luck holding, not a
control. Six weighted buckets and ~60 rules cannot be fitted from 23
observations in any case: the parameter space exceeds the sample, so anything
found is noise fitted to a defect.

    python3 -m pytest tests/test_learning_freeze.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

bu = pytest.importorskip("learning.bayesian_updater")
pdb_mod = pytest.importorskip("learning.pattern_database")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FROZEN_CFG = {"learning": {"bayesian_enabled": False, "min_trades_before_bayesian": 150,
                           "min_pattern_recorded_at": "2026-07-25T00:00:00",
                           "require_shadow_validation": True,
                           "bayesian_learning_rate": 0.01,
                           "bayesian_max_change_per_trade_pct": 3,
                           "bayesian_weekly_max_total_pct": 10,
                           "bayesian_monthly_max_total_pct": 25,
                           "loss_streak_halt": 5}}
THAWED_CFG = {"learning": {**FROZEN_CFG["learning"], "bayesian_enabled": True}}


class _StubDB:
    def get_recent_trades(self, limit=10):
        return []

    def get_weekly_bayesian_change(self, k):
        return 0.0

    def get_monthly_bayesian_change(self, k):
        return 0.0


# ── the freeze predicate ────────────────────────────────────────────────────

def test_frozen_by_config():
    frozen, why = bu.learning_frozen(FROZEN_CFG)
    assert frozen is True
    assert "bayesian_enabled=false" in why


def test_fails_closed_when_the_flag_is_absent():
    """An absent flag on a safety gate is not consent. A config that never
    mentions bayesian_enabled - an older config.yaml, or a hand-edited one -
    must be treated as frozen, not as permission."""
    assert bu.learning_frozen({})[0] is True
    assert bu.learning_frozen({"learning": {}})[0] is True
    assert bu.learning_frozen(None)[0] is True


def test_thawed_config_is_not_frozen():
    """CONTROL - otherwise every test here would pass on a hardwired True."""
    assert bu.learning_frozen(THAWED_CFG)[0] is False


# ── the proposal path ───────────────────────────────────────────────────────

def _propose(cfg, occurrences=500, **kw):
    return bu.BayesianUpdater(_StubDB(), cfg).propose_update(
        "rsi_oversold", "MOMENTUM", 20.0, occurrences, 0.62, 0.45,
        frequency_class="COMMON", mode="swing", **kw)


def test_proposal_blocked_while_frozen():
    p = _propose(FROZEN_CFG)
    assert p["blocked"] is True
    assert "bayesian_enabled=false" in p["block_reason"]
    assert p["new_weight"] == p["old_weight"]


def test_freeze_outranks_the_trade_count():
    """Ordering matters. With 500 occurrences the count gate would pass, so a
    block here can only come from the freeze - which proves the freeze is
    checked first and cannot be argued past with more data."""
    assert "bayesian_enabled=false" in _propose(FROZEN_CFG, occurrences=500)["block_reason"]


def test_thawed_still_blocks_on_the_raised_minimum():
    """With the freeze lifted, 23 patterns still gets nowhere near the 150
    floor - the two gates are independent on purpose."""
    p = _propose(THAWED_CFG, occurrences=23)
    assert p["blocked"] is True
    assert "need 150" in p["block_reason"]


def test_thawed_with_enough_evidence_proposes():
    """CONTROL. Freeze off and 500 occurrences: an actual proposal."""
    p = _propose(THAWED_CFG, occurrences=500)
    assert p["blocked"] is False
    assert p["new_weight"] != p["old_weight"]


def test_force_is_an_explicit_escape_hatch():
    assert _propose(FROZEN_CFG, occurrences=500, force=True)["blocked"] is False


# ── the write paths ─────────────────────────────────────────────────────────

def test_config_write_refuses_while_frozen():
    """apply_bucket_weight_to_config is the ONLY function that writes a bucket
    weight to config.yaml, so it is the one that must not be bypassable."""
    with pytest.raises(bu.LearningFrozen):
        bu.apply_bucket_weight_to_config("MOMENTUM", 22.0, mode="swing", cfg=FROZEN_CFG)


def test_config_write_reports_the_freeze_not_shadow_validation():
    """Two different problems with two different remedies. Reporting the
    shadow gate here would send you off to run a challenge that §17 also
    forbids."""
    with pytest.raises(bu.LearningFrozen) as e:
        bu.apply_bucket_weight_to_config("MOMENTUM", 22.0, mode="swing", cfg=FROZEN_CFG)
    assert "bayesian_enabled=false" in str(e.value)


def test_shadow_gate_still_applies_once_thawed():
    """CONTROL. Lifting the freeze must not lift the overfitting guardrail."""
    with pytest.raises(bu.ShadowValidationRequired):
        bu.apply_bucket_weight_to_config("MOMENTUM", 22.0, mode="swing", cfg=THAWED_CFG)


def test_challenge_start_refuses_while_frozen():
    """A challenge started on contaminated evidence produces a result that
    LOOKS like out-of-sample proof and would then be fed to
    apply_challenge_promoted_weight. Block it before it exists."""
    with pytest.raises(bu.LearningFrozen):
        bu.propose_as_challenge(FROZEN_CFG, _StubDB(), "MOMENTUM", 22.0)


# ── provenance ──────────────────────────────────────────────────────────────

def test_fingerprint_is_stable_and_order_independent():
    a = {"weights": {"swing_buy": {"bucket_weights": {"TREND": 0.25, "MOMENTUM": 0.2}}},
         "risk_level": "TURBO"}
    b = {"risk_level": "TURBO",
         "weights": {"swing_buy": {"bucket_weights": {"MOMENTUM": 0.2, "TREND": 0.25}}}}
    assert pdb_mod.config_fingerprint(a) == pdb_mod.config_fingerprint(b)


def test_fingerprint_changes_when_a_weight_changes():
    """This is what makes the §19 recalibration self-partitioning: the moment
    the weights change, every later pattern is distinguishable from every
    earlier one with nobody having to remember the date."""
    a = {"weights": {"swing_buy": {"bucket_weights": {"TREND": 0.25}}}}
    b = {"weights": {"swing_buy": {"bucket_weights": {"TREND": 0.30}}}}
    assert pdb_mod.config_fingerprint(a) != pdb_mod.config_fingerprint(b)


def test_fingerprint_ignores_non_decision_config():
    """Deliberately narrow. Adding a key invalidates comparability with every
    previously recorded pattern, so a watchlist edit must not do it."""
    a = {"risk_level": "TURBO", "watchlist": ["ASTS"], "notifications": {"enabled": True}}
    b = {"risk_level": "TURBO", "watchlist": ["NVDA", "HCA"], "notifications": {"enabled": False}}
    assert pdb_mod.config_fingerprint(a) == pdb_mod.config_fingerprint(b)


def test_fingerprint_never_raises_on_odd_values():
    class Weird:
        pass

    assert len(pdb_mod.config_fingerprint({"risk_level": Weird()})) == 16


# ── the shipped config actually says all this ───────────────────────────────

def test_shipped_config_is_frozen():
    with open(os.path.join(REPO, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    learning = cfg["learning"]
    assert learning["bayesian_enabled"] is False
    assert learning["min_trades_before_bayesian"] == 150
    assert learning["require_shadow_validation"] is True
    assert str(learning["min_pattern_recorded_at"]).startswith("2026-07-25")
    assert bu.learning_frozen(cfg)[0] is True


def test_shipped_config_keeps_live_execution_closed():
    """Phase 1 must not have reopened anything Phase 0 step 0.1 closed."""
    with open(os.path.join(REPO, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    t = cfg["trading"]
    assert t["auto_trade"] is False
    assert t["live_execution_enabled"] is False
    assert str(t["watch_execute"]).upper() == "WATCH"
