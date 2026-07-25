"""WATCH-mode paper trading engine (2026-07-16, Akhil's ask).

When trading.watch_execute == WATCH, every BUY signal is executed as a
SIMULATED trade that mimics exactly what would happen live:

  - Buy at the signal price, sized by the Position Sizing Engine's
    suggested dollar amount (conviction/risk/crowding adjusted) - the same
    number you'd be told to trade in EXECUTE mode.
  - The simulated position lands in the same `positions` table
    (simulated=1), so rules/sell_rules.py and Loop B
    (engine/position_management.py) manage it with ZERO special-casing -
    stops, trail highs, MAE/MFE, exit scores, all identical to a real fill.
  - Sell happens automatically when the sell rules fire, or when Loop B
    flags an URGENT exit - the same moments you'd be alerted to act live.
  - A single-row purse (`paper_account`) tracks cash: debited on buy,
    credited on sell, so "how much is bought / how much is left / what's
    the P/L" is answerable at any moment (see snapshot()).
  - On close, the linked pattern_database entry is closed with the
    RULE-DRIVEN outcome (not the flat 5-day time-based close), which is
    what the EV engine / walk-forward / Bayesian updater learn from - so
    WATCH mode actively improves the model with realistic exits.

Seeding (per Akhil's choice): the purse starts at
paper_trading.starting_cash from config.yaml, and the CURRENT REAL BOOK is
cloned into the simulated book at its real entry prices - so from day one
the paper portfolio mirrors the actual portfolio, then evolves by the
rules alone.

THIS MODULE never calls Robinhood and never places an order - every fill here
is simulated. That is a property of this file and stays true regardless of
configuration. It is NOT a claim about the platform: engine/live_trader.py
places real orders when its gates are open. See storage/banner.py for the
resolved runtime posture (§6, 2026-07-24 - the previous wording asserted a
platform-wide "no real trades from this codebase" guarantee that stopped being
true on 16 July, when live_trader.py landed).
"""
import logging

logger = logging.getLogger("trading")


def is_watch_mode(cfg: dict) -> bool:
    return str(cfg.get("trading", {}).get("watch_execute", "WATCH")).upper() == "WATCH"


def ensure_seeded(db, cfg: dict) -> dict:
    """Idempotent. First WATCH-mode cycle creates the purse and clones the
    real book; every later call is a cheap single-row SELECT."""
    account = db.get_paper_account()
    if account:
        return account

    starting_cash = float(cfg.get("paper_trading", {}).get("starting_cash", 1000.0))
    account = db.init_paper_account(starting_cash)

    cloned = 0
    for pos in db.get_all_positions(simulated=False):
        # Clone at the REAL entry price/time so unrealized P/L continuity
        # matches the actual portfolio. pattern_id stays None on the clone -
        # the real row keeps the pattern linkage (confirm_fill.py closes it
        # with the real outcome; double-closing the same pattern from both
        # books would corrupt the learning data).
        db.open_position(
            pos["ticker"], pos["entry_price"], pos["shares"], pos["dollar_amount"],
            pattern_id=None, simulated=True, entry_time=pos.get("entry_time"),
            trade_mode="SEED",
        )
        db.log_paper_trade(
            pos["ticker"], "buy", pos["entry_price"], pos["shares"],
            pos["dollar_amount"], reason="seeded_from_real_portfolio",
            trade_mode="SEED",
        )
        cloned += 1

    logger.info(f"Paper account seeded: ${starting_cash:.2f} cash, "
                f"{cloned} real position(s) cloned into the simulated book")
    db.log_ui_event("paper_account_seeded", {
        "starting_cash": starting_cash, "positions_cloned": cloned,
    })
    return db.get_paper_account()


