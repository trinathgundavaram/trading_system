"""Portfolio Rotation Engine (2026-07-17, Akhil's ask: "if a high score is
available and the cap of 10 stocks is reached, is it worth analyzing the
weakest stock in the current portfolio, closing the position, and getting the
stock with the higher score?").

When the book is full (trading.max_positions) and a NEW candidate fires a
buy signal, this module decides whether one existing holding should be
rotated out to make room. It deliberately does NOT compare the candidate's
buy score against the holdings' buy scores - a buy score measures ENTRY
setup quality, which is consumed the moment you enter; a position's ongoing
quality is measured by the exit-side engines (rules/exit_scorer.py /
engine/position_health.py). Comparing a fresh candidate's entry score to a
holding's stale entry score would be apples-to-oranges and would churn the
book on score noise.

So the rotation victim is chosen by the EXIT side's own opinion:

  GUARDRAILS (all config.yaml `rotation:`, all must pass):
    1. rotation.enabled                  - master switch, default false.
    2. candidate score >= min_candidate_score (default 85) - only a
       top-tier setup may displace anything; "merely higher than something
       I hold" is not enough.
    3. victim position_health_score <= max_victim_health_score (default 55,
       i.e. deep MONITOR / REDUCE tier per position_health.py's labels) -
       we only rotate out positions our own exit engine was ALREADY souring
       on. A STRONG_HOLD/HOLD position is never sacrificed, no matter how
       shiny the candidate. Positions Loop B hasn't health-scored yet
       (position_health_score is NULL - typically brand-new fills) are
       never eligible.
    4. victim days_held >= min_hold_days (default 3) - a position gets time
       to develop before it can be rotation-eligible (anti-churn).
    5. Rotation budget: at most max_rotations_per_week (default 2) per book
       (paper/live counted separately) - persisted in the rotation_log
       table (storage/database.py), so restarts don't reset the budget.

  Among eligible victims, the LOWEST health score loses.

Called from engine/paper_trader.execute_buy (paper book) and
engine/live_trader.execute_buy_live (real book) at the exact branch that
used to unconditionally skip the buy at max positions. This module only
PICKS the victim - the caller owns the actual sell/buy execution, so every
existing execution guard (purse, buying power, kill switch, breaker) still
applies to both legs.
"""
import logging
from datetime import datetime

logger = logging.getLogger("trading")

DEFAULTS = {
    "enabled": False,
    "min_candidate_score": 85.0,
    "max_victim_health_score": 55.0,
    "min_hold_days": 3.0,
    "max_rotations_per_week": 2,
}


def _rcfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("rotation", {}) or {})
    return out


def _days_held(pos: dict) -> float:
    """days_held column when Loop B has stamped it; else derived from
    entry_time; else 0 (never eligible on min_hold_days grounds)."""
    dh = pos.get("days_held")
    if dh is not None:
        try:
            return float(dh)
        except (TypeError, ValueError):
            pass
    try:
        entry = datetime.fromisoformat(pos["entry_time"])
        return (datetime.utcnow() - entry).total_seconds() / 86400
    except (KeyError, TypeError, ValueError):
        return 0.0


def find_rotation_victim(db, cfg: dict, candidate_ticker: str,
                          candidate_score: float | None,
                          simulated: bool) -> dict | None:
    """Returns {"ticker", "health", "days_held", "reason"} for the position
    that should be rotated out to make room for candidate_ticker, or None if
    no rotation should happen. Every 'no' is logged at INFO with the guard
    that said no - 'wanted to rotate but a guard said no' is signal, same
    philosophy as the buy/sell skip logging."""
    rc = _rcfg(cfg)
    book = "PAPER" if simulated else "LIVE"

    if not rc.get("enabled"):
        return None

    if candidate_score is None:
        logger.info(f"{candidate_ticker}: [{book}] rotation skipped - "
                    f"candidate has no buy score this cycle")
        return None

    min_score = float(rc.get("min_candidate_score", 85.0))
    if float(candidate_score) < min_score:
        logger.info(f"{candidate_ticker}: [{book}] rotation skipped - score "
                    f"{float(candidate_score):.1f}% < rotation bar {min_score:.0f}%")
        return None

    max_per_week = int(rc.get("max_rotations_per_week", 2))
    recent = db.count_recent_rotations(days=7, simulated=simulated)
    if recent >= max_per_week:
        logger.info(f"{candidate_ticker}: [{book}] rotation skipped - weekly "
                    f"budget used ({recent}/{max_per_week} in last 7 days)")
        return None

    max_health = float(rc.get("max_victim_health_score", 55.0))
    min_days = float(rc.get("min_hold_days", 3.0))

    eligible = []
    for pos in db.get_all_positions(simulated=simulated):
        if pos["ticker"] == candidate_ticker:
            continue
        # SYNC (engine/account_sync.py's auto-import of real Robinhood
        # holdings) and SEED (robinhood_sync.py's seed-paper mirror) rows
        # are positions the account happens to hold, not ones this engine
        # chose to enter (2026-07-23) - never eligible as a rotation victim.
        # For the real book this matters a lot: rotation executes an actual
        # market sell on the chosen victim, so a SYNC row reaching here
        # would mean placing a real, unrequested sell order on a holding
        # the algorithm was never asked to manage.
        if str(pos.get("trade_mode") or "").upper() in ("SYNC", "SEED"):
            continue
        health = pos.get("position_health_score")
        if health is None:
            continue  # Loop B hasn't judged it yet - too young to sacrifice
        days = _days_held(pos)
        if days < min_days:
            continue
        if float(health) > max_health:
            continue
        eligible.append({"ticker": pos["ticker"], "health": float(health),
                          "days_held": days})

    if not eligible:
        logger.info(f"{candidate_ticker}: [{book}] rotation skipped - book is "
                    f"full but no holding is weak enough (need health <= "
                    f"{max_health:.0f} and >= {min_days:.0f} days held)")
        return None

    victim = min(eligible, key=lambda p: p["health"])
    victim["reason"] = (f"rotation: {victim['ticker']} health "
                        f"{victim['health']:.0f}/100 after "
                        f"{victim['days_held']:.1f}d vs {candidate_ticker} "
                        f"score {float(candidate_score):.1f}%")
    logger.info(f"{candidate_ticker}: [{book}] ROTATION - {victim['reason']} "
                f"(weekly budget {recent + 1}/{max_per_week})")
    return victim
