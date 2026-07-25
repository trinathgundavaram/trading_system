"""Overfitting Risk Report - a single, direct answer to "how do we validate
this / are we at risk of overfitting right now," pulling together every
safeguard already built across this codebase's learning stack into one
report instead of five separate tools someone has to remember to check:

  1. Sample size    - closed trades vs. what each downstream safeguard
                       actually requires, and vs. the review's own "a few
                       hundred trades" recommended bar before trusting any
                       weight adaptation.
  2. Feature importance - analytics/feature_importance.py's full 25-feature
                       ranking + 30/90/180-day drift detection (is a feature
                       DRIFTING, i.e. is the model's basis for trusting it
                       changing over time?).
  3. Walk-forward stability - learning/walk_forward.py's per-rule edge +
                       stability label, read from the latest automated run
                       (engine/learning_loop.py already triggers this every
                       50 closed trades / 30 days - see that module).
  4. Champion/challenger - learning/champion_challenger.py's out-of-sample
                       test history - has anything ever actually been
                       validated this way, or does live config still reflect
                       only in-sample choices?
  5. Bayesian drift budget - learning/bayesian_updater.py's weekly/monthly
                       caps utilization, and whether the shadow-validation
                       gate (ShadowValidationRequired) is even turned on.

This computes NOTHING new statistically - it's a rollup of what those five
modules already produce. Each check gets an explicit status: OK /
INSUFFICIENT_DATA / WARNING. Nothing here blocks anything automatically,
same posture as the rest of the learning stack (learning/walk_forward.py's
own "nothing here is applied automatically"). Call generate_report()
whenever you want the current answer - there's no scheduled/cached version,
since this is a diagnostic, not something that affects live decisions.
"""
from datetime import datetime

from analytics.feature_importance import rank_all_features

REVIEW_RECOMMENDED_MIN_TRADES = 200  # "a few hundred trades" - the review's own bar
MIN_TRADES_FOR_PACE_ESTIMATE = 5     # below this, a pace estimate is more noise than signal


def _parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError, AttributeError):
        return None


def _estimate_trade_pace(patterns: list) -> dict | None:
    """Observed closed-trades-per-day, estimated from the spread of
    pattern_database.recorded_at timestamps (when each trade was OPENED,
    not closed - there's no closed_at column, but open-time spacing is a
    reasonable proxy for trade cadence since patterns are recorded once per
    trade regardless of hold length). Returns None when there aren't enough
    trades yet, or they all landed on the same day (zero span - can't
    estimate a rate from a single point in time).

    This directly answers the review's own math: "~11 tickers * 4 trades/
    month = ~44 trades/month, so 200 trades is 4-5 months out" - except
    computed from this system's REAL trade history instead of an assumed
    round number, so the ETA tightens (or lengthens) as actual data comes in."""
    n = len(patterns)
    if n < MIN_TRADES_FOR_PACE_ESTIMATE:
        return None
    timestamps = sorted(t for t in (_parse_ts(p.get("recorded_at")) for p in patterns) if t is not None)
    if len(timestamps) < MIN_TRADES_FOR_PACE_ESTIMATE:
        return None
    span_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return None
    pace = len(timestamps) / span_days
    return {
        "trades_per_day": round(pace, 3), "trades_per_month": round(pace * 30, 1),
        "observed_span_days": round(span_days, 1), "n_trades_in_estimate": len(timestamps),
    }


def _eta_days(current_n: int, target_n: int, pace: dict | None) -> dict:
    """Days (and a human ETA date) until current_n reaches target_n at the
    observed pace. remaining<=0 means the target is already met. pace=None
    (not enough history yet to estimate) reports eta_days=None rather than
    guessing - same "insufficient data, don't fake a number" posture as the
    rest of this module."""
    remaining = target_n - current_n
    if remaining <= 0:
        return {"target": target_n, "remaining_trades": 0, "eta_days": 0, "eta_date": None,
                "note": "Target already met."}
    if pace is None or pace["trades_per_day"] <= 0:
        return {"target": target_n, "remaining_trades": remaining, "eta_days": None, "eta_date": None,
                "note": "Not enough trade history yet to estimate a pace."}
    days = remaining / pace["trades_per_day"]
    eta_date = None
    try:
        from datetime import timedelta
        eta_date = (datetime.utcnow() + timedelta(days=days)).date().isoformat()
    except OverflowError:
        eta_date = None
    return {
        "target": target_n, "remaining_trades": remaining, "eta_days": round(days, 0), "eta_date": eta_date,
        "note": f"At the observed pace of {pace['trades_per_month']} trades/month, "
                f"~{round(days, 0):.0f} days to reach {target_n} closed trades.",
    }


