"""Bayesian weight updates for individual buy-rule weights, gated by statistical
significance thresholds and hard drift caps so the rules engine can't over-fit
to a short streak of trades. Every gate here blocks the update outright and
logs why - it never silently applies a smaller version of a blocked change.

As of the Priority 3 config-driven-weights pass (rules/swing_buy_rules.py's
_bucket_weight() / rules/exit_scorer.py's _exit_bucket_weight()), bucket
weights are read from config.yaml's `weights` section at scoring time
instead of being hardcoded literals - so get_current_bucket_weight() below
returns a REAL, live weight for propose_update()'s current_weight argument,
and apply_bucket_weight_to_config() can actually change what the rules
engine uses next cycle. Applying a proposal to config.yaml is STILL a
separate, deliberate call from propose_update()/apply_update() (which only
computes the proposal and writes the audit-trail row) - "never silently
overwrite" applies here too.

OVERFITTING GUARDRAIL (added after a review flagged that a system with this
many free parameters needs an enforced out-of-sample check, not just an
available-but-optional one): the SAFE end-to-end path is now
    propose_update() -> propose_as_challenge() -> [trades accumulate] ->
    ChampionChallenger.evaluate() -> apply_challenge_promoted_weight()
apply_bucket_weight_to_config() called directly (skipping the challenge)
now refuses by default - see ShadowValidationRequired and
config.yaml's learning.require_shadow_validation. force=True still exists
as an explicit, logged escape hatch, the same "never a silent default"
pattern scheduler.py's run_cycle(force=True) uses."""
import hashlib
import uuid
from datetime import datetime, timedelta

import yaml

from config_loader import CONFIG_PATH

_MODE_TO_WEIGHTS_KEY = {"swing": "swing_buy", "day": "day_trade"}

MIN_OCCURRENCES_BY_FREQUENCY = {
    "VERY_COMMON": 30, "COMMON": 30, "MODERATE": 40, "RARE": 60, "VERY_RARE": 100,
}
MAJOR_CHANGE_THRESHOLD_MULTIPLIER = 2.5  # e.g. 30 -> 75 occurrences required for a "major" change

BUCKET_WEIGHT_BOUNDS = {
    "swing": {
        "TREND": (15, 35), "MOMENTUM": (15, 32), "VOLUME_PA": (10, 25),
        "EXTERNAL": (8, 22), "SENTIMENT_MACRO": (8, 22), "MARKET_BREADTH": (6, 18),
        # 2026-07-15: VOLATILITY_EXPANSION no longer carries composite weight
        # at all - it's a pure additive bonus (up to VOL_EXP_BONUS_MAX_PTS pts,
        # see rules/swing_buy_rules.py). Bounds pinned to (0, 0) so a Bayesian
        # proposal can never resurrect it as a weighted bucket.
        "VOLATILITY_EXPANSION": (0, 0),
    },
    "day": {
        "ORB_VWAP": (25, 45), "INTRADAY_MOMENTUM": (15, 32), "RELATIVE_STRENGTH": (12, 28),
        "CATALYST": (8, 22), "PRICE_ACTION": (2, 8), "MARKET_BREADTH": (2, 6),
    },
}


def _week_start(d: datetime = None) -> str:
    d = d or datetime.utcnow()
    monday = d - timedelta(days=d.weekday())
    return monday.date().isoformat()


def _month_start(d: datetime = None) -> str:
    d = d or datetime.utcnow()
    return d.replace(day=1).date().isoformat()


