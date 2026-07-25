"""Automates the learning loop that learning/walk_forward.py's own docstring
explicitly leaves to the caller ("caller decides WHEN to call this"). Before
this module, running walk-forward analysis or checking on an active
champion/challenger test required opening a Python shell and calling these
functions by hand - nothing in scheduler.py ever triggered them.

Call maybe_run() once per scheduler.py cycle. It's a cheap no-op almost every
call - it only does real work when the trigger condition from config.yaml is
met: every `learning.walk_forward_trigger_trades` newly-closed patterns, or
every `learning.walk_forward_trigger_days` days, whichever comes first.
Results are persisted to the new `learning_runs` table (storage/database.py)
so the UI's Learning tab has something to show without a manual Python-shell
call.

Does NOT modify learning/*.py or analytics/*.py (left alone per instruction -
already working, already tested). This module only calls their public
functions and decides WHEN to call them; all the actual statistics (Wilson
CI, two-proportion z-test, 30/90/180-day stability windows, bucket-weight
bounds/drift caps) live where they always did.

2026-07-23 addition: maybe_run_threshold_regret() below is the same pattern
applied to analytics/missed_opportunity.py's evaluate_threshold_regret() -
call it once per scheduler.py cycle alongside maybe_run(); it's a weekly,
time-triggered (not trade-count-triggered - it evaluates HOLD signals, not
closed trades), cheap-cached evaluation of whether the dynamic threshold is
rejecting setups that would have worked.

HONESTY NOTE: this does NOT auto-generate OR auto-apply Bayesian weight-change
proposals (learning/bayesian_updater.py's propose_update()/
apply_bucket_weight_to_config()). As of the Priority 3 config-driven-weights
pass, the 7 swing-buy / 6 exit-score bucket weights ARE now read live from
config.yaml's `weights` section (rules/swing_buy_rules.py's _bucket_weight(),
rules/exit_scorer.py's _exit_bucket_weight()) - so a proposal now has a real
current_weight to target, and learning/bayesian_updater.py's
get_current_bucket_weight()/apply_bucket_weight_to_config() can read/write
it. What this module still does NOT do is call propose_update() or
apply_bucket_weight_to_config() itself - generating and applying a weight
change is a deliberate, human-reviewed action (same "never silently
overwrite" posture as champion/challenger promotion), not something the
per-cycle automation trigger below does on its own. What IS wired here:
walk-forward rule attribution/stability (tells you WHICH rules have a real
edge) and champion/challenger re-evaluation (tells you whether a manually
started challenger config beat the champion) - both genuinely automated,
neither auto-applies anything (matching walk_forward.py's own "nothing here
is applied automatically" design).
"""
import logging
from datetime import datetime

from learning.champion_challenger import ChampionChallenger
from learning.walk_forward import run_walk_forward

logger = logging.getLogger(__name__)


def maybe_run(db, cfg: dict, mode: str = "SWING") -> dict | None:
    """Returns the learning_runs row dict if a run happened this call, else
    None (trigger condition not met yet)."""
    learn_cfg = cfg.get("learning", {})
    trigger_trades = learn_cfg.get("walk_forward_trigger_trades", 50)
    trigger_days = learn_cfg.get("walk_forward_trigger_days", 30)

    last_run = db.get_last_learning_run(mode=mode)
    current_closed = db.get_patterns(mode=mode, closed_only=True)
    current_n = len(current_closed)

    reason = _check_trigger(last_run, current_n, trigger_trades, trigger_days)
    if reason is None:
        return None

    logger.info(f"Learning loop triggered ({mode}): {reason}")

    rule_names = _distinct_rule_names(current_closed)
    if rule_names:
        wf_result = run_walk_forward(db, mode, rule_names)
    else:
        wf_result = {
            "run_at": datetime.utcnow().isoformat(), "mode": mode, "n_patterns": current_n,
            "proposals": {}, "requires_human_approval": True,
            "note": "No rules_passed data found in closed patterns yet - nothing to attribute.",
        }

    challenges_evaluated = _evaluate_active_challenges(db, cfg)

    db.log_learning_run(reason, mode, current_n, wf_result, challenges_evaluated)

    n_stable = sum(
        1 for p in wf_result.get("proposals", {}).values()
        if p.get("stability", {}).get("stability_label") == "STABLE"
        and not p.get("attribution", {}).get("insufficient_data", True)
    )
    logger.info(
        f"Learning loop run complete ({mode}): {len(rule_names)} rules attributed "
        f"({n_stable} with a STABLE edge and enough data), "
        f"{len(challenges_evaluated)} active challenge(s) evaluated"
    )
    return db.get_last_learning_run(mode=mode)


def _check_trigger(last_run, current_n: int, trigger_trades: int, trigger_days: int) -> str | None:
    if last_run is None:
        return "first run for this mode - no prior learning_runs row"

    trades_since = current_n - (last_run.get("n_patterns") or 0)
    try:
        last_run_at = datetime.fromisoformat(last_run["run_at"])
        days_since = (datetime.utcnow() - last_run_at).total_seconds() / 86400
    except (ValueError, TypeError, KeyError):
        days_since = trigger_days  # can't parse the timestamp - force a run rather than getting stuck forever

    if trades_since >= trigger_trades:
        return f"{trades_since} closed trades since last run >= {trigger_trades} threshold"
    if days_since >= trigger_days:
        return f"{days_since:.1f} days since last run >= {trigger_days} threshold"
    return None


