"""LIVE Robinhood order execution - BUILT 2026-07-16, HARD-DISABLED
2026-07-17 (see LIVE_EXECUTION_ENABLED below). Execution is Claude-Desktop-
only: the scheduler writes output/trade_prompt.md, Claude Desktop (with the
Robinhood MCP) places any orders, confirm_fill.py records the fills.

Original design notes (still accurate if ever re-enabled):

This module is the ONLY place in the platform that can place a real order.
It calls robin_stocks directly (the read-only robinhood-mcp server exposes
zero trading tools by design, so writes cannot go through it). Everything
else - signals, sizing, sell rules, Loop B, the price watch - stays exactly
the same engine that paper trading already exercises; this just swaps the
fill from simulated to real when, and only when, EVERY gate below is open:

  1. trading.watch_execute == "EXECUTE"      (config.yaml / Control tab)
  2. trading.auto_trade   == true            (config.yaml / Control tab
                                              toggle - token-protected)
  3. risk.kill_switch_triggered == false
  4. Robinhood credentials present and login succeeds
  5. Per-trade guards: not already holding, max_positions,
     risk.max_position_size_usd cap, risk.max_trades_per_day budget,
     live buying-power check

With ANY gate closed this module does nothing and the platform behaves as
before (WATCH mode paper-trades; EXECUTE without auto_trade just writes
trade_prompt.md for manual execution - the old Claude Desktop flow still
works, it's simply no longer the only way).

Orders are FRACTIONAL MARKET orders (buy by dollar amount, sell by share
count - matching how the paper book sizes positions), polled to fill for up
to FILL_WAIT_SECONDS; an unfilled order is cancelled and NOT recorded, so
the local book can never drift ahead of the account. Every fill is recorded
to the same `positions` (real book) + `trades` tables confirm_fill.py uses,
increments daily_stats.trades_placed (which risk_rules' max_trades_per_day
reads), and closes the linked pattern on exit so the learning loop trains
on real outcomes.

UNOFFICIAL API WARNING (same as robinhood-mcp's README): robin_stocks is an
unofficial client. Robinhood can change or rate-limit it at any time. The
circuit breaker below stops repeated failures from hammering your account.
"""
import logging
import os
import threading
import time
from datetime import datetime

from mcp_clients.base import SourceCircuitBreaker

logger = logging.getLogger("trading")

FILL_WAIT_SECONDS = 60      # market orders in-hours fill in seconds; this is generous
_POLL_INTERVAL = 2.0

_login_lock = threading.Lock()
_login_state = {"ok": False, "checked_at": 0.0, "error": ""}
_LOGIN_FAIL_CACHE_SECONDS = 300  # same 5-min fail cache robinhood-mcp uses

breaker = SourceCircuitBreaker("robinhood-orders", fail_threshold=3, cooldown_seconds=1800)


# ── MASTER SWITCH (2026-07-17, Akhil's design) ──────────────────────────────
# Direct in-process execution was reverted to off-by-default the day it was
# built (unofficial-API risk vs the Claude-Desktop human checkpoint), then
# made a CONTROLLED switch: trading.live_execution_enabled in config.yaml,
# default false, hot-reloaded. The only writer is server.py's
# /api/live_execution endpoint, which requires BOTH the auth token AND
# typing the exact confirmation phrase (LIVE_EXECUTION_CONFIRM_PHRASE) - it
# cannot be flipped by accident, and this module keeps every per-trade
# guard regardless.
#
# THREE gates must ALL be on before any order code runs:
#   1. trading.live_execution_enabled  (master switch, typed-phrase protected)
#   2. trading.watch_execute == EXECUTE
#   3. trading.auto_trade == true
# With the master switch off, execution is Claude-Desktop-only via
# output/trade_prompt.md + confirm_fill.py, exactly as before.
LIVE_EXECUTION_CONFIRM_PHRASE = "ENABLE LIVE TRADING"


# §38.4 - the environment-level veto. Set by scripts/tp for every version that
# is not the designated primary, and settable by hand for any run that must not
# be able to reach the account.
FORCE_PAPER_ENV = "TP_FORCE_PAPER"