def execute_buy(db, cfg: dict, ticker: str, price: float, position_size=None,
                pattern_id: int = None, trade_mode: str = None,
                buy_score: float = None, prices: dict = None,
                pattern_db=None, entry_seed: dict = None) -> dict:
    """Mimics taking the BUY signal at the signal price. Returns a summary
    dict, or {} if the buy couldn't be simulated (already open / no purse /
    insufficient cash / max positions) - each skip is logged, because 'the
    system wanted to buy but the purse couldn't' is itself useful signal.

    buy_score/prices/pattern_db (2026-07-17, rotation): at max_positions a
    top-tier candidate may rotate out the weakest holding instead of being
    skipped - engine/rotation.py picks the victim (or says no), this
    function sells it through the normal execute_sell path (so the purse is
    credited and the linked pattern closes with the real rotation outcome),
    then proceeds with the buy. `prices` is {ticker: current_price} from
    this cycle's data cache - a victim without a price this cycle can't be
    rotation-sold (paper sells need a fill price), so rotation is skipped.

    entry_seed (2026-07-20): entry_signal_score/entry_regime/setup_type/
    risk_per_share computed by scheduler.py at signal time - see its call
    site for why this exists (db.open_position() itself doesn't take these
    columns). Price-dependent fields (current_stop_price/current_target_price/
    high_watermark_price) are filled in here from the actual fill price
    rather than passed in, same convention confirm_fill.py already uses for
    the manual live-confirm path."""
    if not price or price <= 0:
        return {}

    account = db.get_paper_account() or ensure_seeded(db, cfg)

    if db.get_open_position(ticker, simulated=True):
        return {}  # same setup already held - identical to live behavior

    # ── §10 (Phase 2): the risk gate, per TRADE ────────────────────────────
    # scheduler.py checks once per cycle, before the ticker loop. A cycle that
    # begins at 9 trades and finds 15 qualifying candidates placed all 15.
    # engine/live_trader.py has always re-checked per trade, so the LIVE path
    # was protected and the paper path was not - exactly backwards, since the
    # paper book is the one being used to estimate future behaviour.
    #
    # Placed here, before any state is mutated: after the already-held check
    # (which is not a risk decision and should not consume budget) and before
    # the purse, the position row or the counters are touched.
    from rules.risk_rules import RiskEngine
    risk = RiskEngine(db, cfg, simulated=True).check()
    if not risk["can_trade"]:
        logger.info(f"{ticker}: [PAPER] buy blocked - {risk['reason']}")
        db.log_ui_event("paper_buy_skipped",
                        {"ticker": ticker, "reason": risk["reason"]})
        return {}

    # DAY-specific position cap (2026-07-22, enhancement item #1 - Trinath's
    # "mode-level position caps" ask): checked BEFORE the general max_positions/
    # rotation logic below and deliberately does NOT rotate - day trading is
    # higher-frequency/tighter-risk than swing (see position_sizing.py's
    # day_size_multiplier and stop_state_machine.py's tighter DAY stop), and
    # rotating out a SWING holding to make room for one more DAY leg would
    # cannibalize a different risk budget for a same-session trade. Simply
    # skips the buy - the next cycle (or an EOD flatten freeing a DAY slot)
    # gets another chance. Config-driven, defaults to trading.max_positions
    # (i.e. no additional restriction) when unset, so existing configs are
    # unaffected until max_day_positions is explicitly set lower.
    if str(trade_mode or "").upper() == "DAY":
        max_day_positions = int(cfg.get("trading", {}).get(
            "max_day_positions", cfg.get("trading", {}).get("max_positions", 10)))
        open_day_count = sum(
            1 for p in db.get_all_positions(simulated=True)
            if str(p.get("trade_mode") or "").upper() == "DAY")
        if open_day_count >= max_day_positions:
            logger.info(f"{ticker}: [PAPER] DAY buy skipped - "
                        f"{open_day_count}/{max_day_positions} DAY positions open")
            return {}

    # trade_mode='SEED' rows (robinhood_sync.py's seed-paper command, cloning
    # the real Robinhood account into the paper book for display) are
    # deliberately excluded here (2026-07-23, Trinath's ask) - they're an
    # informational mirror of a real account, not a position this engine is
    # managing, and counting them toward max_positions could silently starve
    # out genuine new WATCH signals just because the real account happens to
    # be holding a lot of names.
    max_positions = int(cfg.get("trading", {}).get("max_positions", 10))
    open_count = sum(
        1 for p in db.get_all_positions(simulated=True)
        if str(p.get("trade_mode") or "").upper() != "SEED")
    if open_count >= max_positions:
        from engine import rotation
        victim = rotation.find_rotation_victim(db, cfg, ticker, buy_score,
                                                simulated=True)
        victim_price = (prices or {}).get(victim["ticker"]) if victim else None
        if victim and not victim_price:
            logger.info(f"{ticker}: [PAPER] rotation skipped - no current "
                        f"price for victim {victim['ticker']} this cycle")
            victim = None
        if not victim:
            logger.info(f"{ticker}: [PAPER] buy skipped - {open_count}/{max_positions} positions open")
            return {}
        closed = execute_sell(db, victim["ticker"], victim_price,
                               reason=victim["reason"], pattern_db=pattern_db, cfg=cfg)
        if not closed:
            logger.warning(f"{ticker}: [PAPER] rotation sell of {victim['ticker']} "
                           f"failed - buy skipped")
            return {}
        db.log_rotation("PAPER", ticker, buy_score, victim["ticker"],
                         victim["health"], victim["days_held"], victim["reason"])
        db.log_ui_event("rotation", {
            "book": "PAPER", "candidate": ticker, "candidate_score": buy_score,
            "victim": victim["ticker"], "victim_health": victim["health"],
        })
        account = db.get_paper_account()  # purse changed by the sell

    # Position Sizing Engine's suggested $ (what you'd be told to trade
    # live); flat trade_size_usd only as fallback when sizing didn't run.
    amount = None
    if position_size is not None and getattr(position_size, "applicable", False):
        amount = float(getattr(position_size, "suggested_dollar_amount", 0) or 0)
    if not amount or amount <= 0:
        amount = float(cfg.get("trading", {}).get("trade_size_usd", 100))

    if account["cash"] < amount:
        logger.info(f"{ticker}: [PAPER] buy skipped - purse ${account['cash']:.2f} "
                    f"< suggested ${amount:.2f} (insufficient cash, exactly as live)")
        db.log_ui_event("paper_buy_skipped", {
            "ticker": ticker, "reason": "insufficient_cash",
            "cash": account["cash"], "needed": amount,
        })
        return {}

    # Mode attribution (2026-07-16): stamp which trading mode (SWING/DAY/
    # HYBRID) was active when this buy fired, so every trade can be
    # dissected by the strategy category it was bought under.
    trade_mode = (trade_mode or cfg.get("trading", {}).get("mode", "SWING")).upper()
    shares = amount / price

    # ── §14 (Phase 2): the buy is ONE transaction ──────────────────────────
    #
    # Every check above this line is still worth making - they decide STRATEGY
    # (rotate a holding out, skip a DAY leg, log why the purse said no) and
    # they produce the log lines that make a skipped buy legible. What they
    # cannot do is hold. Between the already-held check at the top of this
    # function and this point, six ThreadPoolExecutor workers run the same
    # code on different tickers, and nothing stopped two of them agreeing that
    # there were 24 of 25 positions open, or that the purse held $100.
    #
    # So the checks above stay advisory and this call is authoritative: the
    # duplicate check, the cap and the cash debit happen inside a single
    # transaction, under a lock, against a table that now has a unique index.
    # None means another worker got there first - a normal outcome on a
    # parallel cycle, not an error.
    opened = db.try_open_position(
        ticker, price, shares, amount, pattern_id=pattern_id, simulated=True,
        trade_mode=trade_mode, max_positions=max_positions,
        debit_paper_cash=amount)
    if opened is None:
        logger.info(f"{ticker}: [PAPER] buy skipped - already held, at cap, or "
                    f"insufficient cash (lost the race to a parallel worker)")
        db.log_ui_event("paper_buy_skipped",
                        {"ticker": ticker, "reason": "lost_open_race"})
        return {}

    # §51 (Phase 2.5): the pattern learns which position it became.
    #
    # This is the only line in the codebase where both ids exist at once.
    # PatternDatabase.record_entry() runs back in scheduler.py at SIGNAL time,
    # before any position row exists, which is why pattern_database.trade_id
    # was NULL on every row it had ever written. Without it the only path from
    # a pattern to its true intraday excursion runs transitively through
    # positions.pattern_id, and mae_mfe_data.trade_id collides badly enough
    # that the transitive join mixes tickers - see get_pattern_excursions().
    if pattern_id:
        db.link_pattern_to_trade(pattern_id, opened.get("id"))

    if entry_seed:
        seed = dict(entry_seed)
        rps = seed.get("risk_per_share") or 0
        if rps > 0:
            seed["current_stop_price"] = price - rps
            seed["current_target_price"] = price + rps * 3
        seed["stop_state"] = "INITIAL_RISK"
        seed["high_watermark_price"] = price
        # §16: simulated=True is load-bearing, not decoration. Without the
        # book scope this UPDATE matched an open position in EITHER book, so
        # this line - a $100 paper entry - could write its stop onto a real
        # SYNC holding of the same ticker.
        db.update_position_by_ticker(ticker, seed, simulated=True)
    db.log_paper_trade(ticker, "buy", price, shares, amount,
                        reason="buy_signal", pattern_id=pattern_id, trade_mode=trade_mode)
    # §7 (Phase 2): the paper book now consumes the daily budget. Placed AFTER
    # the fill is recorded, so a skipped or failed buy never burns budget.
    db.record_trade_placed(simulated=True)
    # Re-read rather than computing account['cash'] - amount. That arithmetic
    # was correct when this function was the only writer, but under §14 a
    # parallel worker may have debited the purse between the snapshot taken at
    # the top of this call and the debit that just happened - so the
    # subtraction would print a balance that was never true. The purse is the
    # first number an operator reconciles against the UI; it should be read,
    # not inferred.
    cash_after = float((db.get_paper_account() or {}).get("cash", 0.0) or 0.0)
    logger.info(f"{ticker}: [PAPER] BOUGHT {shares:.4f} sh @ ${price:.2f} "
                f"(${amount:.2f}) [{trade_mode}] - purse now ${cash_after:.2f}")
    db.log_ui_event("paper_buy", {
        "ticker": ticker, "price": price, "shares": round(shares, 4),
        "dollar_amount": round(amount, 2), "cash_after": round(cash_after, 2),
        "trade_mode": trade_mode,
    })
    return {"ticker": ticker, "price": price, "shares": shares, "dollar_amount": amount}


