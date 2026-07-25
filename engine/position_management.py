"""Loop B: Position Management Engine. Runs every cycle for ALL open positions
(populated by confirm_fill.py, not by the buy-signal scan - see README for why
this codebase never opens a position automatically). Produces action dicts that
scheduler.py folds into trade_prompt.md alongside the Loop A buy/sell signals.

EXIT PRIORITY HIERARCHY (deployment-review recommendation - "this hierarchy
prevents a minor indicator from overriding a much more important signal"),
implemented explicitly in _evaluate_priority() below, highest priority first:
  1. RISK CONTROL   - kill switch / daily loss limit already triggered
                       (config.yaml's risk.*) - account-level, overrides
                       everything regardless of this one position's own score.
  2. THESIS BROKEN   - stop_state_machine.py's THESIS_BROKEN stage, which
                       itself fires at exit_score >= 90 (see that file) - a
                       proxy for "thesis broken" (this codebase has no real
                       earnings-miss/guidance-cut/regulatory event feed to
                       detect that directly yet - same honesty gap as
                       rules/hard_vetoes.py's REG_NEWS veto).
  3. EXIT SCORE      - rules/exit_scorer.py's unified 6-bucket action tier
                       (HOLD/MONITOR/TIGHTEN_STOP/REDUCE_POSITION/EXIT) -
                       replaces the old separate mae_eval/health override
                       branches, which are now bucket inputs to this SAME
                       score instead of competing opinions next to it.
  4. PROFIT MANAGEMENT - stop advancement / hold, unchanged.
"""
import logging
from datetime import datetime

import pytz

from engine.mae_mfe_engine import evaluate_mae_percentile, update_live as update_mae_mfe
from engine.position_health import calculate as calc_health
from engine.stop_state_machine import StopState, calculate as calc_stop, should_advance
from engine.ticker_data_adapter import ticker_to_dict, market_to_dict
from rules.exit_scorer import calculate as calc_exit_score
from storage.database import Database

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")  # same tz object convention as scheduler.py


def _clean(pos: dict) -> dict:
    """Strips None-valued keys from a SQLite position row so downstream
    `.get(key, default)` calls in stop_state_machine.py/position_health.py
    actually see their defaults - a dict.get() only falls back to its default
    when the key is ABSENT, not when it's present-but-None, and every Phase 3
    column starts out NULL until confirm_fill.py or this module populates it."""
    return {k: v for k, v in pos.items() if v is not None}