def is_force_paper() -> bool:
    """True when this process is vetoed from live execution regardless of config."""
    return os.getenv(FORCE_PAPER_ENV) == "1"


def is_live_execution_enabled(cfg: dict) -> bool:
    """The master switch (Control tab, typed-phrase + token gated), with an
    environment-level veto layered above it.

    Config alone cannot be trusted here. An old tag carries an OLD config.yaml,
    and v1.0.0's committed config has live_execution_enabled: true, auto_trade:
    true and watch_execute: EXECUTE baked into it - so simply running that tag
    to reproduce a backtest result would arm live trading against the real
    account. §38 makes every past version runnable; this veto is what stops
    reproducibility from extending to reproducing the unsafe configuration.

    Two schedulers both believing they own the account is the single worst
    outcome the side-by-side arrangement could produce, so scripts/tp sets
    TP_FORCE_PAPER=1 on every version except the one named in ~/tp/PRIMARY.
    """
    if is_force_paper():
        return False
    return bool((cfg.get("trading", {}) or {}).get("live_execution_enabled", False))


def is_live_mode(cfg: dict) -> bool:
    """True only when ALL THREE gates are on: master switch + EXECUTE mode +
    auto_trade. Anything less and this codebase places no orders."""
    if not is_live_execution_enabled(cfg):
        return False
    t = cfg.get("trading", {}) or {}
    return (str(t.get("watch_execute", "WATCH")).upper() == "EXECUTE"
            and bool(t.get("auto_trade", False)))


def _rh():
    import robin_stocks.robinhood as rh
    return rh


def _login() -> bool:
    """Idempotent login; robin_stocks caches its session in
    ~/.tokens/robinhood.pickle so only the first call does a real login.
    Failures are cached 5 min so a bad credential can't hammer the account."""
    with _login_lock:
        now = time.time()
        if _login_state["ok"]:
            return True
        if now - _login_state["checked_at"] < _LOGIN_FAIL_CACHE_SECONDS and _login_state["error"]:
            return False
        user = os.getenv("ROBINHOOD_USERNAME", "").strip()
        pw = os.getenv("ROBINHOOD_PASSWORD", "").strip()
        if not user or not pw:
            _login_state.update(checked_at=now, error="no credentials in .env")
            logger.warning("live_trader: ROBINHOOD_USERNAME/PASSWORD not set - live trading unavailable")
            return False
        try:
            rh = _rh()
            kwargs = {"store_session": True}
            totp_secret = os.getenv("ROBINHOOD_TOTP_SECRET", "").strip()
            if totp_secret:
                import pyotp
                kwargs["mfa_code"] = pyotp.TOTP(totp_secret).now()
            rh.login(user, pw, **kwargs)
            _login_state.update(ok=True, checked_at=now, error="")
            logger.info("live_trader: Robinhood login OK (session cached)")
            return True
        except Exception as e:
            _login_state.update(ok=False, checked_at=now, error=str(e)[:200])
            logger.error(f"live_trader: Robinhood login failed: {e}")
            return False


def _account_number(cfg: dict) -> str | None:
    """config.yaml account.robinhood_account_number - set this to your
    Robinhood AGENTIC account number (2026-07-17, Akhil) so orders and
    buying-power checks target the account designated for agent trading,
    not the primary individual account robin_stocks defaults to. Empty =
    primary account (old behavior)."""
    acct = str((cfg.get("account", {}) or {}).get("robinhood_account_number", "") or "").strip()
    return acct or None


def _buying_power(account_number: str = None) -> float | None:
    try:
        rh = _rh()
        profile = rh.profiles.load_account_profile(account_number=account_number) or {}
        for k in ("buying_power", "cash_available_for_withdrawal", "cash"):
            v = profile.get(k)
            if v not in (None, ""):
                return float(v)
    except Exception as e:
        logger.warning(f"live_trader: buying power check failed: {e}")
    return None


