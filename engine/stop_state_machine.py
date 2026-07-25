"""6-state dynamic stop machine. Replaces a static ATR stop with one that only
ever moves in the trade's favor - never widened.

`position` is expected to have None values already stripped by the caller
(engine/position_management.py's _clean() helper) so `.get(key, default)`
behaves the way this file assumes throughout (a present-but-None column from
SQLite would otherwise silently defeat every default here).

MODE-AWARE + CONFIG-DRIVEN (2026-07-22, Trinath's ATR stop-machine review):
every ATR multiplier and R-based staging threshold below now comes from
config.yaml's `stop_machine.DAY` / `stop_machine.SWING` sections instead of
being hardcoded identically for both modes. Previously DAY and SWING shared
the exact same 1.2/1.5/2.0 initial multipliers and 0.5R/1R/2R staging -
reasonable for a first cut, but a DAY position is force-flattened same-day
regardless of where its stop sits (day_eod_flatten_enabled), so it doesn't
need swing-sized room to work and can afford to lock in profit faster.
DEFAULT_MODE_CFG below reproduces the OLD hardcoded numbers exactly, so a
config missing the new `stop_machine` section (or missing one mode's
sub-keys) degrades gracefully to the pre-2026-07-22 behavior rather than
crashing or silently using zeros.

ATR-SPIKE WIDENING (regime awareness): when ATR% of price clears
`stop_machine.atr_spike.atr_pct_threshold` (a post-earnings/macro-shock-sized
move, not normal chop), the initial-stop ATR multiplier gets a one-time bonus
(`atr_spike.multiplier_bonus`) so a real volatility shock isn't treated
identically to a calm-regime entry. This only widens the raw ATR distance -
it's still clamped by the same per-profile stop_loss_day_pct/
stop_loss_swing_pct cap as before, so the hard risk ceiling never moves.
Pairs with (does not replace) config.yaml's position_sizing.
volatility_atr_pct_bands, which already shrinks position SIZE in the same
regime - same dollar risk, more room for the stop to breathe.
"""
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class StopState(Enum):
    INITIAL_RISK = "INITIAL_RISK"
    TRADE_CONFIRMING = "TRADE_CONFIRMING"
    BREAKEVEN = "BREAKEVEN"
    PROFIT_PROTECT = "PROFIT_PROTECT"
    TREND_FOLLOWING = "TREND_FOLLOWING"
    THESIS_BROKEN = "THESIS_BROKEN"

    @property
    def exit_kind(self) -> str:
        """§D: which EXIT_KINDS member an exit IN THIS STATE represents.

        Lives here rather than being inferred downstream because this file is
        the only place that knows what its own states mean. A stop in
        INITIAL_RISK is capping a loss; the identical mechanism in
        TREND_FOLLOWING is protecting a profit, and folding the two together
        is how p_stop_loss would end up counting winners as stop-outs.

        The table itself is in rules/common.py alongside EXIT_KINDS, so that
        the vocabulary and its mappings stay in one file and cannot drift into
        two half-agreeing copies.
        """
        from rules.common import exit_kind_for_stop_state
        return exit_kind_for_stop_state(self.value)


@dataclass
class StopLevel:
    state: StopState
    stop_price: float
    stop_reason: str
    trail_from: Optional[float]
    calculated_at: str

    @property
    def exit_kind(self) -> str:
        """Convenience passthrough so a caller holding a StopLevel does not
        have to reach through to .state.exit_kind. Records only - nothing in
        this file's stop ARITHMETIC reads it."""
        return self.state.exit_kind