def run_loop_b(ticker_data_cache: dict, mkt, cfg: dict, regime=None, analyzer=None) -> list:
    """
    ticker_data_cache: {ticker: TickerData} already fetched by Loop A this
        cycle (scheduler.py) - reused here to avoid double-fetching a ticker
        that's both on the watchlist and an open position.
    mkt: MarketContextData from engine/market_context.py, same object Loop A used.
    regime: RegimeState from engine/regime_engine.py, same object Loop A used -
        needed by rules/exit_scorer.py's MARKET_CONTEXT bucket (has the
        regime the market is in NOW deteriorated since this position was
        entered?). Optional/None is tolerated (regime_deteriorated rule just
        won't fire) so existing callers/tests don't break.
    analyzer: engine/ticker_analyzer.py's TickerAnalyzer instance, used only if
        an open position's ticker isn't already in ticker_data_cache (e.g. you
        confirm_fill.py'd a ticker that isn't on the watchlist).
    Returns a list of per-position action dicts for engine/packet_builder.py.
    """
    db = Database()
    # get_MANAGED_positions, not get_all_positions (§5, 2026-07-24): Loop B
    # runs the ATR stop machine, writes current_stop_price back to the row and
    # raises URGENT exit actions that scheduler.py executes automatically. A
    # SYNC row (an imported real holding) or a SEED row (a paper mirror of
    # one) entering here would get engine-sized stop machinery armed on
    # capital this engine never chose to deploy. Excluding them here also
    # means no NEW stop is ever written to those rows - the existing stale
    # ones are neutralised by migrations/002 (§5).
    positions = db.get_managed_positions()
    if not positions:
        return []

    market_dict = market_to_dict(mkt, cfg)
    actions = []

    for raw_pos in positions:
        ticker = raw_pos["ticker"]
        td = ticker_data_cache.get(ticker)

        if td is None and analyzer is not None:
            try:
                td = analyzer.analyze(ticker, mkt, cfg=cfg)
            except Exception as e:
                logger.warning(f"{ticker}: could not fetch data for open position ({e}) - skipping this cycle")
                continue

        if td is None:
            logger.warning(f"No data for open position {ticker} - skipping position management")
            continue

        pos = _clean(raw_pos)
        pos.setdefault("entry_price", raw_pos["entry_price"])

        # Update MAE/MFE, then re-read the (possibly updated) row
        update_mae_mfe(raw_pos["id"], {"price": td.price}, pos["entry_price"])
        pos = _clean(db.get_position(raw_pos["id"]) or raw_pos)

        # days_held - real, computed from entry_time
        try:
            entry_time = datetime.fromisoformat(pos["entry_time"])
            days_held = (datetime.utcnow() - entry_time).total_seconds() / 86400
        except (KeyError, ValueError, TypeError):
            days_held = 0.0
        pos["days_held"] = days_held

        ticker_dict = ticker_to_dict(td, mkt, cfg)

        risk_per_share = pos.get("risk_per_share") or (ticker_dict.get("atr") or pos["entry_price"] * 0.015) * 1.5
        current_profit_r = (td.price - pos["entry_price"]) / risk_per_share if risk_per_share else 0.0
        pos["current_profit_r"] = current_profit_r

        # Order matters here: health/mae_eval/time_stop are all INPUTS to the
        # unified Exit Score's POSITION_HEALTH bucket (rules/exit_scorer.py),
        # so they're computed first - health.calculate() no longer takes
        # exit_score as a parameter (see that file's docstring for why the
        # old circular dependency was removed).
        health = calc_health(pos, ticker_dict, market_dict)
        mae_eval = evaluate_mae_percentile(pos, pos.get("setup_type", "unknown"), pos.get("entry_regime", "UNKNOWN"))
        time_stop = _check_time_stop(pos, cfg)
        # EOD flatten check (2026-07-22, full DAY/SWING/HYBRID separation) -
        # see config.yaml's day_eod_flatten_enabled comment and
        # pre_selection_criteria_and_trading_modes.md Section 3. Computed
        # every cycle for every DAY-tagged position; _evaluate_priority()
        # below turns a hit into an urgent exit_full action, which
        # scheduler.py's existing urgent-action handling already executes
        # automatically (same path as THESIS_BROKEN/kill-switch) - no new
        # execution code needed, just a new reason to fire the existing one.
        eod_flatten = _check_eod_flatten(pos, cfg)

        exit_result = calc_exit_score(pos, ticker_dict, market_dict, regime, health, mae_eval, time_stop, cfg=cfg, db=db)

        new_stop = calc_stop(pos, ticker_dict, exit_result.total_score, cfg)
        stop_advances = should_advance(pos.get("current_stop_price", 0), new_stop)

        partial = _check_partial_exit(pos, exit_result)

        priority_action = _evaluate_priority(exit_result, health, new_stop, cfg, eod_flatten=eod_flatten)

        db_updates = {
            "stop_state": new_stop.state.value,
            "position_health_score": health.score,
            "current_profit_r": current_profit_r,
            "days_held": days_held,
            "prev_cycle_pnl_pct": (td.price - pos["entry_price"]) / pos["entry_price"] * 100 if pos["entry_price"] else 0.0,
            # Stashed for NEXT cycle's rules/exit_scorer.py delta checks
            # (adx_weakening, stoch_rollover - see that file) - this cycle's
            # readings become "prev_cycle_*" the next time Loop B runs.
            "prev_cycle_adx": ticker_dict.get("adx"),
            "prev_cycle_macd_hist": ticker_dict.get("macd_hist"),
            "prev_cycle_stoch_k": ticker_dict.get("stoch_k"),
        }
        if stop_advances:
            db_updates["current_stop_price"] = new_stop.stop_price
        if partial:
            db_updates["exit_stage_reached"] = partial["stage"]
        db.update_position(raw_pos["id"], db_updates)

        actions.append({
            "ticker": ticker,
            "position": pos,
            "ticker_data": td,
            "exit_score": exit_result,
            "position_health": health,
            "new_stop": new_stop,
            "stop_should_advance": stop_advances,
            "mae_eval": mae_eval,
            "time_stop": time_stop,
            "eod_flatten": eod_flatten,
            "partial_exit": partial,
            "priority_action": priority_action,
        })

    return actions


