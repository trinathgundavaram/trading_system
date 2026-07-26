"""Schema-1 pattern rows must not be re-read as measurements they never made.

THE FAILURE THIS GUARDS AGAINST is a second-order one, and it is the reason
fixing engine/pattern_features.py was not a one-line change.

While every row in the table held adx = 0.0, that column's standard deviation
was zero, `_encode_patterns` clamped it to 1.0, and every row encoded to
exactly 0.0. Useless, but symmetric and harmless. The instant real ADX
readings (15-40) start being written, the column acquires a real mean, and
every historical 0.0 z-scores to a large negative number. Those old rows stop
being uninformative and start asserting "extremely low ADX" - a measurement
nobody ever took. The similarity search would then confidently match new
low-ADX setups against a cohort of rows whose only qualification is that they
predate the fix.

So the fix has two halves, and this file tests the half that has no visible
symptom: reading. If someone later removes the schema check as dead code
because "all the rows have the stamp now", these tests fail and explain why
the stamp exists.
"""
import numpy as np
import pytest

from learning import pattern_database as pdb

REAL = {
    "adx": 30.0, "cmf": 0.2, "sector_rs_1d": 1.0, "sector_rs_1m": 2.0,
    "squeeze_active": True, "unusual_options": True, "opex_status": "opex_week",
}


def _row(schema=None, adx=None, **over):
    """`adx` varies deliberately across schema-2 rows in the tests below. If
    every real reading were identical the column's std would be 0, the encoder
    would clamp it to 1.0, and every row - real or imputed - would z-score to
    0.0, which would make these assertions pass for the wrong reason."""
    f = {"rsi14": 55.0, "final_score": 70.0}
    if schema == 1:
        # exactly what the old writer produced
        f.update({"adx": 0.0, "cmf": 0.0, "sector_rs_1d": 0.0, "sector_rs_1m": 0.0,
                  "squeeze_active": False, "unusual_options": False,
                  "opex_status": "normal"})
    elif schema == 2:
        f.update(REAL)
        f["feature_schema"] = 2
        if adx is not None:
            f["adx"] = adx
    f.update(over)
    return {"features": f}


def _idx(name):
    return pdb.NUMERIC_FEATURES.index(name)


# ── the core guarantee ──────────────────────────────────────────────────────

def test_schema1_numeric_constants_encode_as_neutral_not_extreme():
    """The whole point. A schema-1 row mixed with real ones must sit at z = 0
    on adx, not at the bottom of the distribution."""
    patterns = [_row(1), _row(1), _row(2, adx=20.0), _row(2, adx=40.0)]
    q, vecs = pdb._encode_patterns(patterns, dict(REAL, feature_schema=2))
    i = _idx("adx")
    assert vecs[0][i] == pytest.approx(0.0), "schema-1 row asserted a real ADX"
    assert vecs[1][i] == pytest.approx(0.0)
    assert vecs[2][i] != pytest.approx(0.0), "schema-2 row lost its real ADX"
    assert vecs[3][i] != pytest.approx(0.0)


def test_without_the_guard_the_old_rows_would_read_as_extreme():
    """Demonstrates the bug rather than trusting the prose above. Disables the
    schema check (every row reads as current, which is what the pre-2026-07-26
    encoder effectively did) and shows the same 0.0 becoming a strong negative
    - the historical row asserting 'extremely low ADX' rather than silence."""
    patterns = [_row(1), _row(1), _row(2, adx=20.0), _row(2, adx=40.0)]
    i = _idx("adx")

    original = pdb._row_schema
    pdb._row_schema = lambda feats: 2          # pretend nothing is stale
    try:
        _, unguarded = pdb._encode_patterns(patterns, dict(REAL, feature_schema=2))
    finally:
        pdb._row_schema = original

    assert unguarded[0][i] < -0.5, (
        "the unguarded encoding no longer misreads old rows - if the writer "
        "changed, this test's premise needs revisiting"
    )

    # and with the guard in place, the same row says nothing instead
    _, guarded = pdb._encode_patterns(patterns, dict(REAL, feature_schema=2))
    assert guarded[0][i] == pytest.approx(0.0)


