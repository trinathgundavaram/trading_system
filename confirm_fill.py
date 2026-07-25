#!/usr/bin/env python3
"""Bridges the gap between "Claude Desktop placed a real Robinhood order" and
this codebase's SQLite state. Robinhood stays Claude-Desktop-only by design -
this script is never called automatically and never places orders itself. It
just records what you tell it happened, so:

  1. `positions` reflects your real, actually-filled positions (which is what
     activates sell_rules.py in the next scheduler cycle - today, with no
     confirmed position, get_open_position() always returns None and sell
     signals never fire).
  2. The linked pattern_database entry (if the buy matches a recent BUY signal
     this system generated) gets closed with your REAL fill price/outcome
     instead of - or in addition to - the simulated time-based close in
     scheduler.py's _close_due_patterns().

Usage:
    python3 confirm_fill.py buy   TICKER PRICE SHARES
    python3 confirm_fill.py sell  TICKER PRICE
    python3 confirm_fill.py list

Examples:
    python3 confirm_fill.py buy NVDA 145.32 3.5
    python3 confirm_fill.py sell NVDA 152.10
"""
import argparse
import json
import sys
from datetime import datetime

import yaml

from learning.pattern_database import PatternDatabase
from storage.database import Database

db = Database()
pattern_db = PatternDatabase(db)


def _load_config() -> dict:
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _most_recent_open_pattern(ticker: str):
    """Finds the open pattern_database entry this buy most likely corresponds
    to - the most recently recorded unclosed BUY signal for this ticker. If
    none exists (e.g. you bought something the rules engine never flagged),
    returns None and the position is still recorded, just without a linked
    pattern to close later."""
    # 2026-07-22 (EV mode-keying fix): no longer filters mode="SWING" - real
    # fills need to match against a DAY-mode pattern just as readily as a
    # SWING one now that scheduler.py genuinely records patterns under both
    # (see scheduler.py's _has_open_pattern comment for the full story).
    open_patterns = [
        p for p in db.get_patterns(ticker=ticker, closed_only=False)
        if not p["is_closed"]
    ]
    if not open_patterns:
        return None
    open_patterns.sort(key=lambda p: p["recorded_at"], reverse=True)
    return open_patterns[0]


def _snapshot_id(ticker: str, side: str) -> str:
    import uuid
    return f"{ticker}_{side}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"


