"""Position Health Score (0-100) for each open position - a self-contained
read on "is this position behaving the way a good position should", used as
ONE of the six input buckets to the unified Exit Score
(rules/exit_scorer.py's POSITION_HEALTH bucket), not a competing score.

This used to take exit_score as an input (a "lower exit score = healthier"
component) - that created a circular dependency once the Exit Score engine
itself wanted to use Position Health as one of ITS inputs (health needs
exit_score, exit_score needs health). Removed: health is now computed
independently from the position's own P&L/EV/RS/volume/AVWAP/breadth/time
behavior, and the Exit Score engine calls this first, then folds the result
in as its own POSITION_HEALTH bucket. The dropped component's 20% weight was
redistributed below.

`position` is expected to have None values already stripped by the caller
(engine/position_management.py's _clean() helper), same note as
stop_state_machine.py.
"""
from dataclasses import dataclass, field


@dataclass
class PositionHealth:
    score: float              # 0-100
    label: str                # STRONG_HOLD | HOLD | MONITOR | REDUCE | EXIT
    action: str                # hold | tighten | reduce | exit
    components: dict = field(default_factory=dict)
    recommendation: str = ""


def calculate(position: dict, ticker_data: dict, market_data: dict) -> PositionHealth:
    components = {}

    entry = position["entry_price"]
    price = ticker_data.get("price", entry)

    # Component 1: P&L trend (25%, was 20% - absorbed part of the removed
    # exit_score component's weight, since P&L trend is the most direct
    # self-contained read on "is this position behaving well")
    pnl_pct = (price - entry) / entry * 100 if entry else 0.0
    prev_pnl = position.get("prev_cycle_pnl_pct", pnl_pct)
    pnl_trend = pnl_pct - prev_pnl
    components["pnl_trend"] = min(100, max(0, 50 + pnl_trend * 10))

    # Component 2: Position EV (20%, was 15%)
    ev = _calculate_position_ev(position, ticker_data)
    ev_pts = min(100, max(0, 50 + ev * 15))
    components["position_ev"] = ev_pts

    # Component 3: RS trend (20%, was 15%) - rs_percentile is a placeholder
    # (50, neutral) in engine/ticker_data_adapter.py until real relative-
    # strength data exists
    rs_pct = ticker_data.get("rs_percentile", 50)
    entry_rs = position.get("entry_rs_percentile", rs_pct)
    rs_delta = rs_pct - entry_rs
    components["rs_trend"] = min(100, max(0, 50 + rs_delta * 2))

    # Component 4: Volume behavior (10%)
    rvol = ticker_data.get("rvol_quality_score", 50)
    components["volume"] = rvol

    # Component 5: AVWAP relationship (10%) - avwap_earnings is a placeholder
    # (0.0) until a real anchored-VWAP calculation is wired up
    avwap = ticker_data.get("avwap_earnings", 0)
    if avwap:
        avwap_delta_pct = (price - avwap) / avwap * 100
        components["avwap"] = min(100, max(0, 50 + avwap_delta_pct * 5))
    else:
        components["avwap"] = 60  # neutral

    # Component 6: Breadth support (10%, was 5%)
    ad_ratio = market_data.get("ad_ratio", 0.5)
    entry_ad = position.get("entry_ad_ratio", ad_ratio)
    ad_delta = ad_ratio - entry_ad
    components["breadth"] = min(100, max(0, 50 + ad_delta * 100))

    # Component 7: Time decay (5%)
    days_held = position.get("days_held", 0)
    profit_r = position.get("current_profit_r", 0)
    expected_hold = 5  # default swing trade target
    effective_days = max(0, days_held - profit_r * 2)
    decay = max(0, 100 - (effective_days / expected_hold * 50))
    components["time_decay"] = decay

    weights = {"pnl_trend": 0.25, "position_ev": 0.20,
               "rs_trend": 0.20, "volume": 0.10, "avwap": 0.10,
               "breadth": 0.10, "time_decay": 0.05}

    score = sum(components[k] * weights[k] for k in weights)

    if score >= 90:
        label, action = "STRONG HOLD", "hold"
    elif score >= 75:
        label, action = "HOLD", "hold"
    elif score >= 60:
        label, action = "MONITOR", "tighten"
    elif score >= 40:
        label, action = "REDUCE", "reduce"
    else:
        label, action = "EXIT", "exit"

    rec = f"Position health {score:.0f}/100 ({label})"

    return PositionHealth(score=score, label=label, action=action,
                           components=components, recommendation=rec)


def _calculate_position_ev(position: dict, ticker_data: dict) -> float:
    """Forward EV from current price to stop vs. target."""
    current = ticker_data.get("price", position["entry_price"])
    stop = position.get("current_stop_price") or position["entry_price"] * 0.95
    target = position.get("current_target_price") or position["entry_price"] * 1.10
    p_win = position.get("entry_p_win") or 0.60

    if current <= 0:
        return 0.0
    risk = (current - stop) / current
    reward = (target - current) / current

    return (p_win * reward) - ((1 - p_win) * risk)
