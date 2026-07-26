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

2026-07-26 (documentation audit): that "once the deferred engines exist"
sentence had come due and nobody collected. ADX, CMF, sector RS, the TTM
squeeze, unusual-options flow and the opex calendar all went REAL over
several sessions - in engine/ticker_analyzer.py's _calc_indicators, in
engine/market_breadth.py's get_sector_return()/_opex_status() - and
engine/ticker_data_adapter.py was updated each time. This module was not,
so seven of the 40 encoded features were still being written as literal
0.0/False/"normal" while the real values sat in the caller's scope.

Why that was worse than merely wasteful. The four numeric ones (adx, cmf,
sector_rs_1d, sector_rs_1m) z-score to exactly 0.0 for every row - dead
weight, no harm. The three CATEGORICAL ones (squeeze_active,
unusual_options, opex_status) are one-hot encoded over the union of observed
values, so a constant means every pattern pair matches on those dimensions.
That inflates the cosine similarity of every comparison uniformly and pushes
unrelated setups over the SIMILARITY_THRESHOLD_BY_COUNT bar - the similarity
search was quietly getting LESS discriminating, not just no better.

Values are taken from the caller's already-built ticker_dict/market_dict
(engine/ticker_data_adapter.py) when passed, so this costs zero extra MCP
calls or recomputation. Both arguments are optional: confirm_fill.py and any
older caller that omits them falls back to the TickerData attributes, and
then to the original neutral defaults. What remains genuinely placeholder is
now a short and specific list - see the block at the bottom of `features`.
"""
from datetime import datetime

from learning.pattern_database import FEATURE_SCHEMA_VERSION


def build_pattern_features(ticker: str, td, mkt, buy_result, cfg: dict,
                            regime=None, score_result=None,
                            ticker_dict=None, market_dict=None) -> dict:
    """
    regime: engine/regime_engine.py's RegimeState, or None if the caller hasn't
        run the Phase 1 regime engine for this cycle (falls back to the old
        "unknown" placeholder).
    score_result: rules/swing_buy_rules.py's SwingScoreResult, or None if the
        caller is still using the simple 15-rule rules/buy_rules.py engine
        (falls back to buy_result.top_signals/rules_passed instead).
    ticker_dict / market_dict: the flat dicts engine/ticker_data_adapter.py
        already built for this ticker this cycle (ticker_to_dict /
        market_to_dict). Optional - passing them is how adx/cmf/sector RS/
        squeeze/unusual-options/opex reach the pattern database as REAL
        values instead of the pre-2026-07-26 constants. Omitting them is
        safe and falls back to TickerData attributes, then to neutral
        defaults; nothing raises on a missing key.
    """
    _tdict = ticker_dict or {}
    _mdict = market_dict or {}

    def _num(key, attr=None, default=0.0):
        """ticker_dict first, then the TickerData attribute, then default.
        A present-but-None value counts as absent - `or` is deliberately
        avoided so a real 0.0 reading is not mistaken for a missing one."""
        v = _tdict.get(key)
        if v is None and attr is not None:
            v = getattr(td, attr, None)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # unusual_options is tri-state on purpose. td.unusual_options_bullish is
    # True / False / None, and None means "stock-scanner MCP did not answer
    # this cycle" - which is NOT the same claim as "no unusual flow". It is a
    # categorical feature, so the encoder one-hots "None" as its own bucket
    # and an outage cohort stops being silently pooled with a quiet-tape
    # cohort. See engine/ticker_data_adapter.py's note on the same field.
    _unusual = _tdict.get("unusual_options_bullish",
                          getattr(td, "unusual_options_bullish", None))
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
        # feature_schema (2026-07-26): stamps this row as carrying REAL adx/
        # cmf/sector RS/squeeze/unusual-options/opex. Rows written before this
        # date have no stamp, and learning/pattern_database.py's
        # _encode_patterns treats their constants as missing rather than as
        # measurements - see FEATURE_SCHEMA_VERSION there for why encoding
        # them as real 0.0s would have been worse than leaving the bug alone.
        "feature_schema": FEATURE_SCHEMA_VERSION,

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

        # ---- REAL as of 2026-07-26 (see the module docstring for why these
        # spent several sessions as constants after their sources went live).
        # adx/cmf: engine/ticker_analyzer.py's _calc_indicators, from the same
        # daily OHLCV bars as every other indicator. sector_rs_1d/1m: the
        # ticker's sector ETF vs SPY, from the price history
        # engine/market_breadth.py already fetches. squeeze_active: TTM
        # squeeze fired. opex_status: a pure calendar calculation
        # (market_breadth._opex_status), never needed a data source at all.
        # unusual_options: tri-state, see the note above. ----
        "adx": _num("adx", "adx"),
        "cmf": _num("cmf", "cmf"),
        "sector_rs_1d": _num("sector_rs_1d"),
        "sector_rs_1m": _num("sector_rs_1m"),
        "squeeze_active": bool(_tdict.get("squeeze_active",
                                          getattr(td, "squeeze_active", False))),
        "unusual_options": _unusual,
        "opex_status": _mdict.get("opex_status", "normal"),

        # ---- still genuine placeholders, and now an exhaustive list ----
        # vix_percentile_1y/3m: engine/market_context.py fetches SPOT VIX
        #   only (_get_vix -> yfinance). A percentile needs 1y/3m of VIX
        #   history, which nothing in this stack stores or fetches yet.
        # premarket_gap/premarket_rvol/gap_pct: the scheduler runs inside
        #   regular market hours only (features["session"] is hardcoded
        #   "regular" three lines up for the same reason), so there is no
        #   premarket observation to record. gap_pct is overnight
        #   open-vs-prior-close, which is derivable from daily bars but is
        #   not currently computed anywhere - left honest rather than
        #   half-wired.
        "vix_percentile_1y": 0.0, "vix_percentile_3m": 0.0,
        "gap_pct": 0.0, "premarket_gap": 0.0, "premarket_rvol": 0.0,

        # ---- bookkeeping (not part of NUMERIC/CATEGORICAL_FEATURES, ignored
        # by the similarity encoder, used only by scheduler's close-out logic) ----
        "_entry_price": td.price,
        "rules_passed": rules_passed,
    }
    return features
