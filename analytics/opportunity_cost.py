"""Tracks signals the 10-step framework rejected, and simulates what would
have happened had they been taken - so you can tell "the threshold is too
strict" apart from "the threshold is correctly filtering junk"."""


def track_rejected_signal(db, ticker: str, reject_stage: str, reject_reason: str,
                           score_at_rejection: float, price_at_rejection: float) -> int:
    return db.log_rejected_signal(ticker, reject_stage, reject_reason,
                                   score_at_rejection, price_at_rejection)


def simulate_rejected_outcome(rejected: dict, later_prices: list[dict],
                               hold_days: int = 5) -> float | None:
    """later_prices: OHLCV rows (dicts with 'date'/'close') AFTER the rejection
    timestamp, e.g. from mcp_clients.yfinance_mcp.get_all(ticker)['daily_ohlcv'].
    Returns the % move over `hold_days` bars if enough data is available,
    else None (caller should skip logging rather than guess)."""
    if not later_prices or len(later_prices) < hold_days:
        return None
    entry_price = rejected["price_at_rejection"]
    if not entry_price:
        return None
    exit_price = later_prices[min(hold_days, len(later_prices) - 1) - 1]["close"]
    return (exit_price - entry_price) / entry_price * 100


def opportunity_cost_report(db) -> dict:
    """Aggregates simulated outcomes by reject_stage - tells you which gate is
    costing the most missed upside vs correctly avoiding losers."""
    rejected = db.get_rejected_signals(unsimulated_only=False, limit=2000)
    by_stage = {}
    for r in rejected:
        if r.get("simulated_outcome_pct") is None:
            continue
        stage = r["reject_stage"] or "unknown"
        by_stage.setdefault(stage, []).append(r["simulated_outcome_pct"])

    report = {}
    for stage, outcomes in by_stage.items():
        n = len(outcomes)
        avg = sum(outcomes) / n if n else 0.0
        missed_winners = sum(1 for o in outcomes if o > 0)
        report[stage] = {
            "n": n, "avg_missed_pct": round(avg, 2),
            "would_have_won_pct": round(missed_winners / n * 100, 1) if n else 0.0,
        }
    return report