def execute_sell(db, ticker: str, price: float, reason: str, pattern_db=None,
                  cfg: dict = None) -> dict:
    """Closes the simulated position at the current price and settles the
    purse. Closes the linked pattern with the rule-driven outcome so the
    learning loop trains on realistic exits. Returns {} if nothing to sell.

    DELIBERATELY HAS NO RISK-ENGINE CHECK (§10). execute_buy() gained one;
    this did not, and must not. Being unable to close a losing position
    because you already hit the daily trade count is how a small loss becomes
    a large one - the limit would convert itself from a risk control into a
    risk. This mirrors the reasoning already documented in live_trader's sell
    path and in the kill switch, both of which block entries and never exits.

    The sell still INCREMENTS the counter (§7), so an exit consumes budget for
    the purposes of the next entry. It is only never BLOCKED by it.
    """
    if not price or price <= 0:
        return {}
    closed = db.close_position(ticker, price, simulated=True)
    if not closed:
        return {}

    proceeds = closed["shares"] * price if closed.get("shares") else 0.0
    db.adjust_paper_cash(proceeds, realized_pnl_delta=closed.get("pnl", 0.0))
    db.log_paper_trade(ticker, "sell", price, closed.get("shares"), proceeds,
                        reason=reason, pattern_id=closed.get("pattern_id"),
                        pnl=closed.get("pnl"), pnl_pct=closed.get("pnl_pct"),
                        trade_mode=closed.get("trade_mode"))
    # §7: sells count too. max_trades_per_day limits how much the system may
    # DO in a day, and a runaway exit loop is exactly as damaging as a runaway
    # entry loop - the MAN sequence in the July data (bought and sold four
    # times in 30 hours) is that loop starting.
    db.record_trade_placed(simulated=True)

    # Learning: rule-driven outcome replaces the flat time-based close.
    if pattern_db is not None and closed.get("pattern_id"):
        # §15: hold_hours comes from close_position, which is now the single
        # place it is computed. This used to re-derive it from
        # closed["entry_time"] against a fresh clock reading, and the MAE/MFE
        # block below re-derived it a third time from a re-fetched row - which
        # is how ADPT ended up recorded as 6.34h in one table and 5.0h in
        # another. Three computations, three answers.
        try:
            pattern_db.close_trade(closed["pattern_id"], closed["pnl_pct"],
                                    closed.get("hold_hours", 0.0),
                                    exit_reason=f"paper_{reason}")
        except Exception as e:
            logger.error(f"{ticker}: [PAPER] pattern close failed: {e}", exc_info=True)

    # MAE/MFE recording (2026-07-17): confirm_fill.py's real-fill sell path
    # already did this (feeds engine/mae_mfe_engine.py's evaluate_mae_percentile()
    # historical-winner comparison, which Loop B calls every cycle) - this
    # WATCH-mode path never did, so mae_mfe_data stayed almost empty even
    # after dozens of paper closes, meaning the live "is this drawdown normal
    # for a winner" check had ~nothing to compare against. Mirrors
    # confirm_fill.py's exact shape: re-fetch the closed row (close_position()'s
    # own return value is a minimal subset without setup_type/entry_regime/
    # MAE-MFE fields) filtered to the simulated book specifically, so a
    # same-ticker real position closed around the same time can't be picked
    # up by mistake.
    try:
        trade = db.get_closed_trade_for_ticker(ticker, simulated=True)
        if trade:
            # §15: the AUTHORITATIVE values come from close_position's own
            # arithmetic - the same numbers written to paper_trades. This
            # block used to recompute pnl_pct's companion figures from the
            # re-fetched row, which is what produced two different answers for
            # ADPT (-1.88% over 6.34h in paper_trades, -3.20% over 5.0h in
            # mae_mfe_data - one trade, two records, neither reconcilable
            # against the other). The re-fetch itself stays: it is the only
            # source of setup_type/entry_regime/MAE/MFE, which
            # close_position's return value does not carry.
            trade["pnl_pct"] = closed["pnl_pct"]
            trade["pnl"] = closed["pnl"]
            trade["hold_hours"] = closed.get("hold_hours", 0.0)
            from engine.mae_mfe_engine import record_completed
            record_completed(trade)
    except Exception as e:
        logger.warning(f"{ticker}: [PAPER] MAE/MFE recording failed (non-fatal): {e}")

    logger.info(f"{ticker}: [PAPER] SOLD @ ${price:.2f} ({reason}) - "
                f"P/L ${closed['pnl']:+.2f} ({closed['pnl_pct']:+.2f}%)")
    db.log_ui_event("paper_sell", {
        "ticker": ticker, "price": price, "reason": reason,
        "pnl": round(closed["pnl"], 2), "pnl_pct": round(closed["pnl_pct"], 2),
    })

    # §9 (Phase 2): check the breaker after EVERY close, not once per cycle.
    # A cycle can take twelve minutes; a stop cascade inside one cycle would
    # otherwise run to completion before anything checked. cfg is threaded in
    # from the call site rather than re-read here - this path has five callers
    # and a hidden disk read in each is both slow and a source of
    # inconsistency.
    try:
        from rules.risk_rules import trip_kill_switch_if_needed
        trip_kill_switch_if_needed(db, cfg, simulated=True)
    except Exception as e:
        logger.error(f"{ticker}: [PAPER] kill-switch check failed: {e}", exc_info=True)

    return closed