def _wait_for_fill(order_id: str) -> dict | None:
    """Polls the order until filled; cancels and returns None if it doesn't
    fill inside FILL_WAIT_SECONDS (the local book must never record a fill
    the account doesn't have)."""
    rh = _rh()
    deadline = time.time() + FILL_WAIT_SECONDS
    info = {}
    while time.time() < deadline:
        try:
            info = rh.orders.get_stock_order_info(order_id) or {}
        except Exception as e:
            logger.warning(f"live_trader: order poll failed: {e}")
            info = {}
        state = (info.get("state") or "").lower()
        if state == "filled":
            return info
        if state in ("cancelled", "rejected", "failed"):
            logger.warning(f"live_trader: order {order_id} ended {state}")
            return None
        time.sleep(_POLL_INTERVAL)
    try:
        rh.orders.cancel_stock_order(order_id)
        logger.warning(f"live_trader: order {order_id} unfilled after "
                       f"{FILL_WAIT_SECONDS}s - cancelled, nothing recorded")
    except Exception as e:
        logger.error(f"live_trader: could not cancel unfilled order {order_id}: {e} "
                     f"- VERIFY IN THE ROBINHOOD APP, the local book did not record it")
    return None


def _fill_details(info: dict, fallback_price: float) -> tuple:
    """(avg_price, quantity) from a filled-order payload, defensively."""
    def _f(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    price = _f(info.get("average_price")) or _f(info.get("price")) or fallback_price
    qty = _f(info.get("cumulative_quantity")) or _f(info.get("quantity"))
    return price, qty


def execute_buy_live(db, cfg: dict, ticker: str, price: float, position_size=None,
                     pattern_id: int = None, trade_mode: str = None,
                     buy_score: float = None, pattern_db=None,
                     entry_seed: dict = None) -> dict:
    """Places a real fractional market buy for the same dollar amount the
    paper engine would have used. Returns fill summary dict or {} (every
    skip is logged - 'wanted to buy but a guard said no' is signal).

    buy_score/pattern_db (2026-07-17, rotation): at max_positions a top-tier
    candidate may rotate out the weakest holding instead of being skipped -
    engine/rotation.py picks the victim (or says no), the victim is closed
    through the normal execute_sell_live path (market sell, every guard and
    the pattern-close intact), and only if that sell actually FILLS does the
    buy proceed. A failed rotation sell means no buy - the book never goes
    over cap and a position is never abandoned half-rotated."""
    if not is_live_mode(cfg):
        return {}
    if cfg.get("risk", {}).get("kill_switch_triggered"):
        logger.warning(f"{ticker}: [LIVE] buy blocked - kill switch active")
        return {}
    if not breaker.available():
        return {}
    if db.get_open_position(ticker, simulated=False):
        return {}
    # Daily trade budget BEFORE the rotation branch (order matters: a
    # rotation SELLS first, and selling a healthy-enough-to-hold position
    # only to have this budget then block the replacement buy would leave a
    # slot empty for nothing).
    trades_today = (db.get_daily_stats() or {}).get("trades_placed", 0)
    max_trades = int(cfg.get("risk", {}).get("max_trades_per_day", 10))
    if trades_today >= max_trades:
        logger.warning(f"{ticker}: [LIVE] buy blocked - {trades_today}/{max_trades} trades today")
        return {}
    # DAY-specific position cap (2026-07-22, enhancement item #1) - mirrors
    # paper_trader.execute_buy's identical check; see its comment for the
    # full rationale (no rotation for DAY overflow, config-driven, no-op
    # unless trading.max_day_positions is explicitly set).
    if str(trade_mode or "").upper() == "DAY":
        max_day_positions = int(cfg.get("trading", {}).get(
            "max_day_positions", cfg.get("trading", {}).get("max_positions", 10)))
        open_day_count = sum(
            1 for p in db.get_all_positions(simulated=False)
            if str(p.get("trade_mode") or "").upper() == "DAY")
        if open_day_count >= max_day_positions:
            logger.info(f"{ticker}: [LIVE] DAY buy blocked - "
                        f"{open_day_count}/{max_day_positions} DAY positions open")
            return {}
    # trade_mode='SYNC' rows (engine/account_sync.py's auto-import of real
    # Robinhood holdings, config.yaml account.auto_sync) are deliberately
    # excluded here (2026-07-23, Trinath's ask, same rationale as
    # paper_trader.py's SEED exclusion) - they're positions the account
    # happens to hold, not ones this engine chose to enter, and counting
    # them toward max_positions could silently block real trading decisions
    # just because the linked account holds a lot of unrelated names.
    max_positions = int(cfg.get("trading", {}).get("max_positions", 10))
    open_count = sum(
        1 for p in db.get_all_positions(simulated=False)
        if str(p.get("trade_mode") or "").upper() != "SYNC")
    if open_count >= max_positions:
        from engine import rotation
        victim = rotation.find_rotation_victim(db, cfg, ticker, buy_score,
                                                simulated=False)
        if not victim:
            logger.info(f"{ticker}: [LIVE] buy skipped - {open_count}/{max_positions} real positions open")
            return {}
        closed = execute_sell_live(db, cfg, victim["ticker"],
                                    reason=victim["reason"], pattern_db=pattern_db)
        if not closed:
            logger.warning(f"{ticker}: [LIVE] rotation sell of {victim['ticker']} "
                           f"did not fill - buy skipped, book unchanged")
            return {}
        db.log_rotation("LIVE", ticker, buy_score, victim["ticker"],
                         victim["health"], victim["days_held"], victim["reason"])
        db.log_ui_event("rotation", {
            "book": "LIVE", "candidate": ticker, "candidate_score": buy_score,
            "victim": victim["ticker"], "victim_health": victim["health"],
        })

    amount = None
    if position_size is not None and getattr(position_size, "applicable", False):
        amount = float(getattr(position_size, "suggested_dollar_amount", 0) or 0)
    if not amount or amount <= 0:
        amount = float(cfg.get("trading", {}).get("trade_size_usd", 100))
    amount = min(amount, float(cfg.get("risk", {}).get("max_position_size_usd", 500)))

    if not _login():
        return {}
    acct = _account_number(cfg)
    bp = _buying_power(acct)
    if bp is not None and bp < amount:
        logger.warning(f"{ticker}: [LIVE] buy skipped - buying power ${bp:.2f} < ${amount:.2f}")
        return {}

    try:
        rh = _rh()
        logger.info(f"{ticker}: [LIVE] placing market BUY ${amount:.2f} (signal price ${price:.2f})"
                    + (f" [account {acct}]" if acct else ""))
        order = rh.orders.order_buy_fractional_by_price(
            ticker, round(amount, 2), account_number=acct,
            timeInForce="gfd", extendedHours=False) or {}
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"no order id returned: {str(order)[:200]}")
        info = _wait_for_fill(order_id)
        if not info:
            breaker.record(False, error="buy did not fill")
            return {}
        fill_price, qty = _fill_details(info, price)
        if not qty:
            qty = amount / fill_price if fill_price else 0
        breaker.record(True)
        trade_mode = (trade_mode or cfg.get("trading", {}).get("mode", "SWING")).upper()
        db.open_position(ticker, fill_price, qty, round(fill_price * qty, 2),
                          pattern_id=pattern_id, simulated=False, trade_mode=trade_mode)
        if entry_seed:
            # 2026-07-20: same entry-context seeding confirm_fill.py already
            # does for the manual live-confirm path (entry_signal_score/
            # entry_regime/setup_type/risk_per_share) - see paper_trader.
            # execute_buy's docstring / scheduler.py's call site for the full
            # story. Priced off fill_price (what was actually paid), not the
            # signal price this order was placed against.
            seed = dict(entry_seed)
            rps = seed.get("risk_per_share") or 0
            if rps > 0:
                seed["current_stop_price"] = fill_price - rps
                seed["current_target_price"] = fill_price + rps * 3
            seed["stop_state"] = "INITIAL_RISK"
            seed["high_watermark_price"] = fill_price
            db.update_position_by_ticker(ticker, seed)
        db.log_trade(ticker, "buy", round(fill_price * qty, 2), shares=qty,
                      fill_price=fill_price, order_id=order_id, status="filled")
        logger.info(f"{ticker}: [LIVE] FILLED buy {qty:.4f} sh @ ${fill_price:.2f} [{trade_mode}]")
        db.log_ui_event("live_buy", {"ticker": ticker, "price": fill_price,
                                      "shares": round(qty, 4), "trade_mode": trade_mode})
        return {"ticker": ticker, "price": fill_price, "shares": qty, "order_id": order_id}
    except Exception as e:
        breaker.record(False, error=str(e)[:200])
        logger.error(f"{ticker}: [LIVE] buy failed: {e}", exc_info=True)
        return {}


