"""Dynamic threshold calculation — replaces the fixed buy_score_threshold_pct.
Total adjustment CAPPED at +20% above base.
Regime and VIX: use MAX (not sum) to avoid double-counting market stress.
Calendar adjustments: always additive with regime.
Breadth adjustment: tiered additive modifier, consistent with the continuous
  approach introduced in rules/market_filters.py (2026-07-15). A single weak
  breadth reading doesn't block the scan, but it raises the bar to buy:
    excellent (McClellan≥30, A/D≥0.65):  -3%  (lower bar — tailwind)
    good      (McClellan≥0,  A/D≥0.50):   0%  (neutral)
    weak      (either slightly negative):  +5%
    very_weak (McClellan<-30 or A/D<0.40): +10%
    panic     (McClellan<-70 and A/D<0.30): +15%  (hard-block path in
              market_filters.py handles this first; this is a backstop in
              case breadth_data arrives here without going through that gate)
EV bonus: applied after the cap.
"""
from engine.regime_engine import RegimeState, regime_threshold_adj


def _breadth_adj(breadth_data: dict) -> tuple[float, str]:
    """Returns (adj_pct, tier_name) from raw breadth dict.
    Treats A/D of exactly 0.0/1.0 as suspect (clips to 0.10/0.90) so a
    data artifact doesn't push into the panic tier on its own."""
    if not breadth_data:
        return 0.0, "unknown"
    mcclellan = breadth_data.get("mcclellan", 0)
    ad_ratio = breadth_data.get("ad_ratio", 0.5)
    ad_suspect = breadth_data.get("ad_ratio_suspect", False)
    # clip extreme A/D values that are likely data artifacts
    if ad_suspect or ad_ratio in (0.0, 1.0):
        ad_ratio = max(0.10, min(0.90, ad_ratio))

    # Tiers capped at +8, then further reduced to +5/+3 (2026-07-22, Trinath:
    # "didn't select any ticker for 3 days that crossed 45%... huge
    # improvement needed"): the 2026-07-15 review already identified this as
    # DOUBLE-counting breadth (it has its own 11%-weighted MARKET_BREADTH
    # scoring bucket) and capped it at +8 for that reason, but +8 on top of
    # an already-low base (TURBO 50%, AGGRESSIVE 55%) still meaningfully
    # widened the gap to a score ceiling that was independently found to be
    # capped near 48% by data-availability issues (finviz/FMP outages, the
    # lite/promotion catch-22 above) - stacking a second weak-breadth penalty
    # on top of candidates that were already unable to reach the mid-50s made
    # the combination effectively unclearable on any weak-breadth day,
    # regardless of profile. This is a CONSERVATIVE further reduction, not a
    # removal - weak breadth still raises the bar, just not on top of a
    # ceiling that other fixes this same day already raised. The true-panic
    # hard block still lives in rules/market_filters.py's multi-signal crisis
    # gate (unaffected by this change).
    if mcclellan >= 30 and ad_ratio >= 0.65:
        return -3.0, "excellent"
    if mcclellan >= 0 and ad_ratio >= 0.50:
        return 0.0, "good"
    if mcclellan < -70 and ad_ratio < 0.30:
        return 5.0, "panic"
    if mcclellan < -30 or ad_ratio < 0.40:
        return 5.0, "very_weak"
    return 3.0, "weak"