def _exit_prices(p: dict, cfg: dict) -> tuple:
    """Rule-derived (stop_price, target_price) for an open position - the
    prices at which the sell rules would close it, shown in the UI so you
    always know both exits in advance. Mirrors rules/sell_rules.py exactly:
    stop = Loop B's dynamic stop once it exists, else the flat
    stop_loss.pct below entry; target = entry + risk_per_share * r_multiple
    (ATR-scaled) once risk_per_share is set, else the flat take_profit.pct."""
    rules = ((cfg or {}).get("sell_rules", {}) or {}).get("rules", {}) or {}
    entry = p.get("entry_price") or 0
    stop = p.get("current_stop_price")
    if not stop and entry and rules.get("stop_loss", {}).get("enabled", True):
        stop = entry * (1 - float(rules.get("stop_loss", {}).get("pct", 5.0)) / 100)
    target = p.get("current_target_price")
    if not target and entry:
        tp = rules.get("take_profit", {}) or {}
        if tp.get("enabled", True):
            rps = p.get("risk_per_share") or 0
            if rps > 0:
                target = entry + rps * float(tp.get("r_multiple", 3.0))
            else:
                target = entry * (1 + float(tp.get("pct", 10.0)) / 100)
    return (round(stop, 2) if stop else None, round(target, 2) if target else None)


