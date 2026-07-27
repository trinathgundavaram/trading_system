"""Position Sizing Engine - Priority 1 architectural gap identified in the
deployment review: this codebase had a Position Health Score, an Exit Score,
an EV engine with Wilson-CI confidence, and a regime-aware threshold system,
but every qualifying BUY got the exact same flat dollar amount
(config.yaml's trading.trade_size_usd) regardless of how strong the setup
was. "Not every qualifying trade deserves the same capital."

Pipeline (matches the review's proposed shape):
    Buy Score -> Expected Value -> Confidence -> Volatility -> Portfolio Risk -> Position Size

This module NEVER places an order or moves money - same posture as every
other engine/ module in this codebase (see README: "Trades are never placed
from Python"). It only computes a SUGGESTED size (as a % of the planned
allocation, config.yaml's trading.trade_size_usd) that gets rendered into
output/trade_prompt.md next to the buy signal, for a human (or Claude
Desktop + the robinhood-trading MCP) to act on.

All multipliers below are config-driven (config.yaml's position_sizing
section) rather than hardcoded literals - see Priority 3 in the same
deployment review ("config-driven weights instead of hardcoded literals").
"""
from dataclasses import dataclass, field

_DEFAULT_SCORE_TIERS = [
    {"min_score": 85, "size_pct": 100},
    {"min_score": 75, "size_pct": 70},
    {"min_score": 65, "size_pct": 40},
    {"min_score": 0, "size_pct": 25},
]
_DEFAULT_EV_CONFIDENCE_MULT = {
    "insufficient": 0.50, "low": 0.70, "moderate": 0.90, "high": 1.00,
}
_DEFAULT_ATR_BANDS = [
    {"max_atr_pct": 2.0, "multiplier": 1.00},
    {"max_atr_pct": 4.0, "multiplier": 0.85},
    {"max_atr_pct": 6.0, "multiplier": 0.65},
    {"max_atr_pct": 999.0, "multiplier": 0.45},
]


@dataclass
class PositionSizeResult:
    applicable: bool                 # False for already-open/vetoed/no-score tickers
    suggested_size_pct: float        # 0-100+, % of the planned allocation (trading.trade_size_usd)
    suggested_dollar_amount: float   # suggested_size_pct/100 * base_allocation, capped at risk.max_position_size_usd
    base_allocation_usd: float
    factors: dict = field(default_factory=dict)   # each named multiplier, for transparency in the prompt
    reasons: list = field(default_factory=list)    # human-readable one-liners, most-influential first
    tier_label: str = ""             # e.g. "HIGH" / "MEDIUM" / "LOW" conviction, from the score tier alone


def _cfg_section(cfg: dict) -> dict:
    return (cfg or {}).get("position_sizing", {}) or {}


def _get_portfolio_total(db, simulated=True) -> float:
    """Calculate total value of all open positions in portfolio.

    Args:
        db: Database/storage object with get_all_positions(simulated) method
        simulated: True for paper book, False for live book

    Returns:
        Sum of all open position dollar amounts (excludes closed positions)
    """
    if db is None:
        return 0.0

    try:
        positions = db.get_all_positions(simulated=simulated)
        if not positions:
            return 0.0

        # Sum only open positions (closed_at is None)
        total = sum(
            float(p.get("dollar_amount", 0.0))
            for p in positions
            if p.get("closed_at") is None
        )
        return total
    except Exception:
        return 0.0


def _score_tier(pct_score: float, tiers: list) -> tuple:
    """tiers sorted descending by min_score by config convention - returns
    (size_pct, label) for the first tier the score clears."""
    sorted_tiers = sorted(tiers, key=lambda t: t["min_score"], reverse=True)
    for t in sorted_tiers:
        if pct_score >= t["min_score"]:
            label = "HIGH" if t["min_score"] >= 80 else "MEDIUM" if t["min_score"] >= 65 else "LOW"
            return float(t["size_pct"]), label
    return 25.0, "LOW"


def _ev_confidence_multiplier(ev_result: dict, mult_map: dict) -> tuple:
    if not ev_result or ev_result.get("ev") is None:
        # No real EV lookup yet (fresh pattern DB, or db/ticker not passed to
        # swing_buy_rules.score()) - treat exactly like "insufficient", the
        # same label the EV engine itself uses below its min-occurrence bar.
        conf = "insufficient"
    else:
        conf = ev_result.get("confidence", "insufficient")
    return float(mult_map.get(conf, mult_map.get("insufficient", 0.5))), conf