def cmd_buy(ticker: str, price: float, shares: float):
    ticker = ticker.upper()
    existing = db.get_open_position(ticker)
    if existing:
        print(f"WARNING: {ticker} already has an open position (id={existing['id']}, "
              f"entry ${existing['entry_price']:.2f}). Not opening a second one - "
              f"confirm the sell first if this is meant to replace it.")
        sys.exit(1)

    pattern = _most_recent_open_pattern(ticker)
    pattern_id = pattern["id"] if pattern else None

    dollar_amount = round(price * shares, 2)
    # Stamp the trading mode active at confirmation time (2026-07-16) so real
    # fills carry the same SWING/DAY/HYBRID attribution as paper trades.
    _mode = (_load_config().get("trading", {}) or {}).get("mode", "SWING")
    db.open_position(ticker, entry_price=price, shares=shares, dollar_amount=dollar_amount,
                      pattern_id=pattern_id, trade_mode=_mode)

    # Snapshot the indicators/rule breakdown that actually led to this fill,
    # so you can look back at any confirmed transaction later and see WHY the
    # system flagged it - not just the score, the full bucket/rule detail.
    # pattern["features"] is engine/pattern_features.py's full snapshot
    # (RSI/MACD/SMAs/regime/bucket scores/rules_passed at BUY-signal time,
    # see build_pattern_features()) when this fill matches a signal the
    # system generated; recent_signal (below) is the same rules_fired/
    # bucket_scores detail the Signals tab now shows (Task #25/#26), kept
    # here too since a pattern isn't recorded for every signal (e.g. you
    # bought something the rules engine never flagged as a BUY candidate).
    recent_signal = db.get_recent_signal(ticker)
    snap_id = _snapshot_id(ticker, "buy")
    db.save_trade_snapshot(snap_id, recent_signal.get("id") if recent_signal else None, json.dumps({
        "trigger": "buy", "fill_price": price, "shares": shares,
        "pattern_id": pattern_id,
        "pattern_features": pattern["features"] if pattern else None,
        "recent_signal": recent_signal,
    }, default=str))

    db.log_trade(ticker, "buy", amount=dollar_amount, shares=shares, fill_price=price,
                 status="filled", snapshot_id=snap_id)

    # Phase 3 wiring: seed the Position Management Engine's fields so
    # engine/stop_state_machine.py and engine/position_health.py have real
    # entry-time context instead of falling back to their neutral defaults.
    # recent_signal (fetched above, for the snapshot) comes from the
    # `signals` table, which today only carries buy_pct/price/data_quality -
    # NOT an ATR or a true EV, so entry_signal_score is the only field
    # genuinely populated from it; entry_p_win/entry_ev stay None until EV is
    # wired into the live signal flow (see README - not done yet).
    # regime/setup_type instead come from the linked pattern's feature
    # snapshot, when there is one.
    # ATR-aware initial stop (2026-07-15, external review): a flat 1.5% is
    # noise-distance on a volatile name and needlessly wide on a calm one.
    # Initial risk = max(1.2*ATR, 1.5% of price), capped at the risk-level's
    # stop_loss_swing_pct - the same shape the review recommended
    # (min(max(volatility floor, structure), max allowed)). ATR comes from
    # the linked pattern's feature snapshot when available; the flat 1.5%
    # remains the no-data fallback. stop_state_machine still refines from
    # the next cycle.
    _atr = 0.0
    try:
        _atr = float((pattern["features"].get("atr") if pattern else 0) or 0)
    except (TypeError, ValueError):
        _atr = 0.0
    _cfg = _load_config()
    _max_risk_pct = (_cfg.get("risk", {}).get(_cfg.get("risk_level", "MODERATE"), {})
                     .get("stop_loss_swing_pct", 5.0)) / 100.0
    risk_per_share = min(max(1.2 * _atr, price * 0.015), price * _max_risk_pct)
    entry_regime = pattern["features"].get("regime") if pattern else None
    setup_type = pattern["features"].get("setup_type") if pattern else "unknown"

    db.update_position_by_ticker(ticker, {
        "entry_signal_score": recent_signal.get("buy_pct") if recent_signal else None,
        "entry_regime": entry_regime,
        "setup_type": setup_type,
        "high_watermark_price": price,
        # §53 (Phase 2.5): ATR as a % of price at entry, so this manually
        # confirmed fill counts toward portfolio_risk's high-volatility cap in
        # the same units as everything else. _atr comes from the linked
        # pattern's recorded features - the same source risk_per_share above
        # already uses - so no extra fetch. None when there is no linked
        # pattern, which is honest: _position_atr_pct() then falls back to the
        # stop-distance proxy and says so in the log.
        "entry_atr_pct": (_atr / price * 100) if (_atr and price) else None,
        "risk_per_share": risk_per_share,
        "current_stop_price": price - risk_per_share,
        "current_target_price": price + (risk_per_share * 3),
        "stop_state": "INITIAL_RISK",
        "exit_stage_reached": 0,
    # §16: the REAL book. This command confirms a real fill, and the
    # open_position() call above wrote simulated=0 (its default) - so the
    # seeding has to target the same row. Unscoped, it could land on the paper
    # mirror of the same ticker and leave the real position with the NULL
    # risk_per_share that meant no R-multiple target and zero take-profit
    # exits across 29 trades.
    }, simulated=False)

    print(f"Recorded BUY fill: {ticker} {shares} shares @ ${price:.2f} (${dollar_amount:.2f})")
    if pattern_id:
        print(f"  Linked to pattern_database entry #{pattern_id} "
              f"(recorded {pattern['recorded_at']}) - will close with your real exit, "
              f"not the time-based simulation.")
    else:
        print("  No matching open BUY signal found for this ticker - position recorded "
              "without a linked pattern_database entry.")
    print(f"  Position management seeded: stop ${price - risk_per_share:.2f}, "
          f"target ${price + risk_per_share * 3:.2f} "
          f"(risk/share ${risk_per_share:.2f} = max(1.2xATR, 1.5%), capped at risk-level stop %). "
          f"engine/stop_state_machine.py will refine this on the next scheduler.py cycle.")
    print(f"  Indicator/rule snapshot saved (id={snap_id}) - view it later in the "
          f"UI's Journal tab or via `db.get_trade_snapshot('{snap_id}')`.")