# Reproduces the pre-2026-07-22 hardcoded behavior exactly - used as a
# per-key fallback so a config.yaml without (or with a partial)
# `stop_machine` section still works. The real DAY/SWING split lives in
# config.yaml's `stop_machine.DAY` / `stop_machine.SWING`, not here.
_DEFAULT_MODE_CFG = {
    "atr_multiplier_strong": 2.0,
    "atr_multiplier_standard": 1.5,
    "atr_multiplier_weak": 1.2,
    "breakeven_r": 0.5,
    "breakeven_lock_r": 0.05,
    "profit_protect_r": 1.0,
    "profit_protect_lock_r": 0.25,
    "profit_protect_trail_atr_mult": 1.5,
    "trend_trail_r": 2.0,
    "trend_trail_atr_mult": 1.0,
}
_DEFAULT_ATR_SPIKE_CFG = {"atr_pct_threshold": 5.0, "multiplier_bonus": 0.0}


def _mode_cfg(config: dict, is_day: bool) -> dict:
    stop_cfg = (config or {}).get("stop_machine", {}) or {}
    mode_key = "DAY" if is_day else "SWING"
    merged = dict(_DEFAULT_MODE_CFG)
    merged.update(stop_cfg.get(mode_key, {}) or {})
    return merged


def _atr_spike_cfg(config: dict) -> dict:
    merged = dict(_DEFAULT_ATR_SPIKE_CFG)
    merged.update(((config or {}).get("stop_machine", {}) or {}).get("atr_spike", {}) or {})
    return merged


# ── Stage ratchet (S-1, v1.1.0, found by scripts/audit_stops.py 2026-07-24) ──
# The stages below are ordered by how much the trade has proved itself. A
# position that has REACHED a stage never reports an earlier one, because the
# stop price it earned there never widens (see should_advance).
#
# Before this, _calculate_raw's state was re-derived from the CURRENT profit_r
# every cycle with no memory. The moment price fell back below breakeven_r, the
# state reverted to INITIAL_RISK while current_stop_price stayed locked at
# entry + risk_per_share x breakeven_lock_r. State and price then described
# different things - AES was found at entry 14.8050 with stop 14.8095 and state
# INITIAL_RISK, which reads as "no protection yet" on a position that was in
# fact breakeven-protected.
#
# THESIS_BROKEN is deliberately absent from the ranking: it is an emergency
# that must be able to fire from any stage, so it is never suppressed and never
# treated as a stage that can be regressed FROM.
_STAGE_RANK = {
    StopState.INITIAL_RISK: 0,
    StopState.TRADE_CONFIRMING: 1,
    StopState.BREAKEVEN: 2,
    StopState.PROFIT_PROTECT: 3,
    StopState.TREND_FOLLOWING: 4,
}


def _reached_stage(position: dict):
    """The furthest stage this position has previously reached, or None when it
    has no history (a fresh entry) or its last state was THESIS_BROKEN."""
    name = str(position.get("stop_state") or "").upper()
    try:
        return StopState(name)
    except ValueError:
        return None


def _apply_stage_ratchet(candidate: StopLevel, position: dict) -> StopLevel:
    """Floor `candidate` at the stage this position already reached.

    Only the LABEL and the floor are affected. The stop price returned is
    max(candidate, current) - which is what engine/position_management.py's
    should_advance() already enforced independently, so this changes no exit
    that was not already going to happen. It makes the recorded state agree
    with the recorded price.
    """
    if candidate.state is StopState.THESIS_BROKEN:
        return candidate

    previous = _reached_stage(position)
    if previous is None or previous not in _STAGE_RANK:
        return candidate
    if _STAGE_RANK[candidate.state] >= _STAGE_RANK[previous]:
        return candidate

    held_price = max(float(candidate.stop_price),
                     float(position.get("current_stop_price") or 0))
    return StopLevel(
        previous,
        held_price,
        (f"{previous.value} held (stage ratchet): the current reading would imply "
         f"{candidate.state.value}, but a stage a trade has reached does not revert "
         f"while its stop stands. Underlying: {candidate.stop_reason}"),
        candidate.trail_from,
        candidate.calculated_at,
    )