def _volatility_multiplier(ticker_data: dict, bands: list) -> tuple:
    price = ticker_data.get("price", 0) or 0
    atr = ticker_data.get("atr", 0) or 0
    atr_pct = (atr / price * 100) if price else 0.0
    sorted_bands = sorted(bands, key=lambda b: b["max_atr_pct"])
    for b in sorted_bands:
        if atr_pct <= b["max_atr_pct"]:
            return float(b["multiplier"]), atr_pct
    return float(sorted_bands[-1]["multiplier"]) if sorted_bands else 1.0, atr_pct


def _regime_multiplier(regime) -> float:
    """Reuses engine/regime_engine.py's existing transition_size_scalar() -
    that function already existed (transition-probability -> size modifier)
    but was never actually called from anywhere before this module, since
    nothing computed a real per-ticker suggested size before now."""
    if regime is None:
        return 1.0
    try:
        from engine.regime_engine import transition_size_scalar
        return transition_size_scalar(regime)
    except Exception:
        return 1.0


def calculate(buy_result, score_result, ticker_data: dict, regime, cfg: dict,
              portfolio_risk_result=None, mode: str = "SWING", db=None) -> PositionSizeResult:
    """
    buy_result: rules/swing_buy_rules.py's BuyResultCompat (or equivalent) -
        only buy_result.should_buy / .pct_score are used.
    score_result: rules/swing_buy_rules.py's SwingScoreResult, or None if the
        ticker was vetoed / already an open position / not scored this cycle -
        sizing genuinely doesn't apply to those, so this returns
        applicable=False rather than guessing.
    ticker_data: plain dict from engine/ticker_data_adapter.py's ticker_to_dict().
    regime: RegimeState from engine/regime_engine.py, or None.
    portfolio_risk_result: OPTIONAL engine/portfolio_risk.py PortfolioRiskResult
        for this same candidate - if given, its size_multiplier folds in as
        the final factor (portfolio-level exposure caps this trade's slice of
        capital, on top of the setup's own conviction/EV/volatility). None is
        tolerated (multiplier defaults to 1.0) so this module works standalone.
    mode: "SWING" or "DAY" (2026-07-22, full DAY/SWING/HYBRID separation) -
        the RESOLVED trade_mode (scheduler.py's effective_mode: a HYBRID
        signal has already been classified DAY/SWING by _classify_hybrid_leg
        by the time this is called). "DAY" applies position_sizing's
        day_size_multiplier (default 0.5 - see config.yaml) as one more
        factor in the chain below, since day trading is higher-frequency and
        each individual trade should risk less of the account than the same
        setup traded as a swing position. Case-insensitive; defaults to
        "SWING" (multiplier 1.0, i.e. no change) for any caller that doesn't
        pass a mode - existing callers/tests are unaffected.
    db: OPTIONAL storage/database object for dynamic position sizing. If provided
        and use_dynamic_sizing is True, position size scales with portfolio growth
        as: base_allocation = portfolio_total × position_size_pct_of_portfolio / 100
    """
    scfg = _cfg_section(cfg)
    trading_cfg = (cfg or {}).get("trading", {})
    fallback_allocation = float(trading_cfg.get("trade_size_usd", 100))

    # Dynamic sizing: scales position with portfolio growth
    use_dynamic_sizing = trading_cfg.get("use_dynamic_sizing", False)
    base_allocation = fallback_allocation

    if use_dynamic_sizing and db is not None:
        portfolio_total = _get_portfolio_total(db, simulated=True)
        if portfolio_total > 0:
            position_pct = float(trading_cfg.get("position_size_pct_of_portfolio", 3.0))
            base_allocation = portfolio_total * position_pct / 100.0
        else:
            # Empty portfolio (first trade): fall back to static sizing
            base_allocation = fallback_allocation
    max_position_usd = float((cfg or {}).get("risk", {}).get("max_position_size_usd", base_allocation))

    if not scfg.get("enabled", True) or buy_result is None or not getattr(buy_result, "should_buy", False) \
            or score_result is None:
        return PositionSizeResult(
            applicable=False, suggested_size_pct=0.0, suggested_dollar_amount=0.0,
            base_allocation_usd=base_allocation,
            reasons=["Sizing not applicable (no active BUY score this cycle)."],
        )

    pct_score = getattr(buy_result, "pct_score", 0.0)

    tiers = scfg.get("score_tiers") or _DEFAULT_SCORE_TIERS
    ev_mult_map = scfg.get("ev_confidence_multiplier") or _DEFAULT_EV_CONFIDENCE_MULT
    atr_bands = scfg.get("volatility_atr_pct_bands") or _DEFAULT_ATR_BANDS
    min_pct = float(scfg.get("min_size_pct", 20))
    max_pct = float(scfg.get("max_size_pct", 100))

    base_pct, tier_label = _score_tier(pct_score, tiers)
    ev_mult, ev_conf_label = _ev_confidence_multiplier(getattr(score_result, "ev_result", None), ev_mult_map)
    vol_mult, atr_pct = _volatility_multiplier(ticker_data, atr_bands)
    regime_mult = _regime_multiplier(regime)
    portfolio_mult = 1.0
    if portfolio_risk_result is not None:
        portfolio_mult = float(getattr(portfolio_risk_result, "size_multiplier", 1.0))

    # Execution Quality (rules/execution_quality.py) - already folded a small
    # adjustment into the buy SCORE itself (see rules/swing_buy_rules.py);
    # its size_multiplier here is a separate, independent lever on top -
    # poor execution quality (thin liquidity, high estimated slippage) is a
    # real capital-at-risk concern even for a high-scoring setup.
    execution_quality_result = getattr(score_result, "execution_quality", None)
    execution_mult = 1.0
    if execution_quality_result is not None:
        execution_mult = float(getattr(execution_quality_result, "size_multiplier", 1.0))

    # DAY-mode size multiplier (2026-07-22, full DAY/SWING/HYBRID separation)
    # - see this function's `mode` docstring above and config.yaml's
    # position_sizing.day_size_multiplier comment.
    day_mult = 1.0
    if str(mode or "SWING").upper() == "DAY":
        day_mult = float(scfg.get("day_size_multiplier", 0.5))

    raw_pct = base_pct * ev_mult * vol_mult * regime_mult * portfolio_mult * execution_mult * day_mult
    final_pct = max(min_pct, min(max_pct, raw_pct)) if raw_pct > 0 else 0.0

    dollar_amount = round(min(base_allocation * final_pct / 100.0, max_position_usd), 2)

    reasons = [
        f"Score tier {pct_score:.0f}% -> base {base_pct:.0f}% ({tier_label} conviction)",
        f"EV confidence '{ev_conf_label}' -> x{ev_mult:.2f}",
        f"Volatility (ATR {atr_pct:.1f}% of price) -> x{vol_mult:.2f}",
        f"Regime transition-risk -> x{regime_mult:.2f}",
    ]
    if portfolio_risk_result is not None:
        reasons.append(f"Portfolio risk headroom -> x{portfolio_mult:.2f}")
        reasons.extend(getattr(portfolio_risk_result, "reasons", []) or [])
    if execution_quality_result is not None:
        reasons.append(f"Execution quality '{execution_quality_result.tier}' -> x{execution_mult:.2f}")
    if day_mult != 1.0:
        reasons.append(f"DAY-mode leg -> x{day_mult:.2f} (higher-frequency, smaller per-trade risk)")
    if raw_pct != final_pct and raw_pct > 0:
        reasons.append(f"Clamped to [{min_pct:.0f}%, {max_pct:.0f}%] of planned allocation")
    if portfolio_mult <= 0.0:
        reasons.insert(0, "Portfolio risk multiplier is 0 - suggested size reduced to $0 (see portfolio risk reasons)")

    return PositionSizeResult(
        applicable=True,
        suggested_size_pct=round(final_pct, 1),
        suggested_dollar_amount=dollar_amount,
        base_allocation_usd=base_allocation,
        factors={
            "score_tier_pct": base_pct, "ev_confidence": ev_conf_label, "ev_multiplier": ev_mult,
            "atr_pct_of_price": round(atr_pct, 2), "volatility_multiplier": vol_mult,
            "regime_multiplier": regime_mult, "portfolio_multiplier": portfolio_mult,
            "execution_quality_multiplier": execution_mult, "day_mode_multiplier": day_mult,
        },
        reasons=reasons,
        tier_label=tier_label,
    )
