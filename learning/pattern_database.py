"""25-feature pattern database: stores a feature vector + eventual outcome for
every trade, and finds historically similar setups via cosine similarity with
recency decay. This is the "memory" the EV engine, Bayesian updater, and
walk-forward optimizer all read from.

SIMPLIFICATION NOTE: a real production feature store would maintain versioned,
pre-computed encodings and embeddings. Here, categorical features are one-hot
encoded and numeric features z-score normalized at QUERY TIME, against whatever
patterns currently exist in the DB. This is correct and genuinely computes
cosine similarity, but the encoding can shift slightly as the database grows -
acceptable for a single-user, single-machine tool at this trade volume, but
worth knowing about if you extend this into something with much higher data
volume or multiple concurrent readers/writers.
"""
import hashlib
import json
import math
from datetime import datetime

import numpy as np

# The 25 features tracked per signal (matches the v8.3 spec's FEATURES list).
# Split here into numeric vs categorical because they need different encoding.
NUMERIC_FEATURES = [
    "bull_pct", "bear_pct", "choppy_pct", "transition_prob",
    "vix_raw", "vix_percentile_1y", "vix_percentile_3m",
    "fg_score",
    "gap_pct", "change_pct", "volume_ratio", "premarket_gap", "premarket_rvol",
    "rsi14", "bb_pct", "adx", "cmf", "stochastic_k",
    "bucket1_score", "bucket2_score", "bucket3_score", "bucket4_score",
    "bucket5_score", "bucket6_score", "final_score",
    "sector_rs_1d", "sector_rs_1m",
]
CATEGORICAL_FEATURES = [
    "regime", "fg_rating", "macd_crossover", "squeeze_active",
    "finviz_rating", "analyst_consensus", "insider_direction", "unusual_options",
    "setup_type", "sector", "day_of_week", "opex_status", "session",
]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# Adaptive similarity threshold - tighter as more candidate matches exist.
SIMILARITY_THRESHOLD_BY_COUNT = [
    (15, 0.90), (30, 0.80), (100, 0.75), (float("inf"), 0.70),
]

MIN_RECENCY_COUNT_BY_FREQUENCY = {
    "VERY_COMMON": 15, "COMMON": 15, "MODERATE": 20, "RARE": 30, "VERY_RARE": 50,
}


def _similarity_threshold(n_candidates: int) -> float:
    for max_n, threshold in SIMILARITY_THRESHOLD_BY_COUNT:
        if n_candidates <= max_n:
            return threshold
    return 0.70


def _encode_patterns(patterns: list[dict], query_features: dict) -> tuple[np.ndarray, list[np.ndarray]]:
    """Builds a shared vector space from `patterns` + the query, and returns
    (query_vector, [pattern_vectors]). Numeric features z-scored across the
    pattern set (query included so it's on the same scale); categorical
    features one-hot encoded over the union of observed categories."""
    all_feature_dicts = [p["features"] for p in patterns] + [query_features]

    numeric_matrix = []
    for feats in all_feature_dicts:
        numeric_matrix.append([float(feats.get(f) or 0.0) for f in NUMERIC_FEATURES])
    numeric_matrix = np.array(numeric_matrix, dtype=float)
    means = numeric_matrix.mean(axis=0)
    stds = numeric_matrix.std(axis=0)
    stds[stds == 0] = 1.0
    numeric_z = (numeric_matrix - means) / stds

    cat_vocab = {}
    for f in CATEGORICAL_FEATURES:
        values = sorted({str(feats.get(f, "")) for feats in all_feature_dicts})
        cat_vocab[f] = values

    cat_vectors = []
    for feats in all_feature_dicts:
        row = []
        for f in CATEGORICAL_FEATURES:
            values = cat_vocab[f]
            one_hot = [1.0 if str(feats.get(f, "")) == v else 0.0 for v in values]
            row.extend(one_hot)
        cat_vectors.append(row)
    cat_vectors = np.array(cat_vectors, dtype=float)

    full_vectors = np.hstack([numeric_z, cat_vectors])
    query_vector = full_vectors[-1]
    pattern_vectors = full_vectors[:-1]
    return query_vector, pattern_vectors


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _recency_weight(recorded_at: str, lambda_decay: float) -> float:
    try:
        recorded = datetime.fromisoformat(recorded_at)
    except (ValueError, TypeError):
        return 1.0
    days_ago = max(0.0, (datetime.utcnow() - recorded).total_seconds() / 86400)
    return math.exp(-lambda_decay * days_ago / 365.0)


