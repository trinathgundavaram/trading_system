"""The exit_kind coverage label, and the enforcement that it gets shown.

WHY A COVERAGE NUMBER IS NOT OPTIONAL. `exit_kind` is NULL wherever
rules/common.py's classify_exit() declined to guess - which is the right
behaviour, since a wrong kind is worse than a missing one. The consequence is
that every GROUP BY exit_kind analyses a subset while looking exactly like a
complete analysis. "trailing_stop: 60%" reads as a claim about the strategy
and may be a claim about 12 of 68 trades.

The unclassified rows are also not a random subset - they are the ones whose
exit_reason was prose, which correlates with the older sell_rules path. So a
partial sample is not merely small, it is plausibly biased, and the label says
so rather than leaving the reader to round "41/68" up to "most of them".

These tests need no database. The DB-backed half - that the count is correct
against real rows - lives in tests/test_mae_mfe_fk_lifecycle.py's neighbours
and requires Postgres; this file covers the wording and the wiring, which is
where the regression risk actually is.
"""
import re
from pathlib import Path

from rules.common import EXIT_KINDS, format_exit_kind_coverage

REPO = Path(__file__).resolve().parents[1]


# ── the label ───────────────────────────────────────────────────────────────

def test_complete_coverage_says_so():
    s = format_exit_kind_coverage(68, 68)
    assert "68/68" in s and "100%" in s and "complete" in s
    assert "MINORITY" not in s


def test_partial_coverage_names_the_exclusion():
    s = format_exit_kind_coverage(41, 68)
    assert "41/68" in s and "60%" in s
    assert "excluded" in s, "a partial label must say the rest are excluded"


def test_minority_coverage_warns_about_bias():
    """Below half, "partial" understates it. The unclassified rows correlate
    with the older exit path, so the visible half may be systematically
    different from the hidden half."""
    s = format_exit_kind_coverage(12, 68)
    assert "12/68" in s
    assert "MINORITY" in s
    assert "biased" in s or "bias" in s


def test_the_boundary_is_not_off_by_one():
    assert "MINORITY" in format_exit_kind_coverage(33, 68)      # 48.5%
    assert "MINORITY" not in format_exit_kind_coverage(34, 68)  # 50.0%


def test_empty_book_is_not_reported_as_zero_percent():
    """0/0 is "no trades", not "0% classified". Rendering the second as the
    first is a false alarm on a fresh install, and a false alarm that fires
    every time is one that gets ignored when it matters."""
    s = format_exit_kind_coverage(0, 0)
    assert "no closed trades" in s
    assert "0%" not in s


def test_the_label_always_carries_both_numbers():
    """A percentage alone hides the sample size; a count alone hides the
    proportion. Every caller gets both, in one string, so neither can be
    dropped in transcription."""
    for structured, total in ((0, 5), (1, 2), (41, 68), (68, 68)):
        s = format_exit_kind_coverage(structured, total)
        assert f"{structured}/{total}" in s


# ── the wiring (placement assertions - the failure mode is a dropped call) ──

def test_the_performance_endpoint_carries_coverage():
    """This is the endpoint a breakdown-by-exit-kind would be added to. The
    denominator ships first so the panel that eventually renders it cannot be
    written without one."""
    src = (REPO / "server.py").read_text()
    body = src[src.index('@app.get("/api/analytics/performance")'):]
    body = body[:body.index("@app.get", 10)]
    assert "get_exit_kind_coverage" in body, (
        "/api/analytics/performance stopped returning exit_kind_coverage")


def test_phase4_assess_reports_coverage():
    """phase4_recalibrate.py already SELECTs exit_kind into every sample it
    loads, which makes it the first real consumer and the first place a
    partial sample could be mistaken for a complete one."""
    src = (REPO / "scripts" / "phase4_recalibrate.py").read_text()
    assert "get_exit_kind_coverage" in src
    assert "cov['label']" in src or 'cov["label"]' in src, (
        "coverage is fetched but the label is not printed")


def test_the_database_helper_exists_and_returns_the_label():
    """The label is built once, in the accessor, so a consumer cannot show the
    numbers without the caveat that travels with them."""
    src = (REPO / "storage" / "database.py").read_text()
    body = src[src.index("def get_exit_kind_coverage"):]
    body = body[:body.index("\n    def ", 10)]
    for key in ("structured", "total", "missing", "pct", "label",
                "unclassified_reasons"):
        assert f'"{key}"' in body, f"coverage payload lost its {key!r} key"
    assert "format_exit_kind_coverage" in body