class BayesianUpdater:
    def __init__(self, db, cfg: dict):
        self.db = db
        self.cfg = cfg

    def _recent_loss_streak(self, limit: int = 10) -> int:
        trades = self.db.get_recent_trades(limit)
        streak = 0
        for t in trades:  # most recent first
            # `trades` here are order fills, not closed P&L rows - callers should
            # pass a closed-trade P&L source if they have one; this is a
            # best-effort placeholder using fill status as a stand-in.
            if t.get("status") == "placed":
                break
            streak += 1
        return streak

    def propose_update(self, rule_name: str, bucket: str, current_weight: float,
                        occurrences: int, win_rate_when_fired: float, overall_win_rate: float,
                        frequency_class: str = "COMMON", mode: str = "swing") -> dict:
        """Computes a proposed new weight and every gate's pass/fail, but does
        NOT write anything - call apply_update() separately once you're happy
        with the proposal (mirrors the spec's 'never silently overwrite')."""
        cfg = self.cfg["learning"]
        min_trades = cfg["min_trades_before_bayesian"]

        if occurrences < min_trades:
            return self._blocked(rule_name, bucket, current_weight,
                                  f"only {occurrences} trades, need {min_trades} minimum")

        min_occ = MIN_OCCURRENCES_BY_FREQUENCY.get(frequency_class, 30)
        edge = win_rate_when_fired - overall_win_rate
        is_major_change = abs(edge) > 0.15  # >15pp edge counts as a "major" shift
        required_occ = min_occ * (MAJOR_CHANGE_THRESHOLD_MULTIPLIER if is_major_change else 1)
        if occurrences < required_occ:
            return self._blocked(rule_name, bucket, current_weight,
                                  f"{occurrences} occurrences < {required_occ:.0f} required "
                                  f"for a {'major' if is_major_change else 'normal'} change ({frequency_class})")

        loss_streak_limit = cfg["loss_streak_halt"]
        streak = self._recent_loss_streak()
        if streak >= loss_streak_limit:
            return self._blocked(rule_name, bucket, current_weight,
                                  f"{streak} consecutive losses >= halt threshold {loss_streak_limit}")

        learning_rate = cfg["bayesian_learning_rate"]
        raw_change = edge * learning_rate * current_weight
        max_change = current_weight * (cfg["bayesian_max_change_per_trade_pct"] / 100)
        change = max(-max_change, min(max_change, raw_change))
        proposed_weight = current_weight + change
        change_pct = abs(change / current_weight * 100) if current_weight else 0.0

        # Weekly / monthly drift caps (across ALL rules combined)
        week_key = _week_start()
        month_key = _month_start()
        weekly_used = self.db.get_weekly_bayesian_change(week_key)
        monthly_used = self.db.get_monthly_bayesian_change(month_key)
        weekly_cap = cfg["bayesian_weekly_max_total_pct"]
        monthly_cap = cfg["bayesian_monthly_max_total_pct"]

        if weekly_used + change_pct > weekly_cap:
            return self._blocked(rule_name, bucket, current_weight,
                                  f"would push weekly drift to {weekly_used + change_pct:.1f}% > {weekly_cap}% cap")
        if monthly_used + change_pct > monthly_cap:
            return self._blocked(rule_name, bucket, current_weight,
                                  f"would push monthly drift to {monthly_used + change_pct:.1f}% > {monthly_cap}% cap")

        # Bucket weight bounds
        bounds = BUCKET_WEIGHT_BOUNDS.get(mode, {}).get(bucket)
        if bounds:
            lo, hi = bounds
            if not (lo <= proposed_weight <= hi):
                proposed_weight = max(lo, min(hi, proposed_weight))
                change_pct = abs((proposed_weight - current_weight) / current_weight * 100) if current_weight else 0

        return {
            "rule_name": rule_name, "bucket": bucket, "old_weight": current_weight,
            "new_weight": proposed_weight, "change_pct": change_pct, "occurrences": occurrences,
            "win_rate_when_fired": win_rate_when_fired, "overall_win_rate": overall_win_rate,
            "blocked": False, "block_reason": None,
            "week_key": week_key, "month_key": month_key,
        }

    def apply_update(self, proposal: dict):
        """Persists an update returned by propose_update() (only call this on
        non-blocked proposals - it will happily log a blocked one too if you
        want an audit trail of what was rejected)."""
        self.db.log_bayesian_update(
            proposal["rule_name"], proposal["bucket"], proposal["old_weight"], proposal["new_weight"],
            proposal["occurrences"], proposal["win_rate_when_fired"], proposal["overall_win_rate"],
            applied=not proposal["blocked"], block_reason=proposal.get("block_reason"),
        )
        if not proposal["blocked"]:
            self.db.add_weekly_bayesian_change(proposal["week_key"], proposal["change_pct"])
            self.db.add_monthly_bayesian_change(proposal["month_key"], proposal["change_pct"])

    def _blocked(self, rule_name, bucket, current_weight, reason) -> dict:
        return {
            "rule_name": rule_name, "bucket": bucket, "old_weight": current_weight,
            "new_weight": current_weight, "change_pct": 0.0, "occurrences": None,
            "win_rate_when_fired": None, "overall_win_rate": None,
            "blocked": True, "block_reason": reason,
            "week_key": _week_start(), "month_key": _month_start(),
        }


