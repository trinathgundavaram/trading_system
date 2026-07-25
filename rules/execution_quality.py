"""Execution Quality Score - Priority 5 from the deployment review: "You
improved spread handling, but I'd go one step further with an Execution
Quality Score... that would make execution quality part of the decision
rather than a simple veto."

Combines FOUR components into one 0-100 score:
  1. Spread        - reuses rules/spread_quality.py's existing tiered
                      bid/ask evaluation (excellent..veto -> 100..0)
  2. Dollar volume  - avg_volume * price, banded (a $2 spread on a
                      $50M/day-dollar-volume name is a very different risk
                      than the same spread on a $500K/day name)
  3. Slippage       - a simple microstructure ESTIMATE: half the spread
                      (crossing it) plus a market-impact term scaled by how
                      large the candidate trade is relative to the stock's
                      own average dollar volume. This is a MODEL, not
                      historical fill data - this codebase has no real
                      execution/fill feedback loop from Robinhood yet (fills
                      recorded by engine/live_trader.py and confirm_fill.py
                      are not fed back into this estimate), so there is
                      nothing to calibrate against. §22 (Phase 4) replaces
                      this with the shared execution-cost model. Treat it as
                      directional, not exact - same posture as
                      storage/database.py's get_portfolio_heat().
  4. Liquidity consistency - PROXY from how far today's volume_ratio sits
                      from a "normal" band. ticker_data doesn't carry a
                      multi-day volume series (only avg_volume + today's
                      ratio), so this can't be a true rolling-consistency
                      calculation - it's the closest real signal available
                      without adding a new MCP fetch.

This does NOT replace rules/hard_vetoes.py's SPREAD_WIDE veto or
rules/swing_buy_rules.py's existing spread_penalty deduction - both stay
exactly as they were (still real, still useful, already well-tested). This
module's score_adjustment_pct is a SEPARATE, smaller, additive term folded
into the final buy score by rules/swing_buy_rules.py, carrying the genuinely
NEW information here (dollar volume / slippage / consistency) that spread
alone never captured. It's also exposed as a size multiplier for
engine/position_sizing.py, since poor execution quality is a real capital
risk independent of setup conviction.
"""
from dataclasses import dataclass, field

from rules.spread_quality import evaluate as evaluate_spread

_SPREAD_TIER_SCORE = {"excellent": 100, "good": 80, "acceptable": 55, "warning": 25, "veto": 0}

_DEFAULT_WEIGHTS = {"spread": 0.20, "dollar_volume": 0.35, "slippage": 0.25, "liquidity_consistency": 0.20}

_DEFAULT_DOLLAR_VOLUME_BANDS = [
    {"min_usd": 50_000_000, "score": 100},
    {"min_usd": 15_000_000, "score": 80},
    {"min_usd": 5_000_000, "score": 55},
    {"min_usd": 1_000_000, "score": 25},
    {"min_usd": 0, "score": 0},
]
_DEFAULT_SLIPPAGE_BANDS = [
    {"max_pct": 0.05, "score": 100},
    {"max_pct": 0.15, "score": 80},
    {"max_pct": 0.35, "score": 55},
    {"max_pct": 0.75, "score": 25},
    {"max_pct": 999.0, "score": 0},
]


@dataclass
class ExecutionQualityResult:
    total_score: float          # 0-100
    tier: str                   # EXCELLENT | GOOD | ACCEPTABLE | POOR | VERY_POOR
    components: dict = field(default_factory=dict)
    score_adjustment_pct: float = 0.0   # small additive term for the buy score, config-bounded
    size_multiplier: float = 1.0        # for engine/position_sizing.py
    reasons: list = field(default_factory=list)


def _cfg(cfg: dict) -> dict:
    return (cfg or {}).get("execution_quality", {}) or {}


def _band_score(value: float, bands: list, key_field: str, ascending: bool) -> float:
    sorted_bands = sorted(bands, key=lambda b: b[key_field], reverse=not ascending)
    for b in sorted_bands:
        if (ascending and value <= b[key_field]) or (not ascending and value >= b[key_field]):
            return float(b["score"])
    return float(sorted_bands[-1]["score"]) if sorted_bands else 50.0


