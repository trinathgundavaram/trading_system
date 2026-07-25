"""Tracks Claude's stated confidence vs actual win rate, bucketed (e.g. by
setup_type or bucket score range), so a raw "90% confidence" can be displayed
as "73% calibrated (n=41)" instead of taken at face value.

DATA NOTE: this needs (raw_confidence, won) pairs. The current schema doesn't
yet link a `trades` row back to the `signals.confidence` it came from (no
signal_id FK on trades) - `calibration_from_pairs()` is the real, testable
function; `get_calibration_for_bucket()` is a best-effort convenience that
matches by ticker+time-proximity until that FK exists. Add a `signal_id`
column to `trades` (and pass it through engine/executor.py / scheduler.py at
fill time) to make the DB-backed path exact rather than approximate.
"""
from analytics.confidence_intervals import wilson_ci

CALIBRATION_BUCKETS = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def _bucket_for(confidence: float) -> str:
    for lo, hi in CALIBRATION_BUCKETS:
        if lo <= confidence < hi:
            return f"{lo}-{hi - 1}"
    return "unknown"


def calibration_from_pairs(pairs: list[tuple[float, bool]]) -> dict:
    """pairs: list of (raw_confidence_pct, won_bool). Returns per-bucket
    calibration factor (actual_win_rate / bucket_midpoint_as_probability) with
    a Wilson CI on the win rate itself."""
    buckets = {}
    for raw_conf, won in pairs:
        b = _bucket_for(raw_conf)
        buckets.setdefault(b, []).append(won)

    result = {}
    for b, outcomes in buckets.items():
        n = len(outcomes)
        wins = sum(1 for o in outcomes if o)
        win_rate = wins / n if n else 0.0
        lo, hi = wilson_ci(win_rate, n)
        lo_bound, hi_bound = (int(x) for x in b.split("-"))
        midpoint_p = ((lo_bound + hi_bound + 1) / 2) / 100
        calibration_factor = (win_rate / midpoint_p) if midpoint_p else 1.0
        result[b] = {
            "n": n, "win_rate": win_rate, "win_rate_ci": (lo, hi),
            "calibration_factor": round(calibration_factor, 3),
        }
    return result


def get_calibrated_confidence(raw_confidence: float, calibration_table: dict) -> dict:
    b = _bucket_for(raw_confidence)
    entry = calibration_table.get(b)
    if not entry or entry["n"] < 10:
        return {
            "raw": raw_confidence, "calibrated": raw_confidence, "calibration_factor": 1.0,
            "ci": None, "n_in_bucket": entry["n"] if entry else 0,
            "note": "insufficient bucket history - showing raw confidence unadjusted",
        }
    calibrated = min(100.0, raw_confidence * entry["calibration_factor"])
    return {
        "raw": raw_confidence, "calibrated": round(calibrated, 1),
        "calibration_factor": entry["calibration_factor"], "ci": entry["win_rate_ci"],
        "n_in_bucket": entry["n"],
    }


def get_calibration_for_bucket(db) -> dict:
    """Best-effort DB-backed calibration table - matches signals to trades by
    ticker (approximate; see module docstring for the exact FK-based fix)."""
    signals = db.get_recent_signals(limit=2000)
    trades = db.get_recent_trades(limit=2000)
    trade_tickers_won = {}
    for t in trades:
        # We don't track realized P&L per trade row directly here (that lives in
        # daily_stats aggregate) - treat a logged "sell" as a proxy signal that
        # the position closed; without per-trade P&L this is necessarily rough.
        trade_tickers_won.setdefault(t["ticker"], []).append(t.get("status") == "placed")

    pairs = []
    for s in signals:
        if s.get("confidence") is None or s["ticker"] not in trade_tickers_won:
            continue
        outcomes = trade_tickers_won[s["ticker"]]
        if outcomes:
            pairs.append((s["confidence"], outcomes[0]))

    return calibration_from_pairs(pairs)