def get_current_bucket_weight(cfg: dict, bucket: str, mode: str = "swing") -> float:
    """Reads the LIVE bucket weight straight from config.yaml's `weights`
    section (via the already-loaded cfg dict) - use this to fill in
    propose_update()'s current_weight argument so a proposal is always
    computed against what the rules engine is actually using right now, not
    a stale/remembered number.

    UNIT NOTE: config.yaml stores bucket weights as fractions (0-1, e.g.
    0.21 - that's what rules/swing_buy_rules.py's BucketScore.weight
    multiplies directly against points/max_points). BUCKET_WEIGHT_BOUNDS
    above, and this class's propose_update()/_blocked() math, were written
    against a 0-100 scale (e.g. TREND bounds are (15, 35), matching a 21
    literal, not 0.21). This function returns the 0-100 form so it drops
    straight into propose_update()'s current_weight without a silent
    off-by-100x bug - see apply_bucket_weight_to_config() for the inverse
    conversion on the way back into config.yaml."""
    weights_key = _MODE_TO_WEIGHTS_KEY.get(mode, "swing_buy")
    fraction = float(cfg.get("weights", {}).get(weights_key, {}).get("bucket_weights", {}).get(bucket, 0.0))
    return fraction * 100.0


def _config_hash() -> str:
    """sha256[:16] of config.yaml's current on-disk bytes - a compact,
    reproducible fingerprint for weight_change_log ("git commit for trading
    logic") so 'what did config.yaml look like when this decision was made'
    is answerable exactly. Hashes the FILE on disk, not an in-memory dict -
    dict key ordering/float formatting isn't guaranteed stable across a
    read-modify-write round trip (see the YAML float-reformatting quirk
    noted elsewhere in this codebase), so the file is the real source of
    truth here."""
    with open(CONFIG_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _record_provenance(db, bucket: str, mode: str, old_weight: float, new_weight: float,
                        decision: str, decision_reason: str, challenge_result: dict = None,
                        strategy_version=None, feature_ranking=None, walk_forward_report=None,
                        trade_count: int = None, config_hash: str = None):
    """Writes one row to storage/database.py's weight_change_log table - the
    permanent record a deployment review asked for so "why did we change
    TREND from 21% to 23%?" is answerable six months later without relying
    on memory. Called from apply_bucket_weight_to_config() on EVERY decision
    path - accepted, forced, AND rejected (a blocked attempt is provenance
    too, not just successful writes). db=None (old callers not yet updated
    to pass it) is a silent no-op; any logging exception is swallowed rather
    than raised, since a provenance-logging failure should never block or
    crash the actual weight decision it's describing."""
    if db is None:
        return
    row_id = f"{bucket}-{mode}-{datetime.utcnow().isoformat()}-{uuid.uuid4().hex[:6]}"
    try:
        db.log_weight_change_provenance(
            id=row_id, bucket=bucket, mode=mode, old_weight=old_weight, new_weight=new_weight,
            strategy_version=strategy_version, config_hash=config_hash or _config_hash(),
            feature_ranking=feature_ranking, walk_forward_report=walk_forward_report,
            champion_challenge_id=(challenge_result.get("challenge_id") if challenge_result else None),
            trade_count=trade_count, decision=decision, decision_reason=decision_reason,
        )
    except Exception:
        pass


class ShadowValidationRequired(Exception):
    """Raised by apply_bucket_weight_to_config() when
    config.yaml's learning.require_shadow_validation is True (the default)
    and the caller didn't supply proof the change already survived an
    out-of-sample champion/challenger test. This is the overfitting
    guardrail a deployment review flagged as missing: every OTHER gate in
    this file (min trades, occurrence thresholds, loss-streak halt, drift
    caps, bucket bounds) governs whether a change is PROPOSED - none of them
    required the change to actually prove itself out-of-sample before going
    live. See propose_as_challenge() for the one-call way to start that
    out-of-sample test, and apply_challenge_promoted_weight() for applying
    its result once it's ready."""
    pass


def apply_bucket_weight_to_config(bucket: str, new_weight_0_100: float, mode: str = "swing",
                                   cfg: dict = None, challenge_result: dict = None, force: bool = False,
                                   db=None, strategy_version=None, feature_ranking=None,
                                   walk_forward_report=None, trade_count: int = None) -> float:
    """Writes a single bucket weight into config.yaml's weights section,
    persisted to disk (same read-modify-write pattern
    rules/risk_rules.py's trip_kill_switch_if_needed() already uses for
    config.yaml). Deliberately NOT called automatically by propose_update()/
    apply_update() - this is the manual "I reviewed this proposal and want
    to apply it" step, matching learning/champion_challenger.py's promote()/
    discard() being manual too. Only changes the ONE bucket named - does not
    renormalize the other buckets' weights, since silently changing buckets
    nobody asked to change would be its own kind of silent overwrite.

    SHADOW-VALIDATION GATE: if cfg is given and
    cfg["learning"].get("require_shadow_validation", True) is truthy (the
    default), this refuses to write unless EITHER (a) challenge_result is a
    learning/champion_challenger.py ChampionChallenger.evaluate() result with
    ready=True and recommendation="promote_challenger" (i.e. this exact
    change already beat the champion out-of-sample, with statistical
    significance - see that module), or (b) force=True is passed explicitly,
    which still WRITES but is loud about bypassing the gate (logged, not
    silent) - the same "escape hatch requires an explicit flag, never a
    default" pattern as scheduler.py's run_cycle(force=True). Passing
    cfg=None skips the gate entirely (old behavior) for backward
    compatibility with any existing caller that hasn't been updated to pass
    cfg - new callers should always pass it.

    new_weight_0_100: the SAME 0-100-scale value propose_update() returns as
    "new_weight" (see get_current_bucket_weight()'s unit note) - converted
    back to the 0-1 fraction config.yaml/BucketScore.weight actually use
    before writing. Returns the 0-100 value that was passed in (unchanged,
    for caller convenience/logging - not what was literally written to disk).

    PROVENANCE: if db is given, every decision path (rejected by the shadow
    gate, forced, or accepted via a validated challenge) writes one row to
    weight_change_log via _record_provenance() - including the rejection,
    so a blocked attempt is on record too, not just successful writes. db is
    optional/keyword-only in effect (old callers that haven't been updated
    to pass it just silently skip provenance logging, same backward-compat
    posture as cfg=None skipping the shadow gate)."""
    old_weight = get_current_bucket_weight(cfg, bucket, mode) if cfg is not None else None

    if cfg is not None and cfg.get("learning", {}).get("require_shadow_validation", True) and not force:
        validated = (
            challenge_result is not None
            and challenge_result.get("ready")
            and challenge_result.get("recommendation") == "promote_challenger"
        )
        if not validated:
            reason = (
                f"Refusing to apply {bucket}={new_weight_0_100:.2f} to live config without an "
                f"out-of-sample champion/challenger promotion (learning.require_shadow_validation "
                f"is on). Call propose_as_challenge() to start the shadow test, or pass force=True "
                f"to bypass this gate explicitly (logged, not recommended)."
            )
            _record_provenance(
                db, bucket, mode, old_weight, new_weight_0_100, decision="rejected", decision_reason=reason,
                challenge_result=challenge_result, strategy_version=strategy_version,
                feature_ranking=feature_ranking, walk_forward_report=walk_forward_report, trade_count=trade_count,
            )
            raise ShadowValidationRequired(reason)

    weights_key = _MODE_TO_WEIGHTS_KEY.get(mode, "swing_buy")
    with open(CONFIG_PATH, "r") as f:
        raw = yaml.safe_load(f)
    raw.setdefault("weights", {}).setdefault(weights_key, {}).setdefault("bucket_weights", {})[bucket] = new_weight_0_100 / 100.0
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)

    decision = "forced" if force and not (
        challenge_result is not None and challenge_result.get("ready")
        and challenge_result.get("recommendation") == "promote_challenger"
    ) else "accepted"
    decision_reason = (
        "force=True explicitly bypassed the shadow-validation gate" if decision == "forced"
        else "validated by out-of-sample champion/challenger promotion" if challenge_result is not None
        else "applied with no shadow-validation gate active (cfg not supplied or gate disabled)"
    )
    _record_provenance(
        db, bucket, mode, old_weight, new_weight_0_100, decision=decision, decision_reason=decision_reason,
        challenge_result=challenge_result, strategy_version=strategy_version, feature_ranking=feature_ranking,
        walk_forward_report=walk_forward_report, trade_count=trade_count,
    )
    return new_weight_0_100


