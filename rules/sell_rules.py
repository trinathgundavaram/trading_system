"""SELL trigger logic - HARD EXITS ONLY (single trigger wins, deliberately):
stop_loss/trailing_stop, take_profit, earnings_approaching, vix_spike. These
are risk-management, profit-taking, or event-avoidance decisions where a
single trigger firing IS the correct behavior - waiting for "confirmation"
from other indicators before honoring a stop-loss or taking a scheduled
profit target would be wrong, not more careful. In the exit-priority
hierarchy (see engine/position_management.py's module docstring), these are
the RISK CONTROL / PROFIT MANAGEMENT tiers - the ones that should never wait
on a vote.

Everything that ISN'T a hard, single-signal risk/event trigger - RSI,
MACD, stochastic, Bollinger, price-vs-SMA20, news sentiment, insider selling,
trend/volume/market-context deterioration, position health, MAE anomalies,
time stops - used to live here too, as a second, SEPARATELY-weighted "soft
Exit Score" computed independently of engine/position_management.py's Loop B
(which was already computing its own, richer version of the same idea via
rules/exit_scorer.py + engine/position_health.py). Two scores meant two
opinions that could disagree (deployment-review finding: this system had, in
effect, three different sell scores talking past each other). That soft
score has been REMOVED from this file - the single unified 6-bucket Exit
Score now lives entirely in rules/exit_scorer.py, called from Loop B
(engine/position_management.py's run_loop_b), which has the full context
(regime, position health, MAE history, time-in-trade) this per-ticker Loop A
call never had access to anyway.

DYNAMIC STOP/TARGET, NOT FLAT %: stop_loss and trailing_stop used to be flat
5%/3% regardless of the stock's own volatility. engine/position_management.py's
Loop B (via engine/stop_state_machine.py) already runs a 6-state ATR-based
stop machine every cycle for every open position - initial risk (ATR x
1.2-2.0 depending on entry quality) -> trade-confirming -> breakeven ->
profit-protect -> trend-following ATR trail -> thesis-broken - and persists
the result to the position row as current_stop_price. This module reads that
value as the primary stop trigger instead of recomputing its own flat %,
since stop_state_machine.py's number is strictly more informed (it already
factors in ATR, swing structure, and how much profit is banked). Falls back
to the static config pct only on the first cycle right after entry, before
Loop B has run once for this position yet (current_stop_price is still
NULL/0 at that point) - and that fallback pct is itself clamped to the same
mode-specific ATR risk cap (config.yaml's risk.<level>.stop_loss_day_pct/
stop_loss_swing_pct) the dynamic stop machine enforces, so this one-cycle
window can never carry more risk than the ATR-based stop would ever allow
(2026-07-22, Trinath's stop-machine review - see the clamp below). Same idea
for take_profit: an R-multiple target
(entry + risk_per_share * take_profit.r_multiple) using the same ATR*1.5
risk-per-share convention stop_state_machine.py uses, so a target scales
with each stock's own volatility instead of every stock sharing a flat 10%.
"""
from dataclasses import dataclass, field


@dataclass
class SellResult:
    should_sell: bool
    triggered_rule: str = ""
    reason: str = ""
    urgency: str = "normal"
    # Legacy fields, kept at their neutral default for callers that still
    # read them (none in the live pipeline as of this change - see
    # engine/packet_builder.py/scheduler.py, which only read should_sell/
    # triggered_rule/reason/urgency). The real Exit Score now lives in
    # rules/exit_scorer.py's ExitScoreResult (Loop B), not here.
    exit_score: float = 0.0
    exit_score_threshold: float = 0.0
    contributing: list = field(default_factory=list)


