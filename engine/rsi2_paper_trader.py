"""RSI(2) SPY mean-reversion sleeve - PAPER trading (2026-08-02, tier 2 of
Trinath's "ready to use" deployment). Simulates the validated strategy
(rules/rsi2_spy_swing.py) against its OWN dedicated cash ledger and
position table (storage/database.py's rsi2_account/rsi2_positions) - NOT
paper_trader.py's shared paper_account/positions, and NOT wired through
paper_trader.execute_buy/execute_sell at all. See storage/database.py's
CREATE TABLE comment for why: a shared-table trade_mode tag was considered
and rejected in favor of full separation, because the existing
uq_open_position_per_ticker_book unique index is keyed on (ticker,
simulated) only, not trade_mode - a shared table could not actually
guarantee this sleeve's SPY position and a hypothetical future swing SPY
position wouldn't collide at the database level. A dedicated table makes
that impossible by construction instead of merely unlikely.

THIS MODULE NEVER TOUCHES A BROKER. Every fill here is simulated, exactly
like paper_trader.py's own module docstring says about itself - this
module makes the identical claim, independently, about this sleeve.

SIZING: 100% of the sleeve's own reserved cash when entering a trade, 0%
when flat - the "full capital while in a trade" model this strategy was
backtested and walk-forward validated under (see engine/rsi2_mean_
reversion_backtest.py's POSITION SIZING section). There is no risk_pct/
stop-distance concept here the way swing's position_sizing.py has one -
this sleeve's exit is signal-based (RSI(2)/SMA crossing), not a price stop.

CONCURRENCY: none, deliberately. See storage/database.py's comment on the
rsi2_account/rsi2_positions tables for why this is an accepted scope
decision, not an oversight - this sleeve is single-ticker, single-position,
and driven by one scheduled daily script (scripts/rsi2_paper_cycle.py),
not swing's multi-worker ticker scan.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading")


def ensure_seeded(db, cfg: dict) -> dict:
    """Idempotent, mirrors paper_trader.ensure_seeded()'s shape."""
    account = db.get_rsi2_account()
    if account:
        return account
    starting_cash = float(cfg.get("rsi2_sleeve", {}).get("paper_starting_cash", 3000.0))
    account = db.init_rsi2_account(starting_cash)
    logger.info(f"RSI2 sleeve paper account seeded: ${starting_cash:.2f} cash")
    return account


def execute_buy(db, cfg: dict, price: float, entry_date: str = None) -> dict:
    """Buys SPY with 100% of the sleeve's current cash. Returns a summary
    dict, or {} if the buy couldn't happen (already in a position / no
    seeded account / non-positive cash / bad price) - each skip logged, same
    "a skip is itself useful signal" convention as paper_trader.execute_buy."""
    account = ensure_seeded(db, cfg)
    if db.get_open_rsi2_position():
        logger.warning("rsi2_paper_trader.execute_buy: already holds an open position - skipping")
        return {}
    cash = account["cash"]
    if not price or price <= 0 or cash <= 0:
        logger.warning(f"rsi2_paper_trader.execute_buy: nothing to buy with (cash=${cash:.2f}, price={price})")
        return {}

    entry_date = entry_date or datetime.now(timezone.utc).date().isoformat()
    shares = cash / price
    position_id = db.open_rsi2_position(entry_date=entry_date, entry_price=price,
                                         shares=shares, dollar_amount=cash)
    db.adjust_rsi2_cash(-cash)
    logger.info(f"RSI2 sleeve BUY: {shares:.4f} SPY @ ${price:.2f} (${cash:.2f}) on {entry_date}")
    return {"position_id": position_id, "ticker": "SPY", "entry_date": entry_date,
            "entry_price": price, "shares": shares, "dollar_amount": cash}


def execute_sell(db, cfg: dict, price: float, reason: str, exit_date: str = None) -> dict:
    """Sells the sleeve's open position at `price`, crediting cash + realized
    P&L back to rsi2_account. Returns {} if there's nothing open to sell."""
    position = db.get_open_rsi2_position()
    if not position:
        logger.warning("rsi2_paper_trader.execute_sell: no open position to sell")
        return {}
    if not price or price <= 0:
        logger.warning(f"rsi2_paper_trader.execute_sell: bad price ({price}) - refusing to sell")
        return {}

    exit_date = exit_date or datetime.now(timezone.utc).date().isoformat()
    proceeds = position["shares"] * price
    realized_pnl = proceeds - position["dollar_amount"]
    db.close_rsi2_position(position["id"], exit_date=exit_date, exit_price=price,
                            exit_reason=reason, realized_pnl=realized_pnl)
    db.adjust_rsi2_cash(proceeds, realized_pnl_delta=realized_pnl)
    logger.info(f"RSI2 sleeve SELL: {position['shares']:.4f} SPY @ ${price:.2f} "
                f"(${proceeds:.2f}, pnl=${realized_pnl:+.2f}, reason={reason}) on {exit_date}")
    return {"position_id": position["id"], "exit_date": exit_date, "exit_price": price,
            "proceeds": proceeds, "realized_pnl": realized_pnl, "reason": reason}


def snapshot(db, current_price: float = None) -> dict:
    """cash + (marked-to-market, if current_price given) open position value
    + realized P&L to date - the sleeve's own equivalent of paper_trader.
    snapshot(), scoped to just this sleeve's dedicated ledger."""
    account = db.get_rsi2_account()
    if not account:
        return {"seeded": False}
    position = db.get_open_rsi2_position()
    market_value = None
    if position and current_price:
        market_value = position["shares"] * current_price
    total_value = account["cash"] + (market_value or 0.0)
    return {
        "seeded": True,
        "cash": account["cash"],
        "starting_cash": account["starting_cash"],
        "realized_pnl": account["realized_pnl"],
        "open_position": position,
        "market_value": market_value,
        "total_value": total_value,
        "total_return_pct": round((total_value / account["starting_cash"] - 1) * 100, 2)
                             if account["starting_cash"] else None,
    }
