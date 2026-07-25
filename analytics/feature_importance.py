"""Feature Importance Tracking - closes a gap flagged in a deployment review
about overfitting risk: this codebase's walk-forward analysis
(learning/walk_forward.py) tracks per-RULE attribution (named binary rules
like "rsi_oversold_38" from BuyResult.rules_passed), but nothing ranked the
full 25-feature pattern-database vector (learning/pattern_database.py's
NUMERIC_FEATURES + CATEGORICAL_FEATURES) by actual predictive power. Without
this, a feature can silently carry zero signal - or have LOST whatever
signal it once had - and nobody would notice. That's exactly the kind of
blind spot that lets a system with this many free parameters (7 buckets x
~30 rules, dynamic thresholds, regime adaptation, position sizing) quietly
overfit: more knobs than the data can support, with no visibility into which
ones are dead weight vs. carrying real signal.

Two kinds of features need two different measures:
  - NUMERIC: point-biserial correlation between the raw feature value and
    win (outcome_pct > 0) across closed trades.
  - CATEGORICAL: reuses analytics/performance.py's win_rate_by() (already
    built, already the right tool for this) - a feature's SPREAD across its
    own categories' win rates is its discriminating power. A feature where
    every category shows ~50% win rate carries no information regardless of
    how many categories it has.

Both get the SAME temporal-drift treatment as learning/walk_forward.py's
feature_stability() (30/90/180-day windows, same STABILITY_WINDOWS_DAYS) -
directly comparable to that module's rule-level drift labels. A feature
whose importance swings wildly between windows is either noisy or the
system is fitting a transient pattern, not a durable one - the same
DRIFTING/STABLE distinction walk_forward.py already makes for rules, now
made for every feature the pattern database tracks.

Sample-size gating matches this codebase's existing convention throughout
the learning stack (walk_forward.py's rule_attribution() uses the same
n<20 bar) - insufficient data means "don't rank this yet," never "assume
zero importance."
"""
import math
from datetime import datetime, timedelta

from analytics.performance import win_rate_by
from learning.pattern_database import NUMERIC_FEATURES, CATEGORICAL_FEATURES

MIN_SAMPLE_SIZE = 20  # matches learning/walk_forward.py's rule_attribution() bar
STABILITY_WINDOWS_DAYS = [30, 90, 180]  # same windows as walk_forward.py, for direct comparability


def _point_biserial(values: list, wins: list) -> float | None:
    """Correlation between a continuous feature and a binary win/loss
    outcome. None if there's no variance to correlate against (constant
    feature, or the sample is all-wins or all-losses)."""
    n = len(values)
    if n < 2:
        return None
    win_vals = [v for v, w in zip(values, wins) if w]
    loss_vals = [v for v, w in zip(values, wins) if not w]
    if not win_vals or not loss_vals:
        return None
    n1, n0 = len(win_vals), len(loss_vals)
    m1, m0 = sum(win_vals) / n1, sum(loss_vals) / n0
    mean_all = sum(values) / n
    var = sum((v - mean_all) ** 2 for v in values) / n
    std = math.sqrt(var)
    if std == 0:
        return None
    return ((m1 - m0) / std) * math.sqrt((n1 * n0) / (n * n))


def numeric_feature_importance(patterns: list, feature: str) -> dict:
    rows = [
        (float(p["features"].get(feature) or 0.0), p["outcome_pct"] > 0)
        for p in patterns
        if p.get("outcome_pct") is not None and p["features"].get(feature) is not None
    ]
    n = len(rows)
    if n < MIN_SAMPLE_SIZE:
        return {"feature": feature, "type": "numeric", "n": n, "insufficient_data": True,
                "reason": f"only {n} trades, need {MIN_SAMPLE_SIZE}"}

    values, wins = [r[0] for r in rows], [r[1] for r in rows]
    corr = _point_biserial(values, wins)
    if corr is None:
        return {"feature": feature, "type": "numeric", "n": n, "insufficient_data": True,
                "reason": "no variance in feature or outcome (constant feature, or all-win/all-loss sample)"}

    return {
        "feature": feature, "type": "numeric", "n": n, "insufficient_data": False,
        "correlation_with_win": round(corr, 3), "abs_importance": round(abs(corr), 3),
    }