def _distinct_rule_names(closed_patterns: list) -> list:
    """Discovers which rule/sub-rule tags actually appear in rules_passed
    across the closed patterns, rather than hardcoding a rule-name list here.
    Some tags bake in the triggering value (e.g. "rsi_oversold_38",
    "ad_ratio_0.82" - see rules/swing_buy_rules.py) and will rarely repeat
    exactly; walk_forward.rule_attribution() already handles that correctly
    via its own `insufficient_data` flag (< 20 fired occurrences), so this
    function doesn't try to normalize them away - that would require an
    exact-string match against learning/walk_forward.py's `in` check, which
    this module isn't allowed to modify."""
    names = set()
    for p in closed_patterns:
        for r in (p.get("features", {}).get("rules_passed") or []):
            names.add(r)
    return sorted(names)


def maybe_run_threshold_regret(db, cfg: dict) -> dict | None:
    """2026-07-23 addition (OXY dynamic-threshold review, Trinath: "build a
    process for that evaluation and learning so such false negatives are
    identified and at same time potential stocks are not missed"). Mirrors
    maybe_run()'s exact cheap-no-op-most-cycles shape above, but the trigger
    is time-only (learning.threshold_regret_trigger_days, default 7 - weekly,
    same cadence as walk-forward) rather than a trade count: this evaluates
    HOLD signals, not closed trades, so there's no "n new trades" signal to
    key off of the way maybe_run() does.

    Calls analytics/missed_opportunity.py's evaluate_threshold_regret(),
    which is itself cheap on repeat calls (per-signal yfinance results are
    cached in missed_opportunity_outcomes - a weekly re-run only simulates
    signals that are new or were still_pending last time), and persists the
    full bucketed snapshot to threshold_regret_runs so the picture over time
    is visible, not just the latest read.

    Returns the threshold_regret_runs row dict if a run happened this call,
    else None (trigger condition not met yet). Never raises to the caller -
    same "learning-loop-bg thread logs and moves on" posture as
    scheduler.py's other background automations; a bad HOLD signal, a
    yfinance outage, etc. shouldn't take down the scan cycle."""
    from analytics.missed_opportunity import evaluate_threshold_regret

    learn_cfg = cfg.get("learning", {})
    trigger_days = learn_cfg.get("threshold_regret_trigger_days", 7)

    last_run = db.get_last_threshold_regret_run()
    reason = _check_time_trigger(last_run, trigger_days, ref_key="run_at",
                                  first_run_note="no prior threshold_regret_runs row")
    if reason is None:
        return None

    logger.info(f"Threshold-regret evaluation triggered: {reason}")
    try:
        report = evaluate_threshold_regret(db, cfg=cfg)
    except Exception as e:
        logger.error(f"Threshold-regret evaluation failed: {e}", exc_info=True)
        return None

    db.log_threshold_regret_run(reason, report)
    logger.info(
        f"Threshold-regret evaluation complete: {report['n_evaluated']}/{report['n_signals']} HOLD signals "
        f"evaluated ({report['n_still_pending']} still pending). Overall avg would-have-returned "
        f"{(report['overall'].get('avg_would_have_returned_pct') if report['overall'].get('n') else 'n/a')}"
    )
    return db.get_last_threshold_regret_run()


def _check_time_trigger(last_run, trigger_days: int, ref_key: str, first_run_note: str) -> str | None:
    """Shared time-only trigger check (extracted from _check_trigger's
    days_since branch) - used by maybe_run_threshold_regret above, which has
    no trade-count signal to check the way maybe_run()'s trigger does."""
    if last_run is None:
        return f"first run - {first_run_note}"
    try:
        last_at = datetime.fromisoformat(last_run[ref_key])
        days_since = (datetime.utcnow() - last_at).total_seconds() / 86400
    except (ValueError, TypeError, KeyError):
        return "couldn't parse last run's timestamp - forcing a run"
    if days_since >= trigger_days:
        return f"{days_since:.1f} days since last run >= {trigger_days} threshold"
    return None


def _evaluate_active_challenges(db, cfg: dict) -> list:
    """Re-evaluates every champion_challenger row still in 'running' status.
    If no challenge has ever been started (the common case - starting one is
    a deliberate manual action via ChampionChallenger.start_challenge(), not
    something this module does on its own), this is just an empty list."""
    cc = ChampionChallenger(db, cfg)
    results = []
    for challenge in db.get_active_challenges():
        try:
            result = cc.evaluate(challenge["id"])
            result["challenge_id"] = challenge["id"]
            results.append(result)
            if result.get("ready"):
                logger.info(
                    f"Challenge {challenge['id'][:8]}: {result['recommendation']} "
                    f"(champion {result['champion_win_rate']:.0%} vs "
                    f"challenger {result['challenger_win_rate']:.0%})"
                )
        except Exception as e:
            logger.error(f"Champion/challenger evaluation failed for {challenge['id']}: {e}", exc_info=True)
    return results