def cmd_sell(ticker: str, price: float):
    ticker = ticker.upper()
    closed = db.close_position(ticker, exit_price=price)
    if not closed:
        print(f"No open position found for {ticker} - nothing to close.")
        sys.exit(1)

    # Snapshot what the system was seeing at exit time. There's no live
    # TickerData here (confirm_fill.py makes no MCP calls by design - fast,
    # simple, no network dependency), so the best real data available is the
    # most recent scan cycle's signal for this ticker: same rules_fired/
    # bucket_scores/sell_triggered_rule/sell_reason detail the Signals tab
    # shows (Task #25/#26). entry_pattern (if linked) is included too, so the
    # snapshot has both "why it was bought" and "what was happening at exit"
    # in one place.
    entry_pattern_id = closed.get("pattern_id")
    entry_pattern = db.get_pattern_by_id(entry_pattern_id) if entry_pattern_id else None
    recent_signal = db.get_recent_signal(ticker)
    snap_id = _snapshot_id(ticker, "sell")
    db.save_trade_snapshot(snap_id, recent_signal.get("id") if recent_signal else None, json.dumps({
        "trigger": "sell", "fill_price": price, "shares": closed["shares"],
        "entry_price": closed["entry_price"], "pnl": closed["pnl"], "pnl_pct": closed["pnl_pct"],
        "pattern_id": entry_pattern_id,
        "entry_pattern_features": entry_pattern["features"] if entry_pattern else None,
        "recent_signal": recent_signal,
    }, default=str))

    db.log_trade(ticker, "sell", amount=round(price * closed["shares"], 2),
                 shares=closed["shares"], fill_price=price, status="filled", snapshot_id=snap_id)

    print(f"Recorded SELL fill: {ticker} {closed['shares']} shares @ ${price:.2f}")
    print(f"  Entry ${closed['entry_price']:.2f} -> Exit ${price:.2f} | "
          f"P&L ${closed['pnl']:+.2f} ({closed['pnl_pct']:+.2f}%)")
    print(f"  Indicator/rule snapshot saved (id={snap_id}) - view it later in the "
          f"UI's Journal tab or via `db.get_trade_snapshot('{snap_id}')`.")

    # Phase 4 wiring: record MAE/MFE for this now-closed trade (feeds
    # engine/mae_mfe_engine.py's percentile check on FUTURE positions of the
    # same setup_type/regime - won't have enough history to say anything
    # useful until ~10 closed trades of a given setup_type/regime accumulate)
    # and start a re-entry cooldown so hard_vetoes.py's COOLDOWN check blocks
    # immediately re-buying the same ticker after a stop-out.
    trade = db.get_closed_trade_for_ticker(ticker)
    if trade:
        # §15: authoritative figures from close_position, which is now the one
        # place P&L and hold time are computed. Recomputing them here from the
        # re-fetched row against a fresh clock reading is what let the same
        # trade be recorded two different ways in two tables.
        trade["pnl_pct"] = closed["pnl_pct"]
        trade["pnl"] = closed["pnl"]
        trade["hold_hours"] = closed.get("hold_hours", 0.0)
        from engine.mae_mfe_engine import record_completed
        record_completed(trade)
        print(f"  Recorded MAE/MFE for future pattern-matching "
              f"(setup_type={trade.get('setup_type') or 'unknown'}, regime={trade.get('entry_regime') or 'UNKNOWN'}).")

    cfg = _load_config()
    cooldown_hours = {"CONSERVATIVE": 48, "MODERATE": 24, "AGGRESSIVE": 12, "TURBO": 6}
    hours = cooldown_hours.get(cfg.get("risk_level", "MODERATE"), 24)
    db.set_re_entry_cooldown(ticker, hours, exit_reason="manual_fill_confirmed")
    print(f"  Re-entry cooldown set: {ticker} blocked from new BUY signals for {hours}h "
          f"(rules/hard_vetoes.py's COOLDOWN check).")

    pattern_id = entry_pattern_id
    if not pattern_id:
        print("  No linked pattern_database entry - nothing to close there.")
        return

    pattern = entry_pattern
    if not pattern:
        print(f"  Linked pattern #{pattern_id} not found (unexpected) - skipping.")
        return
    if pattern["is_closed"]:
        sim_outcome = pattern["outcome_pct"]
        print(f"  Pattern #{pattern_id} was already auto-closed by the time-based "
              f"simulation (outcome {sim_outcome:+.2f}%) before you confirmed this sell. "
              f"Real outcome was {closed['pnl_pct']:+.2f}% - left the simulated row as-is "
              f"(consider this a data quality note, not an error).")
        return

    # §15: hold time is measured from ENTRY, using close_position's figure -
    # the same definition paper_trader and live_trader now use. This measured
    # from the pattern's recorded_at instead, i.e. from SIGNAL time, which is
    # a different quantity: a signal recorded at 09:35 and filled at 10:10
    # produced a hold time 35 minutes longer than the same trade closed by any
    # other path. pattern_database.hold_hours has to mean one thing, since
    # engine/ev_engine.py averages it across rows from all three.
    hold_hours = closed.get("hold_hours", 0.0)
    # §50: exit_kind="manual" is stated here rather than derived. A
    # human-confirmed fill is the one exit whose kind no market condition
    # implies - it is a fact about who closed it, which only this path knows.
    pattern_db.close_trade(pattern_id, closed["pnl_pct"], hold_hours,
                            exit_reason="manual_fill_confirmed",
                            exit_kind="manual")
    print(f"  Closed pattern #{pattern_id} with your REAL outcome ({closed['pnl_pct']:+.2f}%, "
          f"{hold_hours:.1f}h held) - this is what the learning backend will use.")


def cmd_list():
    positions = db.get_all_positions()
    if not positions:
        print("No open positions.")
        return
    for p in positions:
        print(f"{p['ticker']:6s} entry ${p['entry_price']:.2f}  shares {p['shares']}  "
              f"since {p['entry_time']}  pattern_id={p.get('pattern_id')}")


def main():
    parser = argparse.ArgumentParser(description="Confirm a real Robinhood fill executed via Claude Desktop.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_buy = sub.add_parser("buy")
    p_buy.add_argument("ticker")
    p_buy.add_argument("price", type=float)
    p_buy.add_argument("shares", type=float)

    p_sell = sub.add_parser("sell")
    p_sell.add_argument("ticker")
    p_sell.add_argument("price", type=float)

    sub.add_parser("list")

    args = parser.parse_args()
    if args.cmd == "buy":
        cmd_buy(args.ticker, args.price, args.shares)
    elif args.cmd == "sell":
        cmd_sell(args.ticker, args.price)
    elif args.cmd == "list":
        cmd_list()


if __name__ == "__main__":
    main()