class SellRulesEngine:
    def evaluate(self, td, position, mkt, cfg: dict) -> SellResult:
        if not position:
            return SellResult(False)

        sell_cfg = cfg["sell_rules"]
        rules = sell_cfg["rules"]
        price = td.price
        entry = position["entry_price"]
        pnl_pct = ((price - entry) / entry) * 100 if entry else 0.0

        # Update trailing stop high watermark (in-memory here; scheduler.py
        # persists it to SQLite via db.update_trail_high once this returns).
        if price > position.get("trail_high", entry):
            position["trail_high"] = price
        trail_high = position.get("trail_high", entry)

        # ---- HARD EXITS: single trigger wins, checked first ----

        # Dynamic stop: prefer Loop B's ATR-based current_stop_price (see
        # module docstring) over the flat config pct. position rows come
        # straight from db.get_open_position() (not run through
        # engine/position_management.py's _clean()), so a NULL SQLite column
        # comes back as None, not absent - `or 0` is required here, a plain
        # .get(key, default) would not catch it.
        dynamic_stop = position.get("current_stop_price") or 0
        stop_state = position.get("stop_state") or ""

        if dynamic_stop > 0:
            stop_enabled = rules.get("stop_loss", {}).get("enabled", True)
            stop_triggered = stop_enabled and price <= dynamic_stop
            stop_rule_name = "dynamic_stop"
            stop_reason = (f"Dynamic stop hit ({stop_state or 'ATR-based'}): "
                            f"price ${price:.2f} <= stop ${dynamic_stop:.2f}")
            stop_urgency = "urgent" if stop_state in ("INITIAL_RISK", "TRADE_CONFIRMING", "THESIS_BROKEN") else "normal"
        else:
            # No dynamic stop yet (first cycle after entry) - fall back to
            # the original flat stop_loss/trailing_stop % checks so a
            # position is never left unprotected while Loop B catches up.
            # stop_loss is checked first (same priority order the original
            # first-trigger-wins list used) since it's anchored to entry and
            # is the more fundamental risk limit; trailing_stop only matters
            # once trail_high has actually moved above entry.
            #
            # CLAMPED TO THE ATR CAP (2026-07-22, Trinath's stop-machine
            # review, "fallback flat stops could be misaligned"): the flat
            # config.yaml pct used to be applied as-is, so if it happened to
            # be wider than the mode-specific ATR ceiling (config.yaml's
            # risk.<level>.stop_loss_day_pct/stop_loss_swing_pct - the same
            # cap engine/stop_state_machine.py's INITIAL_RISK stage enforces
            # once it runs), a position could briefly carry MORE risk during
            # this one first-cycle window than the ATR stop machine would
            # ever allow it to carry once Loop B catches up. Clamping the
            # fallback pct to min(configured pct, ATR cap) keeps this window
            # inside the same risk envelope instead of temporarily exceeding
            # it. Falls back to the flat pct unchanged if risk_level/mode
            # config is missing for any reason (never raises).
            is_day_position = str(position.get("trade_mode") or "").upper() == "DAY"
            try:
                risk_cfg = cfg["risk"][cfg["risk_level"]]
                atr_cap_pct = (
                    risk_cfg.get("stop_loss_day_pct", risk_cfg.get("stop_loss_swing_pct", 5) / 2)
                    if is_day_position else risk_cfg.get("stop_loss_swing_pct", 5)
                )
            except (KeyError, TypeError):
                atr_cap_pct = None

            configured_stop_pct = rules.get("stop_loss", {}).get("pct", 5.0)
            configured_trail_pct = rules.get("trailing_stop", {}).get("pct", 3.0)
            effective_stop_pct = min(configured_stop_pct, atr_cap_pct) if atr_cap_pct is not None else configured_stop_pct
            # Trailing stop isn't itself bounded by the ATR *risk* cap (it's
            # a give-back limit off the high watermark, not distance from
            # entry) but it shouldn't casually exceed the same ceiling
            # either - clamp it too so a wide config value can't leave a
            # profitable-then-reversing position exposed to more give-back
            # than a fresh entry's own ATR cap would allow.
            effective_trail_pct = min(configured_trail_pct, atr_cap_pct) if atr_cap_pct is not None else configured_trail_pct

            trailing_hit = (rules.get("trailing_stop", {}).get("enabled") and
                             price <= trail_high * (1 - effective_trail_pct / 100))
            stop_loss_hit = rules.get("stop_loss", {}).get("enabled") and pnl_pct <= -effective_stop_pct
            stop_triggered = stop_loss_hit or trailing_hit
            stop_rule_name = "stop_loss" if stop_loss_hit else "trailing_stop"
            if stop_loss_hit:
                stop_reason = (f"Stop loss hit: {pnl_pct:.2f}% (limit: -{effective_stop_pct}%"
                                f"{' , ATR-capped' if effective_stop_pct < configured_stop_pct else ''}) - no dynamic stop yet")
            else:
                stop_reason = (f"Trailing stop: price ${price:.2f} fell {effective_trail_pct}% "
                                f"from high ${trail_high:.2f} (no dynamic stop yet"
                                f"{', ATR-capped' if effective_trail_pct < configured_trail_pct else ''})")
            stop_urgency = "urgent"

        # Dynamic take-profit: R-multiple target off the same ATR*1.5
        # risk-per-share convention stop_state_machine.py uses, so the
        # target scales with each stock's own volatility instead of every
        # stock sharing a flat 10%. Falls back to the flat pct if ATR/
        # risk_per_share isn't available yet either.
        risk_per_share = position.get("risk_per_share") or 0
        tp_cfg = rules.get("take_profit", {})
        r_multiple = tp_cfg.get("r_multiple", 3.0)
        if risk_per_share > 0:
            dynamic_target = entry + risk_per_share * r_multiple
            target_triggered = tp_cfg.get("enabled") and price >= dynamic_target
            target_reason = f"Take profit hit: {r_multiple:.1f}R target ${dynamic_target:.2f} reached (+{pnl_pct:.2f}%)"
        else:
            target_triggered = tp_cfg.get("enabled") and pnl_pct >= tp_cfg.get("pct", 10.0)
            target_reason = f"Take profit hit: +{pnl_pct:.2f}% (no ATR yet, static target)"

        hard_checks = [
            (stop_triggered, stop_rule_name, stop_reason, stop_urgency),

            (target_triggered, "take_profit", target_reason, "normal"),

            # 0 <= guard (2026-07-16): days_to_earnings went negative when
            # finviz reported a PAST earnings date ("Earnings in -72 days"
            # exits closing positions within hours). The source is fixed in
            # ticker_analyzer._parse_finviz, but keep this rule defensive -
            # only an actually-upcoming earnings date may force an exit.
            (rules.get("earnings_approaching", {}).get("enabled") and
             0 <= td.days_to_earnings <= rules["earnings_approaching"].get("days_before", 2),
             "earnings_approaching", f"Earnings in {td.days_to_earnings} days", "normal"),

            (rules.get("vix_spike", {}).get("enabled") and mkt.vix_level >= rules["vix_spike"]["threshold"],
             "vix_spike", f"VIX spike: {mkt.vix_level:.1f}", "urgent"),
        ]
        for triggered, rule_name, reason, urgency in hard_checks:
            if triggered:
                return SellResult(True, rule_name, reason, urgency, exit_score=100.0)

        # No hard exit triggered. The graduated hold/monitor/tighten/reduce/
        # exit decision now lives entirely in Loop B (rules/exit_scorer.py's
        # unified Exit Score, via engine/position_management.py) - see this
        # file's module docstring for why that moved out of here.
        return SellResult(False)