def test_schema1_categoricals_get_their_own_bucket():
    """A stale False must not one-hot into the same slot as a measured False,
    which would make an unmeasured row look like positive evidence of 'no
    squeeze'."""
    # A measured False (schema 2) and an unmeasured one (schema 1) must land in
    # different one-hot slots, so their encodings differ on that axis.
    measured_false = _row(2)
    measured_false["features"]["squeeze_active"] = False
    unmeasured = _row(1)   # also carries squeeze_active False, but never observed

    _, vecs = pdb._encode_patterns([unmeasured, measured_false],
                                   dict(REAL, feature_schema=2))
    n_numeric = len(pdb.NUMERIC_FEATURES)
    assert not np.allclose(vecs[0][n_numeric:], vecs[1][n_numeric:]), (
        "an unmeasured squeeze_active encoded identically to a measured False"
    )
    assert pdb.UNRECORDED not in ("", "False", "normal", "True")


def test_a_schema1_row_and_a_schema2_row_are_not_identical_on_those_axes():
    """Two rows that differ only in what was recorded must not encode
    identically - otherwise the fix bought nothing."""
    q, vecs = pdb._encode_patterns([_row(1), _row(2)], dict(REAL, feature_schema=2))
    assert not np.allclose(vecs[0], vecs[1])


# ── robustness ──────────────────────────────────────────────────────────────

def test_all_rows_schema1_still_works():
    """The normal state for a while after this ships: nothing is stamped yet.
    Must not warn, must not emit NaN."""
    with np.errstate(all="raise"):
        q, vecs = pdb._encode_patterns([_row(1), _row(1), _row(1)], _row(1)["features"])
    assert np.isfinite(q).all()
    assert all(np.isfinite(v).all() for v in vecs)


def test_all_rows_schema2_still_works():
    q, vecs = pdb._encode_patterns([_row(2), _row(2)], dict(REAL, feature_schema=2))
    assert np.isfinite(q).all()
    assert all(np.isfinite(v).all() for v in vecs)


def test_no_nan_ever_reaches_a_vector():
    """Cosine similarity against a NaN silently returns NaN, which compares
    False against every threshold - a cohort would vanish with no error."""
    mixed = [_row(1), _row(2), _row(1), _row(2)]
    q, vecs = pdb._encode_patterns(mixed, dict(REAL, feature_schema=2))
    assert np.isfinite(q).all()
    for v in vecs:
        assert np.isfinite(v).all()


def test_a_missing_numeric_key_still_defaults_to_zero():
    """Unchanged behaviour for keys that are simply absent - only the seven
    named features get the missing-data treatment."""
    q, vecs = pdb._encode_patterns([_row(2), {"features": {"feature_schema": 2}}],
                                   dict(REAL, feature_schema=2))
    assert np.isfinite(vecs[1]).all()


def test_a_non_numeric_value_does_not_raise():
    """These come from live MCP payloads via the writer; a bad value must not
    take down every EV lookup in the cycle."""
    bad = _row(2)
    bad["features"]["adx"] = "n/a"
    q, vecs = pdb._encode_patterns([bad, _row(2)], dict(REAL, feature_schema=2))
    assert np.isfinite(vecs[0]).all()


def test_unstamped_rows_are_schema_1():
    assert pdb._row_schema({}) == 1
    assert pdb._row_schema({"feature_schema": None}) == 1
    assert pdb._row_schema({"feature_schema": 2}) == 2


def test_schema_version_matches_what_the_writer_stamps():
    """The writer and the reader agreeing is the entire contract."""
    from engine.pattern_features import FEATURE_SCHEMA_VERSION as writer_version
    assert writer_version == pdb.FEATURE_SCHEMA_VERSION


def test_fingerprint_deliberately_ignores_the_schema_bump():
    """config_fingerprint answers 'different strategy?', and the strategy did
    not change here - only what was recorded about it. Folding the schema in
    would discard the whole pattern history for a recoverable gap."""
    cfg = {"weights": {"a": 1}, "risk_level": "MODERATE"}
    before = pdb.config_fingerprint(cfg)
    assert before == pdb.config_fingerprint(dict(cfg))
    assert "feature_schema" not in str(before)