def _sample_size_check(db, cfg: dict, mode: str) -> dict:
    patterns = db.get_patterns(mode=mode, closed_only=True)
    n = len(patterns)
    learn_cfg = cfg.get("learning", {})
    thresholds = {
        "min_trades_before_bayesian": learn_cfg.get("min_trades_before_bayesian", 10),
        "champion_challenger_min_trades_for_significance":
            learn_cfg.get("champion_challenger_min_trades_for_significance", 30),
        "regime_weight_adaptation_min_closed_trades":
            cfg.get("regime_weight_adaptation", {}).get("min_closed_trades_required", 200),
    }
    meets_review_bar = n >= REVIEW_RECOMMENDED_MIN_TRADES
    if n < thresholds["min_trades_before_bayesian"]:
        status = "INSUFFICIENT_DATA"
    elif not meets_review_bar:
        status = "WARNING"
    else:
        status = "OK"

    pace = _estimate_trade_pace(patterns)
    eta = {
        "champion_challenger_min_trades_for_significance":
            _eta_days(n, thresholds["champion_challenger_min_trades_for_significance"], pace),
        "review_recommended_minimum": _eta_days(n, REVIEW_RECOMMENDED_MIN_TRADES, pace),
        "regime_weight_adaptation_min_closed_trades":
            _eta_days(n, thresholds["regime_weight_adaptation_min_closed_trades"], pace),
    }
    return {
        "n_closed_trades": n, "thresholds": thresholds,
        "meets_review_recommended_minimum": meets_review_bar,
        "status": status,
        "note": f"{n} closed trades vs. the review's own recommended {REVIEW_RECOMMENDED_MIN_TRADES}+ "
                f"trade bar before automating any weight adaptation.",
        "observed_pace": pace,
        "eta": eta,
    }


def _feature_importance_check(db, mode: str) -> dict:
    ranking = rank_all_features(db, mode=mode, include_drift=True)
    drifting = [r for r in ranking["ranked"] if r.get("drift", {}).get("drift_label") == "DRIFTING"]
    if ranking["n_ranked"] == 0:
        status = "INSUFFICIENT_DATA"
    elif drifting:
        status = "WARNING"
    else:
        status = "OK"
    return {
        "n_ranked": ranking["n_ranked"], "n_total_features": ranking["n_total_features"],
        "top_5_by_importance": ranking["ranked"][:5],
        # feature + WHY it drifted (market_regime_changed / sample_size_still_small /
        # signal_decayed / signal_strengthening / noise) - not just the name, per
        # the review's request to make this actionable rather than a bare flag.
        "drifting_features": [
            {"feature": r["feature"], "drift_reason": r["drift"].get("drift_reason"),
             "detail": r["drift"].get("detail")}
            for r in drifting
        ],
        "status": status,
    }


def _walk_forward_check(db, mode: str) -> dict:
    last_run = db.get_last_learning_run(mode=mode)
    if not last_run:
        return {"status": "INSUFFICIENT_DATA", "note": "Walk-forward analysis has never run yet for this "
                                                          "mode (engine/learning_loop.py triggers it every "
                                                          "50 closed trades or 30 days)."}
    proposals = last_run.get("proposals", {}) or {}
    stable = [
        name for name, p in proposals.items()
        if p.get("stability", {}).get("stability_label") == "STABLE"
        and not p.get("attribution", {}).get("insufficient_data", True)
    ]
    unstable = [
        name for name, p in proposals.items()
        if p.get("stability", {}).get("stability_label") in ("VOLATILE", "SPIKING", "SEASONAL")
    ]
    if not proposals:
        status = "INSUFFICIENT_DATA"
    elif unstable:
        status = "WARNING"
    else:
        status = "OK" if stable else "INSUFFICIENT_DATA"
    return {
        "last_run_at": last_run.get("run_at"), "n_patterns_at_last_run": last_run.get("n_patterns"),
        "n_rules_tracked": len(proposals), "n_stable_edges": len(stable),
        "n_volatile_or_spiking_or_seasonal": len(unstable), "unstable_rules": unstable,
        "status": status,
    }