def execute_sell_live(db, cfg: dict, ticker: str, reason: str, pattern_db=None,
                       require_auto_trade: bool = True) -> dict:
    """Closes the REAL position with a fractional market sell. The kill
    switch deliberately does NOT block sells - being unable to exit risk is
    worse than being unable to add it.

    require_auto_trade=True (default, every AUTOMATED call site - scheduler.py's
    cycle/Loop-B/price-watch paths): gates on the full is_live_mode() (master
    switch + watch_execute==EXECUTE + auto_trade) - unchanged from before this
    parameter existed.

    require_auto_trade=False (server.py's /api/real/sell, 2026-07-24, Trinath's
    ask for a manual Sell button on real positions that places an ACTUAL
    order): gates on just the master switch (live_execution_enabled) - a
    human explicitly clicking Sell for one specific ticker (token + typed-
    ticker confirmation at the API layer) is a deliberate one-off action, not
    an automated trading decision, so it shouldn't require EXECUTE mode/
    auto_trade to be armed - those two gate whether the SCHEDULER may decide
    to trade on its own, not whether an explicit human click can act."""
    if require_auto_trade:
        if not is_live_mode(cfg):
            return {}
    elif not is_live_execution_enabled(cfg):
        return {}
    if not breaker.available():
        return {}
    pos = db.get_open_position(ticker, simulated=False)
    if not pos or not pos.get("shares"):
        return {}
    if not _login():
        return {}
    try:
        rh = _rh()
        acct = _account_number(cfg)
        shares = float(pos["shares"])
        logger.info(f"{ticker}: [LIVE] placing market SELL {shares:.4f} sh ({reason})"
                    + (f" [account {acct}]" if acct else ""))
        # robin_stocks renamed this between releases (older:
        # order_sell_fractional_by_shares, current: order_sell_fractional_
        # by_quantity) - resolve whichever the installed version has.
        _sell_fn = (getattr(rh.orders, "order_sell_fractional_by_shares", None)
                    or getattr(rh.orders, "order_sell_fractional_by_quantity"))
        order = _sell_fn(
            ticker, shares, account_number=acct,
            timeInForce="gfd", extendedHours=False) or {}
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(f"no order id returned: {str(order)[:200]}")
        info = _wait_for_fill(order_id)
        if not info:
            breaker.record(False, error="sell did not fill")
            return {}
        fill_price, _ = _fill_details(info, 0.0)
        if not fill_price:
            raise RuntimeError("filled sell returned no price")
        breaker.record(True)
        closed = db.close_position(ticker, fill_price, simulated=False)
        db.log_trade(ticker, "sell", round(fill_price * shares, 2), shares=shares,
                      fill_price=fill_price, order_id=order_id, status="filled")
        if pattern_db is not None and closed.get("pattern_id"):
            try:
                entry = datetime.fromisoformat(closed["entry_time"])
                hold_hours = (datetime.utcnow() - entry).total_seconds() / 3600
                pattern_db.close_trade(closed["pattern_id"], closed["pnl_pct"],
                                        hold_hours, exit_reason=f"live_{reason}")
            except Exception as e:
                logger.error(f"{ticker}: [LIVE] pattern close failed: {e}")
        logger.info(f"{ticker}: [LIVE] SOLD @ ${fill_price:.2f} ({reason}) - "
                    f"P/L ${closed.get('pnl', 0):+.2f} ({closed.get('pnl_pct', 0):+.2f}%)")
        db.log_ui_event("live_sell", {"ticker": ticker, "price": fill_price, "reason": reason,
                                       "pnl": round(closed.get("pnl", 0), 2),
                                       "pnl_pct": round(closed.get("pnl_pct", 0), 2)})
        return closed
    except Exception as e:
        breaker.record(False, error=str(e)[:200])
        logger.error(f"{ticker}: [LIVE] sell failed: {e}", exc_info=True)
        return {}