def categorical_feature_importance(patterns: list, feature: str) -> dict:
    breakdown = win_rate_by(patterns, feature)
    n_total = sum(v["n"] for v in breakdown.values())
    # Ignore near-empty categories (n<5) when computing the SPREAD - one
    # category with 2 trades at 100% win rate shouldn't count as "this
    # feature is highly predictive," it's just noise from a tiny sample.
    win_rates = [v["win_rate"] for v in breakdown.values() if v["n"] >= 5]

    if n_total < MIN_SAMPLE_SIZE or len(win_rates) < 2:
        return {"feature": feature, "type": "categorical", "n": n_total, "insufficient_data": True,
                "reason": f"only {n_total} trades across {len(breakdown)} categories "
                          f"({len(win_rates)} with >=5 trades), need {MIN_SAMPLE_SIZE} total and >=2 categories",
                "breakdown": breakdown}

    spread = max(win_rates) - min(win_rates)
    return {
        "feature": feature, "type": "categorical", "n": n_total, "insufficient_data": False,
        "win_rate_spread": round(spread, 3), "abs_importance": round(spread, 3),
        "breakdown": breakdown,
    }


def feature_importance(patterns: list, feature: str) -> dict:
    if feature in NUMERIC_FEATURES:
        return numeric_feature_importance(patterns, feature)
    return categorical_feature_importance(patterns, feature)


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.utcnow()


def _regime_distribution(patterns: list) -> dict:
    """{regime_name: fraction of patterns in that regime}, using the
    'regime' categorical feature every pattern already carries (see
    learning/pattern_database.py's CATEGORICAL_FEATURES). Empty dict if
    there's nothing to measure."""
    n = len(patterns)
    if n == 0:
        return {}
    counts = {}
    for p in patterns:
        r = (p.get("features") or {}).get("regime") or "UNKNOWN"
        counts[r] = counts.get(r, 0) + 1
    return {k: v / n for k, v in counts.items()}


def _total_variation_distance(dist_a: dict, dist_b: dict) -> float:
    """0.0 (identical mix) to 1.0 (completely different mix) - standard TVD
    between two categorical distributions. Used to tell "the regime mix
    behind this feature's recent trades looks different from its older
    trades" apart from a coincidental score swing."""
    if not dist_a or not dist_b:
        return 0.0
    keys = set(dist_a) | set(dist_b)
    return 0.5 * sum(abs(dist_a.get(k, 0.0) - dist_b.get(k, 0.0)) for k in keys)


REGIME_SHIFT_TVD_THRESHOLD = 0.25  # >= this much mix change counts as "market regime changed"
NOISE_SWING_THRESHOLD = 0.10       # matches the STABLE/DRIFTING cutoff below - a swing this small is noise, not decay/growth


def _classify_drift_reason(patterns: list, windowed: dict, drift_label: str) -> dict:
    """Distinguishes WHY a feature's importance moved, not just THAT it
    moved - a deployment review asked for this explicitly: "MACD importance
    fell" isn't actionable on its own, but "MACD importance fell because the
    market regime changed" vs. "...because the sample is still tiny" vs.
    "...because the signal is genuinely decaying" each point to a different
    response (wait for more data, re-check post-regime-shift, or actually
    downweight the rule).

    Checked in priority order (a regime shift is the most common confound,
    checked first so it isn't misdiagnosed as decay just because the newest
    window's trades happen to skew toward a different market state):
      1. market_regime_changed - the 30-day window's regime mix differs
         (total variation distance) from the rest of the 180-day window's
         mix by more than REGIME_SHIFT_TVD_THRESHOLD.
      2. sample_size_still_small - the newest (30d) window doesn't have
         enough trades on its own to be measured (insufficient_data) or is
         a sliver of the total sample - a shift computed off a handful of
         trades isn't a real finding yet.
      3. signal_decayed / signal_strengthening - scores move consistently
         (monotonically) from the oldest to the newest window, and the swing
         exceeds NOISE_SWING_THRESHOLD - decayed if importance fell,
         strengthening if it rose.
      4. noise - windows moved but not consistently (bounced up and down),
         or the swing is small enough it could just be sampling variation.
      5. stable - drift_label was already STABLE; no further explanation
         needed."""
    if drift_label == "STABLE":
        return {"drift_reason": "stable", "detail": "Importance is consistent across all windows."}
    if drift_label == "insufficient_history":
        return {"drift_reason": "sample_size_still_small",
                "detail": "Fewer than 2 windows have enough trades to compare yet."}

    now = datetime.utcnow()
    recent_cutoff = now - timedelta(days=30)
    recent = [p for p in patterns if _parse_ts(p["recorded_at"]) >= recent_cutoff]
    older = [p for p in patterns if _parse_ts(p["recorded_at"]) < recent_cutoff]

    regime_shift = _total_variation_distance(_regime_distribution(recent), _regime_distribution(older))
    if regime_shift >= REGIME_SHIFT_TVD_THRESHOLD:
        return {
            "drift_reason": "market_regime_changed",
            "detail": f"Regime mix in the last 30 days differs from prior trades by "
                      f"{regime_shift:.2f} (total variation distance, >= {REGIME_SHIFT_TVD_THRESHOLD} threshold) - "
                      f"the importance swing may just reflect a different market backdrop, not a real signal change.",
            "regime_shift_tvd": round(regime_shift, 3),
        }

    windows_30d = windowed.get("30d", {})
    n_30d = windows_30d.get("n", 0)
    n_total = len(patterns)
    if windows_30d.get("insufficient_data") or (n_total and n_30d / n_total < 0.15):
        return {
            "drift_reason": "sample_size_still_small",
            "detail": f"Only {n_30d} trades in the most recent 30-day window "
                      f"(of {n_total} total) - too few to distinguish a real shift from noise.",
        }

    scores_oldest_to_newest = [
        windowed[f"{d}d"]["abs_importance"] for d in sorted(STABILITY_WINDOWS_DAYS, reverse=True)
        if not windowed[f"{d}d"].get("insufficient_data")
    ]
    if len(scores_oldest_to_newest) >= 2:
        diffs = [b - a for a, b in zip(scores_oldest_to_newest, scores_oldest_to_newest[1:])]
        monotonic_up = all(d >= -1e-9 for d in diffs)
        monotonic_down = all(d <= 1e-9 for d in diffs)
        total_swing = abs(scores_oldest_to_newest[-1] - scores_oldest_to_newest[0])
        if total_swing >= NOISE_SWING_THRESHOLD and monotonic_up:
            return {"drift_reason": "signal_strengthening",
                    "detail": f"Importance rose consistently from the oldest to newest window "
                              f"(+{total_swing:.2f}) - this feature may be becoming more predictive."}
        if total_swing >= NOISE_SWING_THRESHOLD and monotonic_down:
            return {"drift_reason": "signal_decayed",
                    "detail": f"Importance fell consistently from the oldest to newest window "
                              f"(-{total_swing:.2f}) - this feature may be losing predictive power."}

    return {"drift_reason": "noise",
            "detail": "Importance moved between windows but not consistently (or the swing is small) - "
                      "likely sampling variation rather than a real change."}


