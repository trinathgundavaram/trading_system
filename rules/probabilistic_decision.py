"""Replaces the score-vs-threshold cliff (rules/dynamic_thresholds.py's
final_pct >= final_threshold) with a probabilistic decision, per Trinath's
explicit "highest priority" ask (2026-07-15): a 68% score and a 95% score
currently get treated identically once both clear the same bar - this
module makes the actual should_buy call from the pattern database's real
outcome distribution (engine/ev_engine.py) instead, whenever there's enough
history to trust it.

Two modes, always labeled so nothing pretends to be more certain than it is:
  - "probabilistic": pattern DB has >= min_required similar closed trades
    (see learning/pattern_database.py's MIN_RECENCY_COUNT_BY_FREQUENCY).
    should_buy = EV > min_ev_pct AND P(win) >= min_win_probability, both
    config-driven (config.yaml's probabilistic_decision section).
  - "score_fallback": not enough pattern-DB history yet for this exact kind
    of setup (a very common state early in this system's life, or for a
    rare setup_type/regime combo even later) - should_buy falls back to the
    existing bucket-score-vs-dynamic-threshold decision, UNCHANGED
    behavior, clearly labeled so it's obvious in the prompt/UI that this
    wasn't a probability-driven call.

This module does NOT touch hard vetoes, market gates, or the data-quality
circuit breaker - those run before rules/swing_buy_rules.py's score() is
even called and stay exactly as safety-critical as before. This only
changes HOW a ticker that reached scoring gets its final should_buy call.
"""

DEFAULT_MIN_WIN_PROBABILITY = 0.50
DEFAULT_MIN_EV_PCT = 0.0


def decide(ev_result: dict, threshold_passed: bool, final_score_pct: float,
           final_threshold_pct: float, cfg: dict) -> dict:
    """
    ev_result: engine/ev_engine.py's get_ev_for_signal() output (already
        computed by rules/swing_buy_rules.py's score() before this is
        called) - None, or ev_result["ev"] is None, means insufficient
        pattern-DB history for this setup.
    threshold_passed: the pre-existing final_score_pct >= final_threshold_pct
        boolean - kept as the should_buy value in "score_fallback" mode, and
        surfaced (not used to gate) as `threshold_would_have_passed` even in
        "probabilistic" mode so the two decision methods can be compared.
    final_score_pct / final_threshold_pct: for the fallback reason string
        only - not used in the probabilistic branch's gate logic.
    cfg: full config dict - reads cfg["probabilistic_decision"].
    """
    pcfg = (cfg or {}).get("probabilistic_decision", {}) or {}
    enabled = pcfg.get("enabled", True)
    min_win_probability = float(pcfg.get("min_win_probability", DEFAULT_MIN_WIN_PROBABILITY))
    min_ev_pct = float(pcfg.get("min_ev_pct", DEFAULT_MIN_EV_PCT))

    has_data = enabled and ev_result is not None and ev_result.get("ev") is not None

    if not has_data:
        n = (ev_result or {}).get("n", 0)
        min_required = (ev_result or {}).get("min_required")
        if enabled:
            reason = (
                f"Score-based fallback: only {n} historically similar closed trades "
                f"found (needs {min_required or '15+'}) - not enough pattern-database "
                f"history yet to trust a probability estimate for this setup. Falling "
                f"back to score {final_score_pct:.1f}% vs. dynamic threshold "
                f"{final_threshold_pct:.0f}%."
            )
        else:
            reason = "Probabilistic decisioning disabled in config.yaml - using score vs. threshold."
        return {
            "mode": "score_fallback",
            "should_buy": threshold_passed,
            "probability_of_success": None,
            "expected_value_pct": None,
            "p_target_gain": None,
            "target_gain_pct": None,
            "p_stop_loss": None,
            "stop_loss_pct": None,
            "expected_return_pct": None,
            "expected_drawdown_pct": None,
            "expected_hold_hours": None,
            "n_matches": n,
            "confidence": (ev_result or {}).get("confidence", "insufficient"),
            "headline": None,
            "reason": reason,
            "min_win_probability": min_win_probability,
            "min_ev_pct": min_ev_pct,
            "threshold_would_have_passed": threshold_passed,
        }

    p_win = ev_result["p_win"]
    ev = ev_result["ev"]
    should_buy = (ev > min_ev_pct) and (p_win >= min_win_probability)

    headline = (
        f"This trade has a {p_win * 100:.0f}% probability of success with "
        f"{ev:+.1f}% expected value."
    )
    gate_notes = []
    if not (ev > min_ev_pct):
        gate_notes.append(f"EV {ev:+.1f}% does not clear the +{min_ev_pct:.1f}% minimum")
    if not (p_win >= min_win_probability):
        gate_notes.append(f"P(win) {p_win * 100:.0f}% is below the {min_win_probability * 100:.0f}% minimum")
    reason = (
        f"{headline} Based on {ev_result['n']} historically similar closed trades "
        f"(confidence: {ev_result['confidence']})."
        + (f" BLOCKED: {'; '.join(gate_notes)}." if not should_buy else "")
    )

    return {
        "mode": "probabilistic",
        "should_buy": should_buy,
        "probability_of_success": p_win,
        "expected_value_pct": ev,
        "p_target_gain": ev_result.get("p_target_gain"),
        "target_gain_pct": ev_result.get("target_gain_pct"),
        "p_stop_loss": ev_result.get("p_stop_loss"),
        "stop_loss_pct": ev_result.get("stop_loss_pct"),
        "expected_return_pct": ev_result.get("expected_return_pct"),
        "expected_drawdown_pct": ev_result.get("expected_drawdown_pct"),
        "expected_hold_hours": ev_result.get("expected_hold_hours"),
        "n_matches": ev_result.get("n"),
        "confidence": ev_result.get("confidence"),
        "headline": headline,
        "reason": reason,
        "min_win_probability": min_win_probability,
        "min_ev_pct": min_ev_pct,
        # Comparison-only, NOT part of the gate in probabilistic mode - lets
        # the prompt/UI show "the old threshold method would have said X,
        # the probabilistic method says Y" when they disagree.
        "threshold_would_have_passed": threshold_passed,
    }