def test_coverage_counts_only_closed_patterns():
    """An open position has no exit yet. Counting it as unclassified would
    make coverage fall every time a position is opened, which is noise that
    would train the reader to ignore the number."""
    src = (REPO / "storage" / "database.py").read_text()
    body = src[src.index("def get_exit_kind_coverage"):]
    body = body[:body.index("\n    def ", 10)]
    assert "is_closed = 1" in body


# ── the vocabulary itself ───────────────────────────────────────────────────

def test_every_exit_kind_is_a_plain_lowercase_token():
    """These are stored values and grouped on. A stray space or capital would
    split one bucket into two that read as the same thing."""
    for k in EXIT_KINDS:
        assert re.fullmatch(r"[a-z_]+", k), f"{k!r} is not a plain token"


def test_verify_phase2_enforces_the_cutover_migrations():
    """§19-§21 re-derive scoring, thresholds and sizing tiers from the
    measurement base 009-012 establish. Until 2026-07-26 that base was checked
    nowhere except the runbook, so a Phase 3 release could be tagged against a
    database where an excursion row belonged to a different ticker - which does
    not error, it just fits on the wrong numbers.

    verify_phase2.py is called by release.sh, so these are the gate. Asserted
    here because a check that is deleted or downgraded to a warning leaves no
    other trace."""
    src = (REPO / "scripts" / "verify_phase2.py").read_text()
    assert "idx_mae_mfe_trade_id" in src, "the 010 unique-index check is gone"
    assert "mae_mfe_data.trade_id is INTEGER (012)" in src
    assert "confdeltype" in src, "the FK delete rule is no longer inspected"
    assert "SET NULL, not CASCADE" in src or "not CASCADE (012)" in src
    assert "orphaned excursion rows" in src, "the §49 purge check is gone"


def test_the_cutover_checks_are_hard_fails_not_warnings():
    """check(name, None, ...) is this script's WARN form; check(name, <bool>)
    is a FAIL. The whole point of moving these out of the runbook is that they
    block a tag, so none of them may be passed None."""
    src = (REPO / "scripts" / "verify_phase2.py").read_text()
    for name in ("idx_mae_mfe_trade_id exists (010)",
                 "mae_mfe_data.trade_id is INTEGER (012)",
                 "no orphaned excursion rows (§49 purge)"):
        i = src.index(name)
        call = src[i:i + 400]
        assert ", None," not in call.split(")")[0] + ")", (
            f"{name!r} was downgraded to a warning - it must block release.sh")


def test_the_phase4_proposal_carries_a_model_identity():
    """Two proposals from either side of a §19 weight change are structurally
    identical documents describing different models, and the default output
    path is the same file every run. Without model_id the only distinguishing
    field is a timestamp, which is how a later notebook ends up pooling two EV
    curves and getting a plausible third one."""
    src = (REPO / "scripts" / "phase4_recalibrate.py").read_text()
    assert '"model_id": model_id' in src, "the proposal lost its model identity"
    for field in ("config_fingerprint", "app_version", "writer_feature_schema",
                  "sample_feature_schemas", "mixed_feature_schema"):
        assert field in src, f"model_id lost {field!r}"


def test_model_identity_imports_only_functions_that_exist():
    """model_id is built inside a bare try/except so provenance can never block
    a proposal - which means a bad import degrades silently to
    {'unavailable': ...} instead of failing. That is the right runtime
    behaviour and the wrong thing to leave untested."""
    import importlib

    from learning.pattern_database import (FEATURE_SCHEMA_VERSION,  # noqa: F401
                                           config_fingerprint)
    v = importlib.import_module("storage.version")
    src = (REPO / "scripts" / "phase4_recalibrate.py").read_text()
    block = src[src.index("model_id = {"):src.index('"sample": {')]
    for name in re.findall(r"\b(app_version|engine_version|ta_backend)\(\)", block):
        assert hasattr(v, name), (
            f"model_id calls storage.version.{name}(), which does not exist - "
            f"the whole block would degrade to 'unavailable' at runtime")


def test_sell_rules_emits_exit_kind_natively():
    """Guards against a stale belief rather than a stale code path. §D gave
    SellResult its own exit_kind field because classify_exit() correctly
    refuses to parse `sell_rules:` prose - so if this field were ever removed,
    the MOST COMMON exit path would silently go back to NULL and coverage
    would decay with no other symptom."""
    src = (REPO / "rules" / "sell_rules.py").read_text()
    assert "exit_kind: str" in src, "SellResult lost its exit_kind field"
    assert "exit_kind=exit_kind" in src, (
        "sell_rules builds an exit_kind but no longer passes it to SellResult")
