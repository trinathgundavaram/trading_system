"""Layer 1: Market Context Gate (dict-based, Phase 1 rewrite).
Evaluates overall market conditions. If score < 40, skip entire scan cycle.
Separate from per-ticker analysis.

REPLACES the old dot-access version of this file, which was only ever
imported by the dead engine/rules_engine.py (not part of the live pipeline -
the live market gate was engine/market_context.py's evaluate_market_gate()).
This new version is dict-based to match engine/ticker_data_adapter.py's
market_to_dict() output and is what scheduler.py now calls in addition to
evaluate_market_gate() - see scheduler.py for how the two combine.

BREADTH DESIGN (2026-07-15 redesign):
Previously a single-indicator breadth check (McClellan < -70 OR A/D < 0.30)
hard-blocked the entire scan before any ticker was even fetched. This was too
aggressive for two reasons:
  1. The screener's job is to find candidates, not decide on buys. Even on a
     weak breadth day you want to know that NVDA or LLY is showing unusual
     relative strength — that information is valuable whether or not you act on
     it today.
  2. An A/D ratio of exactly 0.00 from an 11-ETF proxy is suspicious as a
     genuine reading (see engine/market_breadth.py's ad_ratio_suspect flag) —
     using it as a hard block risks silencing the whole scan on a data artifact.

New design:
  - Breadth is now a CONTINUOUS SCORE MODIFIER (tiered: excellent→-10pts,
    good→0, weak→-15pts, very_weak→-25pts, panic→-40pts) applied to the
    market score. Weak breadth degrades the score; it doesn't auto-block.
  - The screener (engine/screener.py) is NOT affected by this gate and
    continues discovering candidates regardless of breadth — breadth already
    influences SCORING through the MARKET_BREADTH bucket and dynamic threshold.
  - HARD BLOCK is reserved for a genuine multi-signal crisis: ALL FOUR of
    (McClellan < -70 AND A/D < 0.30 AND VIX > 35 AND SPY below 200DMA) must
    agree. Any single indicator alone is too noisy. This is consistent with
    how regime_engine.py's CRISIS mode already requires multiple signals.
  - A/D ratio of exactly 0.00 is flagged as suspect (see ad_ratio_suspect in
    market_breadth.py) and is treated as a data-quality issue rather than a
    confirmed genuine reading for the hard-block threshold.
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MarketGateResult:
    can_trade: bool
    market_score: float   # 0-100
    reason: str
    blocks: list = field(default_factory=list)
    breadth_tier: str = "unknown"   # excellent/good/weak/very_weak/panic — for annotation and dynamic threshold


def _breadth_tier(mcclellan: float, ad_ratio: float) -> tuple[str, float]:
    """Returns (tier_name, score_penalty).
    Tiers are assessed on the COMBINATION of McClellan and A/D, not either alone.
    A suspect A/D (0.00 from market_breadth.py) is treated as 0.50 (neutral)
    to avoid silencing the scan on a data-quality artifact.
    score_penalty: points to subtract from the 100-pt market score."""
    # Treat an extreme A/D as potentially unreliable — if truly 0.0 or 1.0,
    # the WARNING already appears in engine/market_breadth.py. Clip to a
    # "bad but plausible" range so a single-ETF-data artifact doesn't push
    # us into the panic tier on its own.
    ad_clipped = max(0.10, min(0.90, ad_ratio)) if ad_ratio in (0.0, 1.0) else ad_ratio

    if mcclellan >= 30 and ad_clipped >= 0.65:
        return "excellent", 0.0     # breadth participating strongly — no penalty
    if mcclellan >= 0 and ad_clipped >= 0.50:
        return "good", 0.0          # breadth neutral/positive — no penalty
    if mcclellan < -70 and ad_clipped < 0.30:
        return "panic", 40.0        # true breadth panic — heavy penalty
    if mcclellan < -30 or ad_clipped < 0.40:
        return "very_weak", 25.0    # clearly deteriorating — significant penalty
    # mcclellan slightly negative OR ad slightly below 0.50
    return "weak", 15.0             # soft headwind — moderate penalty


def evaluate(market_data: dict, config: dict) -> MarketGateResult:
    score = 100.0
    blocks = []
    risk_cfg = config["risk"][config["risk_level"]]

    # VIX check
    vix = market_data.get("vix", 18)
    vix_max = risk_cfg.get("vix_entry_max", 27)
    if vix > vix_max:
        score -= 40
        blocks.append(f"VIX {vix:.1f} > max {vix_max}")
    elif vix > vix_max * 0.85:
        score -= 20
        blocks.append(f"VIX {vix:.1f} elevated (>85% of max {vix_max})")

    # F&G check
    fg = market_data.get("fg_score", 50)
    fg_min = risk_cfg.get("fg_min", 20)
    fg_max = risk_cfg.get("fg_max", 85)
    if fg < fg_min or fg > fg_max:
        score -= 20
        blocks.append(f"F&G {fg} outside range {fg_min}-{fg_max}")

    # Macro blackout
    macro_event = market_data.get("upcoming_macro_event", "")
    blackout_setting = risk_cfg.get("macro_blackout", "fomc_nfp")
    if macro_event and _is_blackout(macro_event, blackout_setting):
        score -= 30
        blocks.append(f"Macro blackout: {macro_event}")

    # Crisis mode — regime engine already requires multiple signals to declare
    # CRISIS, so this is a composite gate, not a single-indicator block.
    from engine.regime_engine import current_state
    regime = current_state()
    if regime and regime.crisis_active:
        return MarketGateResult(False, 0.0, "Crisis mode — no trading", ["CRISIS"], "panic")

    # Breadth: tiered score modifier (NOT a single-indicator hard block).
    ad_ratio = market_data.get("ad_ratio", 0.5)
    mcclellan = market_data.get("mcclellan", 0)
    ad_suspect = market_data.get("ad_ratio_suspect", False)
    tier, breadth_penalty = _breadth_tier(mcclellan, ad_ratio)

    if breadth_penalty > 0:
        score -= breadth_penalty
        suspect_note = " (A/D reading suspect — possibly incomplete data)" if ad_suspect else ""
        blocks.append(f"Breadth {tier}: McClellan {mcclellan:.0f}, A/D {ad_ratio:.2f}{suspect_note}")

    # Multi-signal hard block: ALL FOUR conditions must agree simultaneously.
    # Any single indicator alone is too noisy to justify halting the whole scan.
    # Note: ad_ratio == 0.0 is treated as suspect (see _breadth_tier clipping
    # above), so a data artifact alone cannot satisfy the A/D leg of this gate.
    spy_vs_200dma = market_data.get("spy_vs_200dma", 1.0)  # >1 = above, <1 = below
    multi_signal_crisis = (
        mcclellan < -70
        and ad_ratio < 0.30 and not ad_suspect
        and vix > 35
        and spy_vs_200dma < 1.0
    )
    if multi_signal_crisis:
        return MarketGateResult(
            False, 0.0,
            f"Multi-signal breadth crisis: McClellan {mcclellan:.0f}, A/D {ad_ratio:.2f}, "
            f"VIX {vix:.1f}, SPY below 200DMA",
            ["MULTI_SIGNAL_CRISIS"],
            "panic",
        )

    # Matches the docstring's own rule ("if score < 40, skip")
    can_trade = score >= 40
    reason = f"Market score: {score:.0f}/100 [{tier} breadth]" + (
        f" — {'; '.join(blocks)}" if blocks else " — clear"
    )

    return MarketGateResult(can_trade, score, reason, blocks, tier)


def _is_blackout(event: str, setting: str) -> bool:
    event_lower = event.lower()
    if setting == "all":
        return any(k in event_lower for k in ["fomc", "cpi", "nfp", "gdp", "pce"])
    if setting == "cpi_fomc_nfp":
        return any(k in event_lower for k in ["fomc", "cpi", "nfp"])
    if setting == "fomc_nfp":
        return any(k in event_lower for k in ["fomc", "nfp"])
    if setting == "fomc":
        return "fomc" in event_lower
    return False
