"""Expected Value calculation from the pattern database, with a Wilson CI on
p(win) propagated through to the EV bounds - so a 12-match EV estimate and a
200-match EV estimate don't get treated with equal confidence downstream.

2026-07-15 (probabilistic-decision pass): extended beyond a single EV number
to the full outcome distribution Trinath asked for - P(>target% gain),
P(stop-loss-range loss), expected return, expected drawdown, expected
holding period - so rules/probabilistic_decision.py can replace the
score-vs-threshold cliff with "this trade has a 63% probability of success
with +2.7% expected value" instead of a flat BUY/no-BUY line.

HONESTY NOTE on p_stop_loss / expected_drawdown_pct - read before trusting
either number: these are PROXIES computed from the horizon OUTCOME (entry
price vs. price at the pattern's actual close), not from a real intraday
path. Two real facts about this codebase's data make that the honest
choice, not a shortcut:
  1. The vast majority of pattern_database rows are SIMULATED closes from
     scheduler.py's _close_due_patterns() - a fixed-hold-days snapshot
     comparing entry price to whatever price showed up N days later. No
     stop-loss is ever actually checked/triggered along the way for these
     rows, so there is no real "did a stop fire" signal to read.
  2. Even for REAL confirmed trades (confirm_fill.py), exit_reason is
     currently always "manual_fill_confirmed" or "time_based_close" - no
     code path yet writes a genuine "stop_loss"/"take_profit" exit_reason
     into pattern_database (see analytics/regret_analysis.py's own
     HONESTY NOTE for the same finding from a different angle) - so
     filtering on exit_reason would silently return zero matches, not a
     real answer.
  p_stop_loss is therefore P(outcome_pct <= -stop_loss_pct) AT THE HORIZON,
  a reasonable and honestly-labeled stand-in, but NOT proof a stop-loss
  order would have actually fired first (price could have dipped further
  intraday and recovered, or vice versa). expected_drawdown_pct reuses
  avg_loss_pct (the average horizon loss magnitude among losing outcomes)
  for the same reason - true per-pattern MAE isn't tracked for simulated
  closes, and even mae_mfe_data (real trades only) has no trade_id linkage
  back to scheduler-recorded pattern rows to join against. If real
  intraday MAE tracking is ever added to the simulated-close path, swap
  the source here rather than treating this note as permanent.
"""
from statistics import mean

from analytics.confidence_intervals import wilson_ci
from learning.pattern_database import MIN_RECENCY_COUNT_BY_FREQUENCY, PatternDatabase

CONFIDENCE_LABELS = [
    (15, "insufficient"), (30, "low"), (75, "moderate"), (float("inf"), "high"),
]

DEFAULT_TARGET_GAIN_PCT = 5.0
DEFAULT_STOP_LOSS_PCT = 5.0


def get_confidence_label(n: int) -> str:
    for max_n, label in CONFIDENCE_LABELS:
        if n < max_n:
            return label
    return "high"


def calculate_ev(similar_trades: list[dict], event_frequency: str = "COMMON",
                  target_gain_pct: float = DEFAULT_TARGET_GAIN_PCT,
                  stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT) -> dict:
    min_required = MIN_RECENCY_COUNT_BY_FREQUENCY.get(event_frequency, 15)
    n = len(similar_trades)
    if n < min_required:
        return {"ev": None, "confidence": "insufficient", "n": n, "min_required": min_required}

    outcomes = [t["outcome_pct"] for t in similar_trades if t.get("outcome_pct") is not None]
    if not outcomes:
        return {"ev": None, "confidence": "insufficient", "n": n, "min_required": min_required}

    wins = [o for o in outcomes if o > 0]
    losses = [o for o in outcomes if o <= 0]
    n_outcomes = len(outcomes)
    p_win = len(wins) / n_outcomes
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0

    ev = (p_win * avg_win) - ((1 - p_win) * avg_loss)
    ci_lower, ci_upper = wilson_ci(p_win, n_outcomes)

    # Probability of clearing target_gain_pct - a stricter bar than "any
    # win" (p_win above), matching Trinath's explicit "Probability(>5%
    # gain)" ask rather than conflating it with the win/loss split the EV
    # decomposition itself uses.
    n_target = sum(1 for o in outcomes if o >= target_gain_pct)
    p_target_gain = n_target / n_outcomes
    tg_ci = wilson_ci(p_target_gain, n_outcomes)

    # Probability(stop loss) - see module HONESTY NOTE above for exactly
    # what this does and doesn't measure.
    n_stop = sum(1 for o in outcomes if o <= -abs(stop_loss_pct))
    p_stop_loss = n_stop / n_outcomes
    sl_ci = wilson_ci(p_stop_loss, n_outcomes)

    hold_hours_vals = [t["hold_hours"] for t in similar_trades
                        if t.get("hold_hours") is not None and t.get("outcome_pct") is not None]
    expected_hold_hours = mean(hold_hours_vals) if hold_hours_vals else None

    return {
        "ev": ev,
        "ev_lower": (ci_lower * avg_win) - ((1 - ci_lower) * avg_loss),
        "ev_upper": (ci_upper * avg_win) - ((1 - ci_upper) * avg_loss),
        "p_win": p_win,
        "p_win_ci": (ci_lower, ci_upper),
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "n": n_outcomes,
        "confidence": get_confidence_label(n_outcomes),
        # --- probabilistic-decision extension (2026-07-15) ---
        "target_gain_pct": target_gain_pct,
        "p_target_gain": p_target_gain,
        "p_target_gain_ci": tg_ci,
        "stop_loss_pct": stop_loss_pct,
        "p_stop_loss": p_stop_loss,
        "p_stop_loss_ci": sl_ci,
        # expected_return_pct is mathematically identical to `ev` (both are
        # the mean of the outcome distribution, just derived via the
        # win/loss decomposition vs. a plain average - they agree by
        # construction) - exposed under this name too since the decision
        # layer/UI talks about "expected return" as its own headline stat.
        "expected_return_pct": ev,
        # PROXY, not true intraday MAE - see module HONESTY NOTE.
        "expected_drawdown_pct": avg_loss,
        "expected_hold_hours": expected_hold_hours,
    }


def get_ev_for_signal(db, signal_features: dict, ticker: str, mode: str = "SWING",
                       event_frequency: str = "COMMON", regime_filter: str = None,
                       target_gain_pct: float = DEFAULT_TARGET_GAIN_PCT,
                       stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT) -> dict:
    """Convenience wrapper: pattern DB lookup + EV calc in one call, for use from
    scheduler.py/packet_builder.py without both callers re-deriving the pattern DB."""
    pdb = PatternDatabase(db)
    similar = pdb.find_similar_trades(
        signal_features, mode=mode, event_frequency=event_frequency, regime_filter=regime_filter,
    )
    ev_result = calculate_ev(similar, event_frequency=event_frequency,
                              target_gain_pct=target_gain_pct, stop_loss_pct=stop_loss_pct)
    ev_result["pattern_confidence"] = pdb.pattern_confidence(similar)
    return ev_result