def calculate(base_threshold: float, regime: RegimeState,
              vix: float, day_of_week: int, opex_status: str,
              ev_pct: float = 0.0, breadth_data: dict = None,
              ev_measured: bool = False, mode: str = "swing",
              calendar_enabled: bool = False,
              quote_freshness_unknown: bool = False) -> dict:
    """
    day_of_week: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
    opex_status: 'normal' | 'opex_week' | 'post_opex'
    breadth_data: dict from engine/market_breadth.py (or market_to_dict) — optional;
        when supplied adds a tiered breadth adjustment (see module docstring).
    quote_freshness_unknown (2026-07-21, external review round 2 - "quote
        staleness: unknown should affect confidence... For DAY mode
        especially, missing freshness evidence should not receive the same
        confidence as a verified quote under two minutes old"): True when
        no provider supplied a real market timestamp this cycle (see
        ticker_analyzer.py's quote_age_is_measured / rules/hard_vetoes.py's
        STALE_QUOTE veto, which stays silent - not falsely-fresh, not
        falsely-stale - in this same case). Only docks confidence in DAY
        mode, where a same-day round-trip is the most exposed to a
        genuinely stale quote; SWING/HYBRID's much wider 30-minute staleness
        bar makes this a non-issue there. Never blocks the trade by itself -
        confidence is informational, not a gate - see the review's own
        "either apply a small penalty, or gate DAY-mode execution, or
        require a timestamped quote right before order submission" options;
        this implements the first, least disruptive one.
    Returns dict with final_threshold and breakdown for UI display.
    """
    regime_adj = regime_threshold_adj(regime)
    vix_adj = 13.0 if vix > 27 else (8.0 if vix > 22 else 0.0)

    # MAX of regime or VIX (not sum — both measure market stress).
    # 2026-07-15: regime_threshold_adj can now return a NEGATIVE credit in a
    # clean bull regime - max() would silently discard it against VIX's 0.0
    # whenever VIX is calm (exactly the conditions where the credit should
    # apply). When VIX shows real stress, max() still governs.
    stress_adj = max(regime_adj, vix_adj) if vix_adj > 0 else regime_adj

    # Calendar: always additive. 2026-07-15 audit: Mon+3 and Wed-3 removed -
    # neither had any empirical basis in this system's own data (zero closed
    # trades), and the net effect of the calendar stack was a persistent
    # upward bias on the bar (Mon+3, Fri+5, OpEx+5, PostOpEx+5 vs. a lone
    # Wed-3). Fri+5 is kept: a swing entry on Friday genuinely carries
    # weekend gap risk that can't be managed intraday. OpEx adjustments kept.
    cal_adj = 0.0
    cal_reason = []
    if day_of_week == 4:
        cal_adj += 5.0
        cal_reason.append("Fri+5")
    if opex_status == "opex_week":
        cal_adj += 5.0
        cal_reason.append("OpEx+5")
    elif opex_status == "post_opex":
        cal_adj += 5.0
        cal_reason.append("PostOpEx+5")

    # Transition probability penalty
    tp_adj = regime.transition_probability * 0.08

    # Mode adjustment (2026-07-15): DAY trades pay the spread twice in one
    # session and live inside intraday noise - a same-day round trip needs a
    # visibly better setup than a multi-day swing. HYBRID scores through the
    # swing engine (see scheduler.py), so it takes the swing bar.
    mode_adj = 3.0 if (mode or "swing").lower() == "day" else 0.0

    # Breadth: tiered additive modifier (see module docstring). Applied
    # BEFORE the cap so heavy stress (bad regime + bad breadth) still hits
    # the +20% ceiling rather than stacking past it.
    b_adj, b_tier = _breadth_adj(breadth_data)

    # Calendar is LOG-ONLY by default as of 2026-07-15 (external review):
    # Friday-gap and OpEx effects are plausible intuitions but have zero
    # empirical support in this system's own (empty) trade history, and
    # untested adjustments should not change entry eligibility. The value is
    # still computed and surfaced in the breakdown so it can be evaluated
    # against real outcomes later; flip config.yaml's
    # thresholds.calendar_enabled to true to re-apply it.
    cal_applied = cal_adj if calendar_enabled else 0.0

    # Cap total at +20%
    raw_total = stress_adj + cal_applied + tp_adj + b_adj + mode_adj
    total_adj = min(raw_total, 20.0)

    # EV bonus AFTER cap.
    # 2026-07-15 fix (zero-trades audit): the old `0 <= ev_pct < 1.0 -> +5`
    # branch fired for EVERY signal while the pattern database was empty
    # (ev_pct silently defaults to 0.0 with no history), so the system
    # punished itself +5% on all 239 scored signals for having no track
    # record yet - "no evidence" was treated as "bad evidence", which is a
    # cold-start deadlock: it can never accumulate the history that would
    # lift the penalty because the penalty helps block every first trade.
    # Now: no measured EV -> strictly neutral (0). A penalty only applies
    # when EV was actually measured from enough similar trades AND is
    # genuinely poor (negative).
    if not ev_measured:
        ev_bonus = 0.0
    elif ev_pct > 3.0:
        ev_bonus = -5.0
    elif ev_pct > 2.0:
        ev_bonus = -3.0
    elif ev_pct < 0.0:
        ev_bonus = 5.0
    else:
        ev_bonus = 0.0

    final = base_threshold + total_adj + ev_bonus
    # Floor lowered 55 -> 50 (2026-07-15): with the regime bull-credit above,
    # a clean bull regime + excellent breadth + measured positive EV should
    # be able to move the bar meaningfully below the AGGRESSIVE base of 55;
    # the old floor==base made every favorable adjustment a no-op.
    final = max(50.0, min(85.0, final))  # Hard floor/ceiling

    # Confidence: how much to trust THIS threshold, not just what it is - two
    # tickers can land on the same 67% threshold for very different reasons
    # (a clean, high-confidence regime read vs. a muddy one where bull/bear/
    # choppy probabilities are all close together). Built from
    # engine/regime_engine.py's own RegimeState.confidence_score (already
    # computed there, just never surfaced here before) minus two penalties:
    # transition_probability directly erodes how long this threshold context
    # will likely hold, and hitting the +20% adjustment cap means multiple
    # stress signals were compounding hard enough to need clamping - both
    # real signals already available, not a fabricated metric.
    confidence = regime.confidence_score
    confidence -= regime.transition_probability * 0.3
    if raw_total > 20.0:
        confidence -= 10.0
    # Quote-freshness dock (2026-07-21, external review round 2) - see this
    # function's quote_freshness_unknown docstring above. Modest (-8, not a
    # blocking penalty) and DAY-only.
    quote_freshness_dock = 8.0 if (quote_freshness_unknown and (mode or "swing").lower() == "day") else 0.0
    confidence -= quote_freshness_dock
    confidence = max(0.0, min(100.0, confidence))
    if confidence >= 85:
        confidence_level = "VERY_HIGH"
    elif confidence >= 70:
        confidence_level = "HIGH"
    elif confidence >= 50:
        confidence_level = "MEDIUM"
    elif confidence >= 30:
        confidence_level = "LOW"
    else:
        confidence_level = "VERY_LOW"

    return {
        "base_threshold": base_threshold,
        "stress_adj": stress_adj,
        "cal_adj": cal_applied,
        "cal_computed": cal_adj,          # what calendar WOULD add (log-only unless enabled)
        "cal_enabled": calendar_enabled,
        "tp_adj": tp_adj,
        "mode_adj": mode_adj,
        "breadth_adj": b_adj,
        "breadth_tier": b_tier,
        "total_adj_before_cap": raw_total,
        "cap_applied": raw_total > 20.0,
        "ev_bonus": ev_bonus,
        "final_threshold": final,
        "confidence": round(confidence, 0),
        "confidence_level": confidence_level,
        "quote_freshness_dock": quote_freshness_dock,
        "breakdown": (
            f"Base {base_threshold:.0f}% + stress {stress_adj:.1f}% "
            f"+ cal {cal_applied:.1f}%{'' if calendar_enabled else f' (computed {cal_adj:+.1f}, log-only)'} "
            f"+ transition {tp_adj:.1f}% + mode {mode_adj:+.1f}% + breadth[{b_tier}] {b_adj:+.1f}% "
            f"(capped {total_adj:.1f}%) + EV {ev_bonus:.1f}% = {final:.0f}% "
            f"(confidence {confidence:.0f}% {confidence_level}"
            + (f", -{quote_freshness_dock:.0f} quote-freshness-unknown" if quote_freshness_dock else "")
            + ")"
        ),
        "cal_reasons": cal_reason,
    }