def config_fingerprint(cfg: dict) -> str:
    """Hash of every config value that can change a score or an exit (§17).

    Two patterns with different fingerprints were produced by different
    strategies and must NOT be pooled, however close their timestamps are.
    This is what makes the Phase 4 recalibration (§19) self-partitioning: the
    moment the weights change, every subsequent row is automatically
    distinguishable from every earlier one, with nobody having to remember
    the date.

    Deliberately narrow. Adding a key here invalidates comparability with
    every previously recorded pattern, so it must only cover values that
    genuinely alter a decision - not watchlists, not intervals, not
    notification settings. `default=str` keeps it total: an unexpected
    non-JSON value produces a stable-but-ugly hash rather than an exception
    on the trade-recording path.
    """
    material = {
        "weights": cfg.get("weights"),
        "buy_rules": cfg.get("buy_rules"),
        "sell_rules": cfg.get("sell_rules"),
        "stop_machine": cfg.get("stop_machine"),
        "position_sizing": cfg.get("position_sizing"),
        "risk_level": cfg.get("risk_level"),
        "risk": {k: v for k, v in (cfg.get("risk") or {}).items()
                 if k in ("CONSERVATIVE", "MODERATE", "AGGRESSIVE", "TURBO")},
    }
    blob = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class PatternDatabase:
    def __init__(self, db):
        self.db = db

    def record_entry(self, ticker: str, mode: str, features: dict, trade_id: str = None,
                      cfg: dict = None) -> int:
        """Called at signal/entry time - outcome fields stay NULL until close_trade().

        `cfg` (§17, Phase 1) stamps the row with the build and the
        configuration fingerprint that produced it. Optional so that existing
        callers keep working during the migration; when it is absent the row
        records config_fingerprint='unstamped', which is honest and
        filterable, rather than NULL, which reads as 'not recorded yet'.
        """
        fingerprint = config_fingerprint(cfg) if cfg is not None else "unstamped"
        return self.db.add_pattern(ticker, mode, features, trade_id=trade_id,
                                    config_fingerprint=fingerprint)

    def close_trade(self, pattern_id: int, outcome_pct: float, hold_hours: float,
                     exit_reason: str, exit_kind: str = None):
        """§50 (Phase 2.5): `exit_kind` is the countable companion to
        `exit_reason` - one of rules/common.py's EXIT_KINDS. Left None it is
        derived from the reason string where that string is a structured token
        (see classify_exit), and stays NULL where it is prose. Pass it
        explicitly from any caller that holds the structured value."""
        self.db.close_pattern(pattern_id, outcome_pct, hold_hours, exit_reason,
                               exit_kind=exit_kind)

    def find_similar_trades(
        self,
        signal_features: dict,
        mode: str = "SWING",
        event_frequency: str = "COMMON",
        regime_filter: str = None,
        lambda_decay: float = None,
    ) -> list[dict]:
        """Returns closed historical patterns similar to `signal_features`, each
        annotated with `similarity` and `recency_weight`. Caller (ev_engine.py)
        decides whether there are enough matches to trust."""
        lambda_decay = lambda_decay if lambda_decay is not None else (0.70 if mode == "SWING" else 1.40)
        min_count = MIN_RECENCY_COUNT_BY_FREQUENCY.get(event_frequency, 15)

        candidates = self.db.get_patterns(mode=mode, closed_only=True)
        if regime_filter:
            candidates = [c for c in candidates if c["features"].get("regime") == regime_filter]

        if not candidates:
            return []

        query_vector, pattern_vectors = _encode_patterns(candidates, signal_features)
        threshold = _similarity_threshold(len(candidates))

        results = []
        for candidate, vec in zip(candidates, pattern_vectors):
            sim = _cosine_similarity(query_vector, vec)
            if sim < threshold:
                continue
            weight = _recency_weight(candidate["recorded_at"], lambda_decay)
            results.append({**candidate, "similarity": sim, "recency_weight": weight})

        results.sort(key=lambda r: r["similarity"] * r["recency_weight"], reverse=True)
        return results

    def pattern_confidence(self, similar_trades: list[dict]) -> dict:
        """count_score + similarity_score, NOT count alone (per build note #26)."""
        if not similar_trades:
            return {"pattern_confidence": 0, "label": "no_data", "n_matches": 0}

        weighted_count = sum(t["recency_weight"] for t in similar_trades)
        count_score = min(50.0, (math.log10(weighted_count + 1) / math.log10(200)) * 50)
        avg_similarity = sum(t["similarity"] for t in similar_trades) / len(similar_trades)
        similarity_score = avg_similarity * 50

        confidence = count_score + similarity_score
        if confidence >= 75:
            label = "high"
        elif confidence >= 50:
            label = "medium"
        elif confidence >= 25:
            label = "low"
        else:
            label = "very_low"

        return {
            "pattern_confidence": round(confidence, 1),
            "label": label,
            "n_matches": len(similar_trades),
            "weighted_count": round(weighted_count, 1),
            "avg_similarity": round(avg_similarity, 3),
        }
