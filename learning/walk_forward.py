"""Walk-forward analysis: runs every 50 trades / 30 days / -10 health points.
Tests each rule's real attribution (win rate when it fired vs the baseline) and
checks whether that edge is stable across 30/90/180-day windows or just noise
from a hot streak. Produces a PROPOSAL only - applying it is a manual step
(BayesianUpdater.apply_update), matching the spec's "requires human approval
before applying weight changes."
"""
from datetime import datetime, timedelta

from analytics.confidence_intervals import wilson_ci

STABILITY_WINDOWS_DAYS = [30, 90, 180]


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.utcnow()


def rule_attribution(patterns: list[dict], rule_name: str) -> dict:
    """patterns: closed pattern_database rows (features + outcome_pct). A rule
    'fired' on a pattern if `features['rules_passed']` (a list the caller must
    populate at record time) contains rule_name - see learning/pattern_database
    usage notes for wiring this through from BuyResult.rules_passed."""
    fired = [p for p in patterns if rule_name in (p["features"].get("rules_passed") or [])]
    all_outcomes = [p["outcome_pct"] for p in patterns if p.get("outcome_pct") is not None]
    fired_outcomes = [p["outcome_pct"] for p in fired if p.get("outcome_pct") is not None]

    if not all_outcomes or not fired_outcomes:
        return {"rule_name": rule_name, "n_fired": len(fired_outcomes), "n_total": len(all_outcomes),
                "insufficient_data": True}

    overall_win_rate = sum(1 for o in all_outcomes if o > 0) / len(all_outcomes)
    fired_win_rate = sum(1 for o in fired_outcomes if o > 0) / len(fired_outcomes)
    ci = wilson_ci(fired_win_rate, len(fired_outcomes))

    return {
        "rule_name": rule_name,
        "n_fired": len(fired_outcomes), "n_total": len(all_outcomes),
        "overall_win_rate": overall_win_rate, "fired_win_rate": fired_win_rate,
        "fired_win_rate_ci": ci,
        "edge": fired_win_rate - overall_win_rate,
        "insufficient_data": len(fired_outcomes) < 20,
    }


def feature_stability(patterns: list[dict], rule_name: str) -> dict:
    """Re-runs rule_attribution() over the last 30/90/180 days separately and
    labels the edge STABLE / VOLATILE / SPIKING / SEASONAL based on how much it
    moves between windows."""
    now = datetime.utcnow()
    windowed = {}
    for days in STABILITY_WINDOWS_DAYS:
        cutoff = now - timedelta(days=days)
        subset = [p for p in patterns if _parse_ts(p["recorded_at"]) >= cutoff]
        windowed[f"{days}d"] = rule_attribution(subset, rule_name)

    edges = [w["edge"] for w in windowed.values() if not w.get("insufficient_data")]
    if len(edges) < 2:
        label = "insufficient_history"
    else:
        spread = max(edges) - min(edges)
        if spread < 0.05:
            label = "STABLE"
        elif spread < 0.15:
            label = "VOLATILE"
        elif edges[-1] < edges[0] - 0.15:
            label = "SPIKING"  # recent window much stronger than older ones
        else:
            label = "SEASONAL"

    return {"rule_name": rule_name, "windows": windowed, "stability_label": label}


def run_walk_forward(db, mode: str, rule_names: list[str]) -> dict:
    """Entry point matching the spec's 'runs every 50 trades or 30 days'
    trigger - caller decides WHEN to call this (e.g. scheduler.py checking
    trade count / last-run date), this function just does the analysis."""
    from learning.pattern_database import PatternDatabase
    pdb = PatternDatabase(db)
    patterns = pdb.db.get_patterns(mode=mode, closed_only=True)

    proposals = {}
    for rule_name in rule_names:
        attribution = rule_attribution(patterns, rule_name)
        stability = feature_stability(patterns, rule_name)
        proposals[rule_name] = {"attribution": attribution, "stability": stability}

    return {
        "run_at": datetime.utcnow().isoformat(),
        "mode": mode,
        "n_patterns": len(patterns),
        "proposals": proposals,
        "requires_human_approval": True,
        "note": "Nothing here is applied automatically - review proposals, then "
                "call BayesianUpdater.propose_update()/apply_update() per rule you approve.",
    }