def _check_time_stop(pos: dict, cfg: dict):
    days_held = pos.get("days_held", 0)
    profit_r = pos.get("current_profit_r", 0)

    if days_held >= 10 and abs(profit_r) < 0.3:
        return {"type": "no_progress", "action": "re_evaluate",
                "message": f"No progress after {days_held:.1f} days ({profit_r:.1f}R). Re-evaluate."}

    max_days = {"CONSERVATIVE": 10, "MODERATE": 14, "AGGRESSIVE": 20, "TURBO": 30}
    limit = max_days.get(cfg.get("risk_level", "MODERATE"), 14)
    if days_held >= limit and profit_r < 1.0:
        return {"type": "max_hold", "action": "exit",
                "message": f"Max hold time {days_held:.1f} days with {profit_r:.1f}R profit."}
    return None


def _check_eod_flatten(pos: dict, cfg: dict):
    """DAY-classified positions get force-closed before the regular session
    ends (2026-07-22, full DAY/SWING/HYBRID separation) - see config.yaml's
    day_eod_flatten_enabled/day_eod_flatten_time_et comments. Returns a dict
    once the cutoff is reached for a DAY position, else None. SWING/HYBRID-
    tagged-SWING positions never match (trade_mode != "DAY") and are
    completely unaffected - they keep carrying overnight exactly as before.

    Deliberately time-of-day only, no explicit weekday/holiday check: this
    only ever runs from inside a live scheduled or manual cycle, which is
    itself already gated to trading days/hours (see scheduler.py's market-
    open coarse gate) - duplicating that logic here would be a second,
    driftable source of truth for "is the market open today" for zero
    practical benefit, since a cycle simply doesn't run at all outside
    trading hours in the first place."""
    trading_cfg = (cfg or {}).get("trading", {}) or {}
    if not trading_cfg.get("day_eod_flatten_enabled", True):
        return None
    if str(pos.get("trade_mode") or "").upper() != "DAY":
        return None
    cutoff_str = trading_cfg.get("day_eod_flatten_time_et", "15:55")
    try:
        cutoff_h, cutoff_m = (int(x) for x in cutoff_str.split(":"))
    except (ValueError, AttributeError):
        cutoff_h, cutoff_m = 15, 55
    now_et = datetime.now(ET)
    if (now_et.hour, now_et.minute) < (cutoff_h, cutoff_m):
        return None
    return {
        "cutoff_et": cutoff_str,
        "checked_at_et": now_et.strftime("%H:%M"),
        "message": f"DAY position past {cutoff_str} ET flatten cutoff ({now_et.strftime('%H:%M')} ET now).",
    }


def _check_partial_exit(pos: dict, exit_result):
    """Partial-exit fraction now comes DIRECTLY from the unified Exit Score's
    action tier (rules/exit_scorer.py's REDUCE_POSITION=50%/EXIT=100%)
    instead of a separate ad-hoc profit_r-staged ladder - one source of
    truth for "how much to sell", not two that could disagree.
    exit_stage_reached (existing DB column) still guards against
    re-recommending the SAME stage every single cycle once it's been acted
    on - a stage only fires once, the next one only fires once THAT tier is
    reached in a later cycle."""
    if exit_result.partial_exit_pct <= 0:
        return None

    stage = pos.get("exit_stage_reached", 0) or 0
    # REDUCE_POSITION = stage 1 (50%), EXIT = stage 2 (remaining/100%)
    new_stage = 1 if exit_result.action == "REDUCE_POSITION" else 2
    if new_stage <= stage:
        return None

    pct = exit_result.partial_exit_pct
    return {
        "stage": new_stage, "pct": pct,
        "label": f"{exit_result.action}: sell {pct*100:.0f}% (score {exit_result.total_score:.0f}/100)",
        "shares": (pos.get("shares") or 0) * pct,
    }