def check_exit_triggers(position: dict, price: float, cfg: dict) -> str | None:
    """Pure decision: given a live price, which exit rule (if any) fires for
    this open position RIGHT NOW. Used by scheduler.py's intra-cycle price
    watch (2026-07-16, Akhil's ask: 'a rapid drop in 5 mins would impact a
    lot') so a stop/target cross triggers a sell within seconds instead of
    waiting for the next scan cycle. Mirrors rules/sell_rules.py's hard
    exits: dynamic-or-flat stop loss, R-multiple-or-flat take profit, and
    the trailing stop off trail_high. Returns a short reason string or None."""
    if not price or price <= 0:
        return None
    stop, target = _exit_prices(position, cfg)
    if stop and price <= stop:
        return f"stop_loss (price {price:.2f} <= stop {stop:.2f})"
    if target and price >= target:
        return f"take_profit (price {price:.2f} >= target {target:.2f})"
    rules = ((cfg or {}).get("sell_rules", {}) or {}).get("rules", {}) or {}
    ts = rules.get("trailing_stop", {}) or {}
    entry = position.get("entry_price") or 0
    trail_high = max(position.get("trail_high") or 0, entry, price if price > entry else 0)
    if (ts.get("enabled", True) and entry and trail_high > entry
            and price <= trail_high * (1 - float(ts.get("pct", 3.0)) / 100)):
        return (f"trailing_stop (price {price:.2f} fell {ts.get('pct', 3.0)}% "
                f"from high {trail_high:.2f})")
    return None


