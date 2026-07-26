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
    "bucket5_score", "bucket6_score", "bucket7_score", "final_score",
    "sector_rs_1d", "sector_rs_1m",
]
CATEGORICAL_FEATURES = [
    "regime", "fg_rating", "macd_crossover", "squeeze_active",
    "finviz_rating", "analyst_consensus", "insider_direction", "unusual_options",
    "setup_type", "sector", "day_of_week", "opex_status", "session",
]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# ── Feature schema versioning (2026-07-26, documentation audit) ─────────────
#
# THE PROBLEM THIS SOLVES. Until 2026-07-26, engine/pattern_features.py wrote
# literal constants for the seven features below - 0.0 for the numeric ones,
# False/False/"normal" for the categorical ones - long after their real data
# sources went live. Fixing the writer is necessary but, on its own, actively
# harmful to every row already in the table.
#
# Why it is harmful rather than merely uneven: `_encode_patterns` z-scores each
# numeric column across the candidate set at QUERY time. While every row held
# 0.0 for adx, the column's std was 0, the guard below forced it to 1.0, and
# every row encoded to exactly 0.0 - dead weight, but harmless and symmetric.
# The moment real ADX readings (typically 15-40) start landing, the column mean
# jumps and every historical row's 0.0 z-scores to a large NEGATIVE number. The
# old rows do not become uninformative; they start actively asserting
# "extremely low ADX", which is a claim nobody ever measured. Same for the
# categorical three, where a stale "False" is a definite claim rather than an
# absent one.
#
# THE FIX. Rows are stamped with the schema that produced them. Anything
# without the stamp is schema 1, and its seven affected features are treated as
# MISSING rather than as measurements: numeric ones are imputed to the column
# mean (which z-scores to 0.0 - "this row tells us nothing about ADX", the
# honest encoding), and categorical ones are mapped to a distinct "unrecorded"
# bucket so they cannot masquerade as a real False.
#
# Note this is deliberately NOT folded into config_fingerprint(). That hash
# answers "was this row produced by a different strategy", and the strategy did
# not change here - only the fidelity of what was recorded about it. Conflating
# the two would discard the entire pattern history for what is, correctly
# handled, a recoverable gap.
FEATURE_SCHEMA_VERSION = 2

# The seven features that were constants under schema 1.
SCHEMA_1_UNRECORDED_NUMERIC = ("adx", "cmf", "sector_rs_1d", "sector_rs_1m")
SCHEMA_1_UNRECORDED_CATEGORICAL = ("squeeze_active", "unusual_options", "opex_status")

# Sentinel for a categorical value that was never actually observed. A plain
# empty string would collide with `_encode_patterns`'s own default for a key
# that is simply absent, which is a different situation.
UNRECORDED = "__unrecorded__"

# Sentinel distinguishing "key never written" from "key present with value
# None/0.0" for numeric features. Plain dict.get(key) can't tell those apart,
# and for bucket7_score (below) the difference matters: a real 0.0 reading of
# the VOLATILITY_EXPANSION bucket is evidence, but a row recorded before that
# bucket existed (or via the legacy buy_result.py path, which never wrote
# bucket7_score at all - see engine/pattern_features.py) has no such evidence
# and must not be silently scored as "measured zero volatility expansion".
_KEY_ABSENT = object()

# Numeric features that may be genuinely absent from a stored row's `features`
# dict (as opposed to schema-1's *always-present-but-constant* fields above),
# and must therefore be treated as MISSING - not zero - whenever the key is
# not there at all. Audit finding P1-01 (external review, 2026-07-26): the
# live scorer has produced 7 buckets since VOLATILITY_EXPANSION was added, but
# this schema only recognized 6, so bucket7_score was silently dropped from
# every similarity comparison even though rules/swing_buy_rules.py /
# engine/pattern_features.py have been computing and storing it all along.
# Fixed by adding it to NUMERIC_FEATURES above; this set is what keeps that
# addition from retroactively asserting "measured zero" on the pre-existing
# rows that predate it.
KEY_ABSENT_AS_MISSING_NUMERIC = ("bucket7_score",)


def _row_schema(feats: dict) -> int:
    return int(feats.get("feature_schema") or 1)

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
    features one-hot encoded over the union of observed categories.

    Schema-1 rows (see FEATURE_SCHEMA_VERSION above) carry constants for seven
    features that were never actually measured. Those are encoded as MISSING
    here - NaN through the z-score, which mean-imputation turns into 0.0 - so
    an unmeasured row contributes nothing on that axis instead of asserting an
    extreme value. Mean and std are computed with nan-aware reductions so the
    unmeasured rows also do not drag the real distribution.
    """
    all_feature_dicts = [p["features"] for p in patterns] + [query_features]

    numeric_matrix = []
    for feats in all_feature_dicts:
        stale = _row_schema(feats) < 2
        row = []
        for f in NUMERIC_FEATURES:
            if stale and f in SCHEMA_1_UNRECORDED_NUMERIC:
                row.append(np.nan)
                continue
            if f in KEY_ABSENT_AS_MISSING_NUMERIC and feats.get(f, _KEY_ABSENT) is _KEY_ABSENT:
                # Key was never written for this row (pre-bucket7 row, or the
                # legacy buy_result.py path) - MISSING, not a measured zero.
                row.append(np.nan)
                continue
            v = feats.get(f)
            try:
                row.append(float(v) if v is not None else 0.0)
            except (TypeError, ValueError):
                row.append(0.0)
        numeric_matrix.append(row)
    numeric_matrix = np.array(numeric_matrix, dtype=float)

    # A column that is ALL-NaN (every candidate predates the schema bump, which
    # is the normal case right after it lands) would make nanmean emit a
    # RuntimeWarning and produce NaN. Handle it explicitly: the whole column
    # carries no information, so it encodes to zero for everyone.
    all_nan = np.isnan(numeric_matrix).all(axis=0)
    means = np.where(all_nan, 0.0, np.nanmean(
        np.where(all_nan, 0.0, numeric_matrix), axis=0))
    stds = np.where(all_nan, 1.0, np.nanstd(
        np.where(all_nan, 0.0, numeric_matrix), axis=0))
    stds[stds == 0] = 1.0
    numeric_z = (numeric_matrix - means) / stds
    # Mean-imputation, applied after standardising: a missing value sits at the
    # column mean, which is exactly z = 0.
    numeric_z = np.nan_to_num(numeric_z, nan=0.0)

    def _cat(feats, f):
        """The categorical value for one feature, with schema-1's unmeasured
        constants routed to their own bucket rather than counted as a real
        observation of False/'normal'."""
        if _row_schema(feats) < 2 and f in SCHEMA_1_UNRECORDED_CATEGORICAL:
            return UNRECORDED
        return str(feats.get(f, ""))

    cat_vocab = {}
    for f in CATEGORICAL_FEATURES:
        cat_vocab[f] = sorted({_cat(feats, f) for feats in all_feature_dicts})

    cat_vectors = []
    for feats in all_feature_dicts:
        row = []
        for f in CATEGORICAL_FEATURES:
            values = cat_vocab[f]
            v = _cat(feats, f)
            row.extend([1.0 if v == val else 0.0 for val in values])
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