def calculate(position: dict, ticker_data: dict, exit_score: float, config: dict) -> StopLevel:
    """Public entry point: the raw per-cycle stage, floored by the ratchet."""
    return _apply_stage_ratchet(
        _calculate_raw(position, ticker_data, exit_score, config), position)


def _calculate_raw(position: dict, ticker_data: dict, exit_score: float, config: dict) -> StopLevel:
    """The stage implied by THIS cycle's numbers alone, with no memory.

    Kept separate and importable so tests can prove the ratchet is what fixes
    the regression rather than some incidental change in the stage maths.
    """
    entry = position["entry_price"]
    current = ticker_data.get("price", entry)
    atr = ticker_data.get("atr") or entry * 0.015
    avwap_earnings = ticker_data.get("avwap_earnings", 0)
    recent_swing_low = ticker_data.get("recent_swing_low", 0)
    high_watermark = position.get("high_watermark_price", current)
    risk_per_share = position.get("risk_per_share") or (atr * 1.5)
    profit_r = (current - entry) / risk_per_share if risk_per_share > 0 else 0

    is_day_position = str(position.get("trade_mode") or "").upper() == "DAY"
    mcfg = _mode_cfg(config, is_day_position)
    mode_label = "DAY" if is_day_position else "SWING"

    now = datetime.utcnow().isoformat()

    # Stage 6: Thesis broken - exit at market equivalent
    if exit_score >= 90:
        return StopLevel(StopState.THESIS_BROKEN, current * 0.999,
                          "Thesis broken — urgent exit", None, now)

    # Stage 5: Profit > mode's trend_trail_r - aggressive trail
    if profit_r >= mcfg["trend_trail_r"]:
        trail = high_watermark - (atr * mcfg["trend_trail_atr_mult"])
        trail = max(trail, entry + risk_per_share * 0.5)
        if recent_swing_low:
            trail = max(trail, recent_swing_low - atr * 0.25)
        return StopLevel(StopState.TREND_FOLLOWING, trail,
                          f"Trail {mcfg['trend_trail_atr_mult']:.2f}xATR below watermark "
                          f"${high_watermark:.2f} ({mode_label}). {profit_r:.1f}R profit.",
                          high_watermark, now)

    # Stage 4: Profit > mode's profit_protect_r - lock in gains
    if profit_r >= mcfg["profit_protect_r"]:
        lock = entry + risk_per_share * mcfg["profit_protect_lock_r"]
        if avwap_earnings:
            lock = max(lock, avwap_earnings - atr * 0.5)
        lock = max(lock, current - atr * mcfg["profit_protect_trail_atr_mult"])
        return StopLevel(StopState.PROFIT_PROTECT, lock,
                          f"Locking {mcfg['profit_protect_lock_r']:.2f}R+ profit ({mode_label}). "
                          f"{profit_r:.1f}R total.", None, now)

    # Stage 3: Profit > mode's breakeven_r - move to breakeven
    if profit_r >= mcfg["breakeven_r"]:
        be_stop = entry + risk_per_share * mcfg["breakeven_lock_r"]
        return StopLevel(StopState.BREAKEVEN, be_stop,
                          f"Breakeven protection ({mode_label}). {profit_r:.1f}R profit.", None, now)

    # Stage 2: Trade confirming (above AVWAP, moving right)
    if current > entry and avwap_earnings and current > avwap_earnings:
        confirmed = entry - risk_per_share
        if avwap_earnings:
            confirmed = max(confirmed, avwap_earnings - atr * 0.5)
        return StopLevel(StopState.TRADE_CONFIRMING, confirmed,
                          "Trade confirming: above AVWAP. Stop below AVWAP support.", None, now)

    # Stage 1: Initial risk stop
    entry_score = position.get("entry_signal_score") or 75
    if entry_score >= 85:
        atr_mult = mcfg["atr_multiplier_strong"]  # Wider for exceptional setups
    elif entry_score < 65:
        atr_mult = mcfg["atr_multiplier_weak"]  # Tighter for weak setups
    else:
        atr_mult = mcfg["atr_multiplier_standard"]  # Standard

    # ATR-spike widening (see module docstring) - a real volatility shock
    # gets more room, not the same distance as a calm-regime entry. Applied
    # only to the raw multiplier; the cap below still has final say.
    spike_cfg = _atr_spike_cfg(config)
    atr_pct = (atr / entry * 100) if entry else 0.0
    spiked = atr_pct >= spike_cfg["atr_pct_threshold"]
    if spiked:
        atr_mult += spike_cfg["multiplier_bonus"]

    initial = entry - (atr * atr_mult)

    # 2026-07-20 (most paper-trade exits were premature stop-outs, audited
    # after Trinath flagged it): this used to also clamp against
    # `floor = max(entry - atr*1.5, entry*0.9925, entry - 0.50)`, then take
    # `initial = max(initial, floor)`. Because these are PRICES (lower price
    # = further from entry = wider stop), max() picks whichever candidate is
    # CLOSEST to entry - i.e. it silently overrode the ATR×atr_mult distance
    # with a ~0.75%-of-entry (or $0.50) ceiling on risk almost every time,
    # since entry*0.9925 or entry-0.50 was very often the tightest of the
    # three. That defeated the whole tiered atr_mult design above (2.0/1.5/
    # 1.2x ATR for strong/standard/weak setups all collapsed to the same
    # ~0.75-1% stop regardless of score or volatility) and is the direct
    # cause of the -0.47% to -1.9% stop-outs (avg -1.05%, 14 of 28 exits)
    # seen in paper_trades - the position never had room to work before
    # normal noise hit it. The zero/missing-ATR edge case this floor may
    # have meant to guard against is already covered upstream by
    # `atr = ticker_data.get("atr") or entry * 0.015` (line ~95), so removing
    # it doesn't reopen that gap. The risk-level cap below (TURBO=8% etc.)
    # remains the one legitimate ceiling on how wide this can get. Do NOT
    # reintroduce a fixed %/$ floor here (2026-07-22 review reaffirmed this) -
    # any future ceiling must go through the mode-specific max_stop_pct
    # below, not a max() against a raw price.

    risk_cfg = config["risk"][config["risk_level"]]
    # DAY-mode stop ceiling (2026-07-22, full DAY/SWING/HYBRID separation -
    # see config.yaml's stop_loss_day_pct comment): a DAY-classified
    # position (position['trade_mode'] == 'DAY', set by scheduler.py's
    # _classify_hybrid_leg for HYBRID legs, or directly for pure DAY-mode
    # trades) is held against a materially tighter cap than a SWING
    # position at the same risk profile - it's expected to be flattened
    # same-day (see config.yaml's day_eod_flatten_enabled), so it should
    # never have been carrying swing-sized risk in the first place. Falls
    # back to half the swing stop if a profile is missing the day-specific
    # key. Every position not explicitly tagged DAY (including every
    # existing SWING/HYBRID-untagged position already open before this
    # change) is completely unaffected - same stop_loss_swing_pct as always.
    max_stop_pct = (
        risk_cfg.get("stop_loss_day_pct", risk_cfg.get("stop_loss_swing_pct", 5) / 2)
        if is_day_position else risk_cfg.get("stop_loss_swing_pct", 5)
    ) / 100
    min_stop = entry * (1 - max_stop_pct)
    initial = max(initial, min_stop)

    reason = f"Initial ATR×{atr_mult:.2f} stop (score={entry_score:.0f}, {mode_label}"
    reason += f", ATR-spike +{spike_cfg['multiplier_bonus']:.2f}" if spiked else ""
    reason += ")"

    return StopLevel(StopState.INITIAL_RISK, initial, reason, None, now)


def should_advance(current_stop: float, new_stop: StopLevel) -> bool:
    """Stop only advances (moves up/in trade's favor). Never widens."""
    return new_stop.stop_price > (current_stop or 0)
