"""Win rate, profit factor, Sharpe, and equity-curve style metrics, broken down
by whatever dimension you want (day-of-week, regime, sector, ...). Operates on
closed pattern_database rows (which carry outcome_pct + arbitrary features) so
it works before a dedicated "closed trades" reporting table exists."""
import math
from datetime import datetime

from analytics.confidence_intervals import wilson_ci


def win_rate_by(patterns: list[dict], group_field: str) -> dict:
    """group_field is looked up in each pattern's `features` dict (e.g.
    'day_of_week', 'sector', 'regime')."""
    groups = {}
    for p in patterns:
        if p.get("outcome_pct") is None:
            continue
        key = str(p["features"].get(group_field, "unknown"))
        groups.setdefault(key, []).append(p["outcome_pct"])

    result = {}
    for key, outcomes in groups.items():
        n = len(outcomes)
        wins = sum(1 for o in outcomes if o > 0)
        win_rate = wins / n if n else 0.0
        ci = wilson_ci(win_rate, n)
        result[key] = {
            "n": n, "win_rate": round(win_rate, 3), "win_rate_ci": ci,
            "avg_outcome_pct": round(sum(outcomes) / n, 2) if n else 0.0,
        }
    return result


def profit_factor(outcomes_pct: list[float]) -> float | None:
    """Gross win / gross loss. ``None`` when there is no loss to divide by.

    Returned None rather than ``float("inf")`` as of 2026-07-26. The infinity
    was mathematically honest and operationally fatal: Starlette's JSONResponse
    calls ``json.dumps(..., allow_nan=False)``, so ANY route whose payload
    contained it raised ``ValueError: Out of range float values are not JSON
    compliant`` and answered HTTP 500. ``/api/analytics/performance`` therefore
    500'd for every book with no losing trade, and the Performance tab - which
    had no error handling - sat on "Loading..." forever.

    Reachable on a small sample, and note the grouping: gross_loss sums
    ``o <= 0``, so a single break-even trade among winners triggers it too.
    A freshly installed version whose first closed trade wins hits it
    immediately, which is exactly when someone is watching that tab.

    None is also the more truthful answer. "Infinite profit factor" reads as a
    spectacular result; it means "not enough evidence to compute a ratio", and
    callers must render it as such rather than as a number.
    """
    gross_win = sum(o for o in outcomes_pct if o > 0)
    gross_loss = abs(sum(o for o in outcomes_pct if o <= 0))
    if gross_loss == 0:
        return None if gross_win > 0 else 0.0
    return gross_win / gross_loss


def sharpe_ratio(outcomes_pct: list[float], risk_free_pct: float = 0.0) -> float:
    if len(outcomes_pct) < 2:
        return 0.0
    mean_r = sum(outcomes_pct) / len(outcomes_pct) - risk_free_pct
    variance = sum((r - mean_r) ** 2 for r in outcomes_pct) / (len(outcomes_pct) - 1)
    std = math.sqrt(variance)
    return (mean_r / std) if std else 0.0


def equity_curve(patterns: list[dict], starting_equity: float = 10_000) -> list[dict]:
    """Sequential equity curve assuming each trade risks a fixed % - simplified
    (doesn't compound position sizing), good enough to visualize drawdown shape."""
    closed = sorted(
        (p for p in patterns if p.get("outcome_pct") is not None),
        key=lambda p: p.get("recorded_at", ""),
    )
    equity = starting_equity
    curve = []
    peak = equity
    for p in closed:
        equity *= (1 + p["outcome_pct"] / 100)
        peak = max(peak, equity)
        drawdown_pct = (equity - peak) / peak * 100 if peak else 0.0
        curve.append({
            "timestamp": p.get("recorded_at"), "ticker": p.get("ticker"),
            "equity": round(equity, 2), "drawdown_pct": round(drawdown_pct, 2),
        })
    return curve


def performance_by_regime(patterns: list[dict]) -> dict:
    """The key diagnostic table from the spec: bull/bear/choppy performance
    kept strictly separate, since a strategy that only works in one regime
    needs a regime-aware gate, not a single blended win rate."""
    return win_rate_by(patterns, "regime")


def exit_efficiency(patterns: list[dict], baseline_fn) -> dict:
    """Compares actual exit outcome_pct against a baseline exit rule (e.g.
    'fixed 2R', 'ATR trail', 'hold until next signal') supplied as
    baseline_fn(pattern) -> float (the baseline's hypothetical outcome_pct).
    Returns how much better/worse the actual exit logic did on average."""
    diffs = []
    for p in patterns:
        if p.get("outcome_pct") is None:
            continue
        try:
            baseline = baseline_fn(p)
        except Exception:
            continue
        diffs.append(p["outcome_pct"] - baseline)
    if not diffs:
        return {"n": 0, "avg_edge_pct": 0.0}
    return {"n": len(diffs), "avg_edge_pct": round(sum(diffs) / len(diffs), 3)}