def _tier_for_score(score: float) -> str:
    if score >= 85:
        return "EXCELLENT"
    if score >= 65:
        return "GOOD"
    if score >= 45:
        return "ACCEPTABLE"
    if score >= 20:
        return "POOR"
    return "VERY_POOR"


def evaluate(ticker_data: dict, candidate_dollar_amount: float, cfg: dict, mode: str = "swing") -> ExecutionQualityResult:
    ecfg = _cfg(cfg)
    if not ecfg.get("enabled", True):
        return ExecutionQualityResult(total_score=100.0, tier="EXCELLENT", reasons=["Execution quality scoring disabled in config"])

    weights = ecfg.get("weights", _DEFAULT_WEIGHTS)
    price = ticker_data.get("price", 0) or 0
    avg_volume = ticker_data.get("avg_volume", 0) or 0
    avg_dollar_volume = avg_volume * price

    # 1. Spread
    spread_result = evaluate_spread(ticker_data, mode=mode)
    spread_score = _SPREAD_TIER_SCORE.get(spread_result.tier, 50)

    # 2. Dollar volume
    dv_bands = ecfg.get("dollar_volume_bands", _DEFAULT_DOLLAR_VOLUME_BANDS)
    dv_score = _band_score(avg_dollar_volume, dv_bands, "min_usd", ascending=False)

    # 3. Slippage estimate
    spread_pct = spread_result.spread_pct * 100  # spread_quality gives a fraction, e.g. 0.004 -> 0.4%
    participation_factor = float(ecfg.get("slippage_participation_factor", 50.0))
    participation_pct = (candidate_dollar_amount / avg_dollar_volume * 100) if avg_dollar_volume else 1.0
    slippage_pct = (spread_pct / 2) + (participation_pct / 100 * participation_factor)
    slip_bands = ecfg.get("slippage_bands", _DEFAULT_SLIPPAGE_BANDS)
    slip_score = _band_score(slippage_pct, slip_bands, "max_pct", ascending=True)

    # 4. Liquidity consistency (proxy - see module docstring)
    volume_ratio = ticker_data.get("volume_ratio", 1.0) or 1.0
    if 0.6 <= volume_ratio <= 1.8:
        consistency_score = 100.0
    elif 0.3 <= volume_ratio <= 3.0:
        consistency_score = 60.0
    else:
        consistency_score = 25.0

    components = {
        "spread": spread_score, "dollar_volume": dv_score,
        "slippage": slip_score, "liquidity_consistency": consistency_score,
    }
    total = sum(components[k] * weights.get(k, _DEFAULT_WEIGHTS[k]) for k in components)
    tier = _tier_for_score(total)

    adj_bands = ecfg.get("score_adjustment_bounds", {"max_bonus_pct": 3.0, "max_penalty_pct": 8.0})
    # Linear map: 100 -> +max_bonus, 50 -> 0, 0 -> -max_penalty
    if total >= 50:
        adjustment = (total - 50) / 50 * adj_bands.get("max_bonus_pct", 3.0)
    else:
        adjustment = (total - 50) / 50 * adj_bands.get("max_penalty_pct", 8.0)

    size_mult_bounds = ecfg.get("size_multiplier_bounds", {"min": 0.5, "max": 1.0})
    size_multiplier = size_mult_bounds["min"] + (total / 100.0) * (size_mult_bounds["max"] - size_mult_bounds["min"])

    reasons = [
        f"Spread: {spread_result.tier} ({spread_score:.0f}/100)",
        f"Avg $ volume: ${avg_dollar_volume/1e6:.1f}M ({dv_score:.0f}/100)",
        f"Est. slippage {slippage_pct:.2f}% for ${candidate_dollar_amount:.0f} trade ({slip_score:.0f}/100)",
        f"Liquidity consistency (volume_ratio {volume_ratio:.2f}x) -> {consistency_score:.0f}/100",
    ]

    return ExecutionQualityResult(
        total_score=round(total, 1), tier=tier, components=components,
        score_adjustment_pct=round(adjustment, 2), size_multiplier=round(size_multiplier, 2),
        reasons=reasons,
    )