def propose_as_challenge(cfg: dict, db, bucket: str, new_weight_0_100: float, mode: str = "swing") -> str:
    """The SAFE path from a propose_update() proposal to actually changing
    live behavior: instead of writing straight to config.yaml, this builds a
    challenger config (a full copy of the live config with ONLY this one
    bucket weight changed) and starts a champion/challenger test
    (learning/champion_challenger.py) - the challenger runs in parallel,
    watch-only, never placing its own trades, until
    champion_challenger_min_trades_for_significance trades have accumulated
    on each side and a two-proportion z-test can say whether the challenger
    is really better, not just luckier over a short run. Returns the
    challenge_id - pass its eventual ChampionChallenger.evaluate() result
    into apply_challenge_promoted_weight() once it's ready."""
    import json
    from learning.champion_challenger import ChampionChallenger

    weights_key = _MODE_TO_WEIGHTS_KEY.get(mode, "swing_buy")
    challenger_cfg = json.loads(json.dumps(cfg))  # deep copy without importing copy.deepcopy's edge cases on this dict
    challenger_cfg.setdefault("weights", {}).setdefault(weights_key, {}).setdefault("bucket_weights", {})[bucket] = (
        new_weight_0_100 / 100.0
    )
    cc = ChampionChallenger(db, cfg)
    return cc.start_challenge(challenger_cfg)