def feature_drift(patterns: list, feature: str) -> dict:
    """Same 30/90/180-day windowing as learning/walk_forward.py's
    feature_stability() - recomputes this feature's importance in each
    window and flags STABLE / DRIFTING / insufficient_history. A feature
    whose importance is swinging between windows is either noisy or the
    system is fitting a transient pattern rather than a durable edge.

    Also classifies WHY (see _classify_drift_reason) - market_regime_changed
    / sample_size_still_small / signal_decayed / signal_strengthening /
    noise / stable - so a DRIFTING flag is actionable, not just a flag."""
    now = datetime.utcnow()
    windowed = {}
    for days in STABILITY_WINDOWS_DAYS:
        cutoff = now - timedelta(days=days)
        subset = [p for p in patterns if _parse_ts(p["recorded_at"]) >= cutoff]
        windowed[f"{days}d"] = feature_importance(subset, feature)

    scores = [w["abs_importance"] for w in windowed.values() if not w.get("insufficient_data")]
    if len(scores) < 2:
        label = "insufficient_history"
    else:
        label = "STABLE" if (max(scores) - min(scores)) < 0.10 else "DRIFTING"

    reason = _classify_drift_reason(patterns, windowed, label)
    return {"feature": feature, "windows": windowed, "drift_label": label, **reason}


def rank_all_features(db, mode: str = "SWING", include_drift: bool = False) -> dict:
    """Main entry point - ranks every one of the 25 pattern-database
    features by |importance|, most to least predictive, gated by sample
    size same as everywhere else in this codebase's learning stack.
    include_drift=True also runs feature_drift() per feature (3x the
    pattern-DB scan per feature - off by default so a quick ranking call
    stays cheap; turn on for a periodic deeper report, e.g. from
    analytics/overfit_risk.py)."""
    patterns = db.get_patterns(mode=mode, closed_only=True)
    all_features = NUMERIC_FEATURES + CATEGORICAL_FEATURES

    results = []
    for f in all_features:
        imp = feature_importance(patterns, f)
        if include_drift and not imp.get("insufficient_data"):
            imp["drift"] = feature_drift(patterns, f)
        results.append(imp)

    ranked = sorted(
        [r for r in results if not r.get("insufficient_data")],
        key=lambda r: r["abs_importance"], reverse=True,
    )
    insufficient = [r["feature"] for r in results if r.get("insufficient_data")]

    return {
        "mode": mode, "n_patterns": len(patterns),
        "ranked": ranked, "insufficient_data_features": insufficient,
        "n_ranked": len(ranked), "n_total_features": len(all_features),
    }