def _champion_challenger_check(db) -> dict:
    active = db.get_active_challenges()
    all_challenges = db.get_all_challenges(limit=50)
    promoted = [c for c in all_challenges if c["status"] == "promoted"]
    status = "OK" if all_challenges else "INSUFFICIENT_DATA"
    note = (
        "No champion/challenger test has ever been started - every current weight in config.yaml "
        "reflects an in-sample/manual choice, not an out-of-sample-validated one."
        if not all_challenges else
        f"{len(active)} running, {len(promoted)} promoted historically out of {len(all_challenges)} ever run."
    )
    return {
        "n_active": len(active), "n_total_ever_run": len(all_challenges), "n_promoted": len(promoted),
        "status": status, "note": note,
    }


def _bayesian_drift_budget_check(db, cfg: dict) -> dict:
    from learning.bayesian_updater import _week_start, _month_start
    learn_cfg = cfg.get("learning", {})
    weekly_cap = learn_cfg.get("bayesian_weekly_max_total_pct", 10)
    monthly_cap = learn_cfg.get("bayesian_monthly_max_total_pct", 25)
    weekly_used = db.get_weekly_bayesian_change(_week_start())
    monthly_used = db.get_monthly_bayesian_change(_month_start())
    weekly_util = (weekly_used / weekly_cap * 100) if weekly_cap else 0.0
    monthly_util = (monthly_used / monthly_cap * 100) if monthly_cap else 0.0
    shadow_required = learn_cfg.get("require_shadow_validation", True)

    if not shadow_required:
        status = "WARNING"
        note = "learning.require_shadow_validation is OFF - Bayesian weight changes can reach live " \
               "config without ever passing an out-of-sample champion/challenger test."
    elif max(weekly_util, monthly_util) >= 80:
        status = "WARNING"
        note = "Drift budget is close to its cap - a lot of weight movement recently relative to " \
               "the configured ceiling."
    else:
        status = "OK"
        note = "Shadow validation is required, and drift budget utilization is within normal range."
    return {
        "weekly_used_pct": round(weekly_used, 2), "weekly_cap_pct": weekly_cap,
        "weekly_budget_utilization_pct": round(weekly_util, 1),
        "monthly_used_pct": round(monthly_used, 2), "monthly_cap_pct": monthly_cap,
        "monthly_budget_utilization_pct": round(monthly_util, 1),
        "shadow_validation_required": shadow_required,
        "status": status, "note": note,
    }


def _summarize(overall: str, checks: dict) -> str:
    if overall == "INSUFFICIENT_DATA":
        return (
            "Not enough live trade history yet for most of these checks to say much either way - "
            "every safeguard here is designed to report INSUFFICIENT_DATA rather than a false OK on "
            "a small sample. This is expected for a system with limited trade history; re-run this "
            "report as trades accumulate rather than treating a clean-looking early report as validation."
        )
    if overall == "WARNING":
        flagged = [name for name, c in checks.items() if c["status"] == "WARNING"]
        return f"Flagged: {', '.join(flagged)}. Review these before trusting current weights/thresholds fully."
    return (
        "All tracked safeguards currently report OK. This is not a one-time clearance - overfitting "
        "risk should be re-checked periodically as trade volume and weight changes accumulate, not "
        "treated as a permanent green light."
    )


def generate_report(db, cfg: dict, mode: str = "SWING") -> dict:
    """The main entry point. mode: "SWING" or "DAY" - matches
    pattern_database.mode/learning_runs.mode elsewhere in this codebase."""
    checks = {
        "sample_size": _sample_size_check(db, cfg, mode),
        "feature_importance": _feature_importance_check(db, mode),
        "walk_forward_stability": _walk_forward_check(db, mode),
        "champion_challenger": _champion_challenger_check(db),
        "bayesian_drift_budget": _bayesian_drift_budget_check(db, cfg),
    }
    statuses = [c["status"] for c in checks.values()]
    if "WARNING" in statuses:
        overall = "WARNING"
    elif all(s == "OK" for s in statuses):
        overall = "OK"
    else:
        overall = "INSUFFICIENT_DATA"

    return {
        "generated_at": datetime.utcnow().isoformat(), "mode": mode,
        "overall_status": overall, "checks": checks,
        "summary": _summarize(overall, checks),
    }