def _evaluate_priority(exit_result, health, new_stop, cfg: dict, eod_flatten: dict = None):
    """Explicit exit-priority hierarchy (see module docstring). Each tier
    short-circuits everything below it, so a single moderate technical
    reading (e.g. RSI >= 70 alone) can no longer outrank risk control or
    override a position that's otherwise behaving fine - it just adds
    evidence to rules/exit_scorer.py's weighted score."""
    risk_cfg = cfg.get("risk", {})

    # PRIORITY 1: risk control - account-level, overrides this position's
    # own score entirely.
    if risk_cfg.get("kill_switch_triggered"):
        return {"priority": 1, "action": "exit_full", "urgent": True,
                "label": "RISK CONTROL — KILL SWITCH", "reason": "Kill switch is active"}
    if risk_cfg.get("daily_loss_limit_triggered"):
        return {"priority": 1, "action": "exit_full", "urgent": True,
                "label": "RISK CONTROL — DAILY LOSS LIMIT", "reason": "Daily loss limit reached"}

    # PRIORITY 1B: EOD flatten (2026-07-22, full DAY/SWING/HYBRID separation)
    # - a DAY position past its session cutoff (see _check_eod_flatten) is
    # mandatory, not a judgment call, so it sits right below account-level
    # risk control and ABOVE thesis-broken/exit-score - a DAY position that
    # would otherwise show HOLD/MONITOR still has to close before the bell.
    if eod_flatten:
        return {"priority": 1, "action": "exit_full", "urgent": True,
                "label": "EOD FLATTEN — DAY POSITION", "reason": eod_flatten["message"]}

    # PRIORITY 2: thesis broken (proxy - see stop_state_machine.py's
    # THESIS_BROKEN stage, itself triggered at exit_score >= 90).
    if new_stop.state == StopState.THESIS_BROKEN:
        return {"priority": 2, "action": "exit_full", "urgent": True,
                "label": "THESIS BROKEN — URGENT EXIT",
                "reason": f"Exit score {exit_result.total_score:.0f}/100: " + "; ".join(exit_result.reasons[:3])}

    # PRIORITY 3: unified Exit Score action tier.
    if exit_result.action == "EXIT":
        return {"priority": 3, "action": "exit_full", "urgent": False,
                "label": "EXIT", "reason": f"Exit score {exit_result.total_score:.0f}/100: " + "; ".join(exit_result.reasons[:3])}
    if exit_result.action == "REDUCE_POSITION":
        return {"priority": 3, "action": "reduce_position", "urgent": False,
                "label": f"REDUCE POSITION ({exit_result.partial_exit_pct*100:.0f}%)",
                "reason": f"Exit score {exit_result.total_score:.0f}/100: " + "; ".join(exit_result.reasons[:3])}
    if exit_result.action == "TIGHTEN_STOP":
        return {"priority": 3, "action": "tighten_stop", "urgent": False,
                "label": "TIGHTEN STOP",
                "reason": f"Exit score {exit_result.total_score:.0f}/100: " + "; ".join(exit_result.reasons[:3])}
    if exit_result.action == "MONITOR":
        return {"priority": 3, "action": "monitor", "urgent": False,
                "label": "MONITOR",
                "reason": f"Exit score {exit_result.total_score:.0f}/100: " + "; ".join(exit_result.reasons[:3]) or "Early-stage weakness"}

    # PRIORITY 4: profit management - stop advancement, or plain hold.
    if new_stop.stop_price > 0 and new_stop.state not in (StopState.INITIAL_RISK,):
        return {"priority": 4, "action": "update_stop", "urgent": False,
                "label": "STOP ADVANCEMENT", "reason": new_stop.stop_reason}
    return {"priority": 4, "action": "hold", "urgent": False, "label": "HOLD", "reason": "Position healthy"}