def snapshot(db, prices: dict = None, cfg: dict = None) -> dict:
    """Full purse accounting: cash left, cost of what's bought, market value
    + unrealized P/L (for tickers whose current price is known), realized P/L,
    and total portfolio value - 'just like the original trade'."""
    account = db.get_paper_account()
    if not account:
        return {"seeded": False}

    prices = prices or {}
    positions = db.get_all_positions(simulated=True)
    invested_cost = sum(p.get("dollar_amount") or 0 for p in positions)

    pos_details, market_value, priced_cost = [], 0.0, 0.0
    for p in positions:
        cur = prices.get(p["ticker"])
        value = (p["shares"] * cur) if (cur and p.get("shares")) else None
        unreal = (value - p["dollar_amount"]) if value is not None else None
        if value is not None:
            market_value += value
            priced_cost += p["dollar_amount"] or 0
        stop_price, target_price = _exit_prices(p, cfg)
        pos_details.append({
            "stop_price": stop_price, "target_price": target_price,
            "ticker": p["ticker"], "entry_price": p["entry_price"],
            "shares": p["shares"], "cost": p["dollar_amount"],
            "current_price": cur, "market_value": round(value, 2) if value is not None else None,
            "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
            "entry_time": p.get("entry_time"),
            "trade_mode": p.get("trade_mode"),
        })

    # Unpriced positions are carried at cost so total value never silently
    # drops just because a quote was unavailable this cycle.
    total_value = account["cash"] + market_value + (invested_cost - priced_cost)
    return {
        "seeded": True,
        "starting_cash": account["starting_cash"],
        "cash": round(account["cash"], 2),
        "invested_cost": round(invested_cost, 2),
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(market_value - priced_cost, 2),
        "realized_pnl": round(account.get("realized_pnl") or 0.0, 2),
        "total_value": round(total_value, 2),
        "total_return_pct": round((total_value - account["starting_cash"])
                                   / account["starting_cash"] * 100, 2) if account["starting_cash"] else 0.0,
        "open_positions": pos_details,
        "n_open": len(positions),
    }
