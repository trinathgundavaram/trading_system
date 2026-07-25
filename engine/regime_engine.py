"""CANONICAL REGIME ENGINE — single shared source of truth.
All modules import RegimeEngine.current_state(), never recalculate independently.

Module-level `_current` is process-global by design (matches the spec's "one
shared instance" requirement) - scheduler.py calls calculate() once per cycle
before the per-ticker loop, and every downstream module (dynamic_thresholds,
swing_buy_rules, market_filters, pattern_features) reads current_state()
rather than recomputing regime from raw inputs.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class RegimeState:
    bull_pct: float           # 0-100
    bear_pct: float           # 0-100
    choppy_pct: float         # 0-100
    transition_probability: float   # 0-100 (independent — can be high in any regime)
    crisis_active: bool
    dominant_regime: str      # BULL | BEAR | CHOPPY | CRISIS
    confidence_gap: float     # dominant% - second_highest%
    confidence_level: str     # VERY_HIGH | HIGH | MEDIUM | LOW | VERY_LOW
    confidence_score: float   # 0-100
    regime_version: str       # e.g. "regime_2.4"
    calculated_at: datetime


_current: Optional[RegimeState] = None


def calculate(spy_price: float, spy_sma50: float, spy_sma200: float,
              vix: float, fg_score: float, ad_ratio: float) -> RegimeState:
    global _current

    bull_ev = bear_ev = choppy_ev = 0.0

    # SPY vs SMA200 (+30pts)
    if spy_price > spy_sma200 * 1.005:
        bull_ev += 30
    elif spy_price < spy_sma200 * 0.995:
        bear_ev += 30
    else:
        choppy_ev += 20

    # SPY vs SMA50 (+20pts)
    if spy_price > spy_sma50 * 1.003:
        bull_ev += 20
    elif spy_price < spy_sma50 * 0.997:
        bear_ev += 20
    else:
        choppy_ev += 25

    # VIX (+25pts)
    if vix < 15:
        bull_ev += 25
    elif vix > 25:
        bear_ev += 25
    else:
        choppy_ev += 30

    # F&G (+20pts)
    if fg_score > 60:
        bull_ev += 20
    elif fg_score < 30:
        bear_ev += 20
    else:
        choppy_ev += 15

    # A/D ratio (+25pts)
    if ad_ratio > 0.60:
        bull_ev += 25
    elif ad_ratio < 0.35:
        bear_ev += 25
    else:
        choppy_ev += 15

    total = bull_ev + bear_ev + choppy_ev or 1
    bull_pct = (bull_ev / total) * 100
    bear_pct = (bear_ev / total) * 100
    choppy_pct = (choppy_ev / total) * 100

    # Transition probability (simple version — 3 signals, spec calls it "5
    # signals" but only specifies these 3 in the reference implementation)
    transition_signals = 0
    if bull_pct > 0 and _current and abs(bull_pct - _current.bull_pct) > 5:
        transition_signals += 1
    if ad_ratio < 0.45 and spy_price > spy_sma200:
        transition_signals += 1  # breadth diverging
    if 18 < vix < 25 and bull_pct > 50:
        transition_signals += 1  # VIX rising in bull
    transition_probability = min(100.0, transition_signals * 25.0)

    # Crisis override
    crisis_active = (vix > 30 and fg_score < 20)

    if crisis_active:
        dominant = "CRISIS"
    else:
        dominant = max([("BULL", bull_pct), ("BEAR", bear_pct), ("CHOPPY", choppy_pct)],
                        key=lambda x: x[1])[0]

    sorted_pcts = sorted([bull_pct, bear_pct, choppy_pct], reverse=True)
    confidence_gap = sorted_pcts[0] - sorted_pcts[1]

    if confidence_gap > 60:
        cl = "VERY_HIGH"
    elif confidence_gap > 45:
        cl = "HIGH"
    elif confidence_gap > 25:
        cl = "MEDIUM"
    elif confidence_gap > 10:
        cl = "LOW"
    else:
        cl = "VERY_LOW"

    confidence_score = min(100.0, confidence_gap * 1.5)

    _current = RegimeState(
        bull_pct=bull_pct, bear_pct=bear_pct, choppy_pct=choppy_pct,
        transition_probability=transition_probability,
        crisis_active=crisis_active, dominant_regime=dominant,
        confidence_gap=confidence_gap, confidence_level=cl,
        confidence_score=confidence_score,
        regime_version="regime_2.4",
        calculated_at=datetime.utcnow(),
    )
    return _current


def current_state() -> Optional[RegimeState]:
    return _current


def transition_size_scalar(state: RegimeState) -> float:
    """Transition probability -> position-size modifier."""
    tp = state.transition_probability
    if tp < 20:
        return 1.00
    elif tp < 40:
        return 0.90
    elif tp < 60:
        return 0.75
    elif tp < 80:
        return 0.60
    else:
        return 0.40


def regime_threshold_adj(state: RegimeState) -> float:
    """Returns % points to add to base buy-score threshold. Capped by the
    caller (dynamic_thresholds.py) at +20 total combined with other adjustments.

    2026-07-15 fix (zero-trades-in-a-bull-market audit): the old formula
    `bear*0.13 + choppy*0.08` could only ever RAISE the bar - even a
    confirmed BULL regime (e.g. 62.5% bull / 37.5% choppy, VIX 16) added
    +3.0%, because bull dominance earned no credit at all. Regime
    probabilities always sum to 100, so some choppy % is nearly always
    present and the adjustment was structurally positive. Now bull_pct
    earns a credit (-0.05/pt), so a clean bull LOWERS the threshold
    (90/0/10 -> -3.7%) while bear/choppy still raise it (0/60/40 -> +11%).
    Floor of -5.0 keeps the credit modest; crisis override unchanged."""
    if state.crisis_active:
        return 20.0
    adj = (state.bear_pct * 0.13) + (state.choppy_pct * 0.08) - (state.bull_pct * 0.05)
    return max(-5.0, adj)
