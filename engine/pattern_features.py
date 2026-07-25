"""Maps a live signal (TickerData + MarketContextData + BuyResult) onto the
pattern_database schema (learning/pattern_database.py's NUMERIC_FEATURES /
CATEGORICAL_FEATURES). This is the wiring between the active 15-rule engine
and the learning backend.

HONESTY NOTE: several fields in the 25-feature spec belong to engine pieces
that were explicitly deferred (regime engine, 6-bucket scoring, ADX/CMF,
premarket data, options-expiration calendar). Those are filled with neutral
placeholders below and clearly marked - they are NOT silently faked as real
signals. Once the deferred engines exist, replace the placeholder block with
real values and the pattern database will pick up the improvement immediately
(nothing else needs to change - similarity search just uses whatever is in
`features`).
"""
from datetime import datetime


def build_pattern_features(ticker: str, td, mkt, buy_result, cfg: dict,
                            regime=None, score_result=None) -> dict:
    """
    regime: engine/regime_engine.py's RegimeState, or None if the caller hasn't
        run the Phase 1 regime engine for this cycle (falls back to the old
        "unknown" placeholder).
    score_result: rules/swing_buy_rules.py's SwingScoreResult, or None if the
        caller is still using the simple 15-rule rules/buy_rules.py engine
        (falls back to buy_result.top_signals/rules_passed instead).
    """
    if score_result is not None:
        top_signal = score_result.rules_fired[0] if score_result.rules_fired else "unspecified"
        rules_passed = score_result.rules_fired
        final_score = score_result.final_score_pct
        bucket_scores = {
            f"bucket{i+1}_score": (b.points / b.max_points * 100 if b.max_points else 0.0)
            for i, b in enumerate(score_result.buckets)
        }
        # HONESTY NOTE: rules/swing_buy_rules.py now scores 7 buckets (added
        # VOLATILITY_EXPANSION), so this produces bucket1_score..bucket7_score.
        # learning/pattern_database.py's NUMERIC_FEATURES only lists
        # bucket1_score..bucket6_score - bucket7_score is silently dropped
        # when this dict is written to the pattern database (Python dict
        # extra keys are just ignored there, not an error). Not extended here
        # since that's a real schema migration (NUMERIC_FEATURES + a DB
        # column + backfill for existing rows) beyond what was asked for -
        # flagging so it isn't mistaken for an oversight.
    else:
        top_signal = buy_result.top_signals[0].name if buy_result.top_signals else "unspecified"
        rules_passed = [r.name for r in buy_result.rules_passed]
        final_score = buy_result.pct_score
        bucket_scores = {f"bucket{i}_score": 0.0 for i in range(1, 7)}

    # Outage hygiene flag (2026-07-15c, external review): patterns recorded
    # while EXTERNAL sources were down carry a redistributed-weight score
    # that isn't comparable to fully-observed ones. The learning loop /
    # EV engine can (and should) filter on this before trusting a
    # similarity cohort. False when score_result is absent or clean.
    _external_outage = bool(
        score_result is not None
        and getattr(score_result, "threshold_result", None)
        and (score_result.threshold_result or {}).get("data_coverage", {}).get("unavailable_buckets")
    )
    features = {
        # ---- real, currently-computed values ----
        "external_outage": _external_outage,
        "vix_raw": mkt.vix_level,
        "fg_score": mkt.fear_greed_score,
        "change_pct": td.change_pct,
        "volume_ratio": td.volume_ratio,
        "rsi14": td.rsi,
        "bb_pct": td.bb_pct,
        "stochastic_k": td.stoch_k,
        "final_score": final_score,
        "fg_rating": mkt.fear_greed_rating,
        "macd_crossover": td.macd_crossover_direction,
        "finviz_rating": getattr(td, "technical_rating", "unknown"),
        "analyst_consensus": getattr(td, "analyst_rating", "unknown"),
        "insider_direction": getattr(td, "insider_net_direction", "unknown"),
        "sector": getattr(td, "sector", "unknown"),
        "setup_type": top_signal,
        "day_of_week": datetime.utcnow().strftime("%A"),
        "session": "regular",  # scheduler only runs inside market hours
        **bucket_scores,

        # ---- real once the Phase 1 regime engine runs (falls back to
        # neutral placeholders if regime=None, e.g. old buy_rules.py path) ----
        "regime": regime.dominant_regime if regime else "unknown",
        "bull_pct": regime.bull_pct if regime else 0.0,
        "bear_pct": regime.bear_pct if regime else 0.0,
        "choppy_pct": regime.choppy_pct if regime else 0.0,
        "transition_prob": regime.transition_probability if regime else 0.0,

        # ---- still placeholders: no data source wired yet (ADX/CMF,
        # premarket, VIX percentile, sector RS, options-expiry calendar) ----
        "vix_percentile_1y": 0.0, "vix_percentile_3m": 0.0,
        "gap_pct": 0.0, "premarket_gap": 0.0, "premarket_rvol": 0.0,
        "adx": 0.0, "cmf": 0.0,
        "sector_rs_1d": 0.0, "sector_rs_1m": 0.0,
        "squeeze_active": False, "unusual_options": False,
        "opex_status": "normal",

        # ---- bookkeeping (not part of NUMERIC/CATEGORICAL_FEATURES, ignored
        # by the similarity encoder, used only by scheduler's close-out logic) ----
        "_entry_price": td.price,
        "rules_passed": rules_passed,
    }
    return features