def apply_challenge_promoted_weight(cfg: dict, db, challenge_id: str, bucket: str, mode: str = "swing"):
    """Once learning/champion_challenger.py's evaluate() says a challenge
    (started via propose_as_challenge()) is ready with recommendation
    "promote_challenger", call this to actually write the validated weight
    to live config.yaml AND mark the challenge promoted - one call instead
    of manually re-deriving the weight from the stored challenger_config
    JSON. Raises ShadowValidationRequired (via apply_bucket_weight_to_config)
    if the challenge isn't actually in a promotable state - this function
    doesn't re-implement that check, it just supplies the real
    evaluate() result so the gate can verify it itself.

    Also passes db through to apply_bucket_weight_to_config() so this
    promotion is captured in weight_change_log with full provenance -
    a strategy-version snapshot, the current feature-importance ranking,
    the latest walk-forward report, and the champion+challenger trade count
    that made the promotion statistically valid."""
    import json
    from analytics.feature_importance import rank_all_features
    from learning.champion_challenger import ChampionChallenger
    from learning.model_versioning import versions_as_dict

    cc = ChampionChallenger(db, cfg)
    result = cc.evaluate(challenge_id)
    row = db.get_challenge(challenge_id)
    if not row:
        raise ValueError(f"No challenge found for id {challenge_id}")
    challenger_cfg = json.loads(row["challenger_config"])
    weights_key = _MODE_TO_WEIGHTS_KEY.get(mode, "swing_buy")
    new_fraction = challenger_cfg.get("weights", {}).get(weights_key, {}).get("bucket_weights", {}).get(bucket)
    if new_fraction is None:
        raise ValueError(f"Challenge {challenge_id} doesn't contain a weight for {weights_key}/{bucket}")

    pd_mode = "SWING" if mode == "swing" else "DAY"
    try:
        feature_ranking = rank_all_features(db, mode=pd_mode, include_drift=False)
    except Exception:
        feature_ranking = None
    try:
        strategy_version = versions_as_dict(cfg)
    except Exception:
        strategy_version = None
    walk_forward_report = db.get_last_learning_run(mode=pd_mode)
    trade_count = (row.get("champion_trades") or 0) + (row.get("challenger_trades") or 0)

    applied = apply_bucket_weight_to_config(
        bucket, new_fraction * 100.0, mode=mode, cfg=cfg, challenge_result=result,
        db=db, strategy_version=strategy_version, feature_ranking=feature_ranking,
        walk_forward_report=walk_forward_report, trade_count=trade_count,
    )
    cc.promote(challenge_id)
    return applied
