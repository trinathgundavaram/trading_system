"""Regime-Specific Weight Adaptation - Priority 6 from the deployment review,
explicitly the LAST priority and explicitly gated: "adapting weights [...]
after sufficient live data." The review's own prerequisites for automating
any weight adaptation (listed before Weak Spot #1) are: enough trade history
(a few hundred trades), statistical significance (not just win rate),
out-of-sample validation via the champion/challenger framework, and
config-driven weights. The last of those is what Priority 3
(rules/swing_buy_rules.py's _bucket_weight(), rules/exit_scorer.py's
_exit_bucket_weight()) just built. The first three are DATA prerequisites
this fresh system does not yet have - so this module is a scaffold, disabled
by default (config.yaml's regime_weight_adaptation.enabled: false), with the
live-data gate enforced IN CODE (not just documentation) so it can't
silently start adjusting weights the moment someone flips the config flag
before there's enough history to justify it.

Bull-market intuition from the review: TREND up, MOMENTUM up, MARKET_BREADTH
down (breadth already near-universal in a strong bull, less discriminating).
Bear-market intuition: MARKET_BREADTH up, FUNDAMENTAL_RISK up (proxy for
"risk" - swing_buy has no dedicated risk bucket, EXTERNAL/SENTIMENT_MACRO
double as sentiment-and-macro risk carriers), MOMENTUM down, TREND down
(trend signals are least reliable near a regime transition).

Deltas are ADDED to the Priority-3 config-driven base weights, then clipped
to learning/bayesian_updater.py's existing BUCKET_WEIGHT_BOUNDS (the same
safety rails the Bayesian updater itself respects - regime adaptation
shouldn't be able to push a bucket anywhere the Bayesian updater couldn't),
then renormalized so all bucket weights still sum to 1.0. Unlike
learning/bayesian_updater.py's apply_bucket_weight_to_config() (which
deliberately changes ONE bucket a human reviewed and approved), this is a
coordinated multi-bucket shift applied together by regime - renormalizing
here is expected, not a silent side effect on buckets nobody asked about.
"""
from learning.bayesian_updater import BUCKET_WEIGHT_BOUNDS

# HONESTY NOTE: BUCKET_WEIGHT_BOUNDS (learning/bayesian_updater.py) only
# defines bounds for the swing-buy/day-trade BUY buckets (TREND, MOMENTUM,
# ...) - it has no entries for the exit-score bucket names
# (TREND_DETERIORATION, MOMENTUM_WEAKNESS, ...), since the Bayesian updater
# itself was only ever wired to the buy side. Mapping "exit_score" here is
# so a future bounds table can be added without changing this module - until
# then, exit_score weights are renormalized after a regime delta but NOT
# bound-clipped (get_effective_bucket_weights()'s `if bound:` check just
# skips clipping when no matching key exists). Acceptable for now because
# the master `enabled: false` gate keeps this scaffold off by default either way.
_ENGINE_TO_MODE = {"swing_buy": "swing", "exit_score": "swing"}


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("regime_weight_adaptation", {}) or {}


def _base_weights(cfg: dict, engine: str) -> dict:
    return dict((cfg or {}).get("weights", {}).get(engine, {}).get("bucket_weights", {}) or {})


def _live_data_gate_passed(cfg: dict, db) -> tuple:
    """Returns (passed: bool, reason: str). Fails CLOSED (adaptation off) on
    any missing data/db - the whole point of a gate is that the absence of
    evidence blocks the change, not defaults to allowing it."""
    rcfg = _cfg(cfg)
    min_trades = int(rcfg.get("min_closed_trades_required", 200))
    if db is None:
        return False, "no db handle passed - cannot verify trade history"
    try:
        closed = db.get_patterns(closed_only=True)
    except Exception as e:
        return False, f"could not read closed trade history: {e}"
    n = len(closed)
    if n < min_trades:
        return False, f"{n} closed trades < {min_trades} required minimum"
    return True, f"{n} closed trades >= {min_trades} required minimum"


def get_effective_bucket_weights(cfg: dict, regime, db=None, engine: str = "swing_buy") -> dict:
    """
    cfg: the full config dict.
    regime: engine/regime_engine.py's RegimeState, or None (no adaptation possible without one).
    db: storage/database.py's Database, used ONLY for the live-data gate count.
    engine: "swing_buy" or "exit_score" - which weights.* section to adapt.

    Returns a bucket_name -> weight dict, either the untouched Priority-3
    config-driven base weights (adaptation disabled, gate not met, or no
    regime data), or the regime-adjusted + renormalized version.
    """
    base = _base_weights(cfg, engine)
    rcfg = _cfg(cfg)

    if not rcfg.get("enabled", False) or regime is None or not base:
        return base

    passed, reason = _live_data_gate_passed(cfg, db)
    if not passed:
        return base

    regime_key = (getattr(regime, "dominant_regime", "") or "").lower()
    deltas = rcfg.get(regime_key, {}) or {}
    if not deltas:
        return base

    mode = _ENGINE_TO_MODE.get(engine, "swing")
    bounds = BUCKET_WEIGHT_BOUNDS.get(mode, {})

    adjusted = {}
    for bucket, weight in base.items():
        new_weight = weight + float(deltas.get(bucket, 0.0))
        bound = bounds.get(bucket)
        if bound:
            lo, hi = bound[0] / 100.0, bound[1] / 100.0  # BUCKET_WEIGHT_BOUNDS is in 0-100 scale
            new_weight = max(lo, min(hi, new_weight))
        adjusted[bucket] = max(0.0, new_weight)

    total = sum(adjusted.values()) or 1.0
    return {k: v / total for k, v in adjusted.items()}
