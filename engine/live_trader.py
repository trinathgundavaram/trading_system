"""LIVE Robinhood order execution.

THIS MODULE PLACES REAL ORDERS when all of its gates are open. It does not
know, and this docstring deliberately does not claim, what today's deployment
is configured to do - that is a resolved runtime value: see storage/banner.py's
execution_posture() and server.py's /api/status (§6, 2026-07-24). The previous
version of this paragraph said "HARD-DISABLED 2026-07-17 ... execution is
Claude-Desktop-only", which had been false since the master switch became a
config value, and was one of the five sentences the audit found describing a
system state that ended in July.

When every gate is closed, execution is Claude-Desktop-only: the scheduler
writes output/trade_prompt.md, Claude Desktop (with the Robinhood MCP) places
any orders, and confirm_fill.py records the fills.

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
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from mcp_clients.base import SourceCircuitBreaker

logger = logging.getLogger("trading")

# ── Unmanaged position modes (§5, Phase 1, 2026-07-24 audit) ────────────────
# Mirrors storage.database.MANAGED_EXCLUDED_MODES; see that constant for why
# the duplication is deliberate. tests/test_sync_quarantine.py guards drift.
UNMANAGED_TRADE_MODES = ("SYNC", "SEED")

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


# ── The validation receipt (§2, Phase 1) ────────────────────────────────────
# "I will not arm live trading until the backtest passes" was an intention.
# This makes it a code path, so it cannot be forgotten at 11pm on a Sunday.
# The receipt is written by run_backtest.py ONLY when a run clears the
# pre-committed go/no-go bar (§23, Phase 4) - until Phase 4 lands, no receipt
# exists, and that is the correct state: live execution stays blocked.
VALIDATION_MAX_AGE_DAYS = 30


def _validation_receipt_path() -> str:
    """Resolved lazily, not at import: TP_OUTPUT_DIR is per-version (§38) and
    tests need to point this somewhere else without reloading the module."""
    from storage.paths import validation_receipt_path
    return str(validation_receipt_path())


def _parse_receipt_time(raw: str) -> datetime:
    """Naive-UTC datetime from an ISO string, tolerating a trailing 'Z' and an
    explicit offset. Anything unparseable raises, and an unreadable receipt is
    treated as no receipt - fail closed, never open.

    Naive strings are ASSUMED to be UTC, matching how every other timestamp in
    this codebase is written (datetime.utcnow().isoformat())."""
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _validation_current(max_age_days: int = VALIDATION_MAX_AGE_DAYS) -> tuple[bool, str]:
    """Live execution requires a signed-off validation run no older than
    max_age_days.

    Returns (ok, human_readable_why). The why-string is surfaced verbatim by
    storage/banner.py, so it is written to be read by an operator at a glance,
    not parsed. NEVER raises: this is called from is_live_mode() on every
    cycle and from the startup banner, and a crash here would be a far worse
    failure than a blocked arm.
    """
    path = _validation_receipt_path()
    if not os.path.exists(path):
        return False, "no validation receipt - run the backtest gate first"
    try:
        with open(path) as f:
            r = json.load(f)
        age = datetime.utcnow() - _parse_receipt_time(r["generated_at"])
        if age > timedelta(days=max_age_days):
            return False, f"validation receipt is {age.days}d old (max {max_age_days})"
        if not r.get("passed"):
            return False, f"last validation FAILED: {r.get('reason', '')}"
        return True, f"validated {age.days}d ago: {r.get('summary', '')}"
    except Exception as e:
        return False, f"unreadable validation receipt: {e}"


_blocked_log_state = {"reason": "", "at": 0.0}
_BLOCKED_LOG_INTERVAL_S = 300


def _log_blocked_once(why: str):
    """is_live_mode() is called several times per ticker per cycle. Log the
    same block reason at most every 5 minutes, but log IMMEDIATELY whenever
    the reason changes - a changed reason is news, a repeated one is noise."""
    now = time.time()
    if (why != _blocked_log_state["reason"]
            or now - _blocked_log_state["at"] > _BLOCKED_LOG_INTERVAL_S):
        logger.error(f"LIVE BLOCKED - {why}")
        _blocked_log_state["reason"] = why
        _blocked_log_state["at"] = now


def is_live_mode(cfg: dict) -> bool:
    """True only when ALL gates are on: master switch (+ no TP_FORCE_PAPER
    veto) + a current validation receipt + EXECUTE mode + auto_trade. Anything
    less and this codebase places no orders.

    The validation receipt (§2, added 2026-07-24) is the gate that did not
    exist when the evaluation report found all three original gates open on a
    strategy with no validated edge. It is checked BEFORE watch_execute/
    auto_trade so the log line names the real blocker.
    """
    if not is_live_execution_enabled(cfg):
        return False
    ok, why = _validation_current()
    if not ok:
        _log_blocked_once(why)
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
        # §14: open_position returns None when this book already holds an open
        # position in this ticker - the unique index refusing a duplicate. The
        # pre-check at the top of this function makes that near-impossible
        # here, but "near-impossible after a real fill" is the case worth
        # naming: the order HAS filled, so the account holds shares regardless
        # of what the positions table says. Alert rather than continue
        # silently; the seeding below would otherwise write this fill's stop
        # onto the pre-existing row, which is the §16 failure in a new place.
        opened_id = db.open_position(ticker, fill_price, qty, round(fill_price * qty, 2),
                                      pattern_id=pattern_id, simulated=False,
                                      trade_mode=trade_mode)
        if opened_id is None:
            try:
                from engine.notifications import send_critical
                send_critical(
                    "LIVE FILL NOT RECORDED",
                    f"{ticker}: bought {qty} @ ${fill_price} but the live book "
                    f"already had an open {ticker}. The fill is real and the "
                    f"position row is not. Reconcile by hand before trading "
                    f"this name again.")
            except Exception as e:
                logger.error(f"could not send the unrecorded-fill alert: {e}")
            return {}

        # §51 (Phase 2.5): mirror of the same call in engine/paper_trader.py -
        # the pattern records which position it became, at the one moment both
        # ids are in scope. See db.get_pattern_excursions() for why the
        # transitive route through positions.pattern_id is not good enough.
        if pattern_id:
            db.link_pattern_to_trade(pattern_id, opened_id)

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
            # §16: scoped to the REAL book. Unscoped, this seeding wrote to
            # whichever open row matched the ticker first - so a live fill
            # could seed the paper mirror instead of the position it just
            # opened, which is the same bug pointing the other way.
            db.update_position_by_ticker(ticker, seed, simulated=False)
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
    to trade on its own, not whether an explicit human click can act.

    §5 (Phase 1, 2026-07-24): an AUTOMATED call (require_auto_trade=True) is
    additionally refused for SYNC/SEED positions - see the block below. A
    human clicking Sell for one named ticker still works, because losing the
    ability to manually exit a real position would itself be a risk."""
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

    # ── §5 layer 3: execution refuses the order ────────────────────────────
    # The last line of defence, after the query layer
    # (db.get_managed_positions) and the decision layer
    # (rules/sell_rules.py). It sits here, AFTER the position lookup and
    # BEFORE _login(), so that no automated path can place a real sell on a
    # holding this engine never chose to enter - not via the sell rules, not
    # via Loop B, not via the price-watch loop, not via rotation, and not via
    # any call site added in future that forgets the other two layers.
    mode = str(pos.get("trade_mode") or "").upper()
    if mode in UNMANAGED_TRADE_MODES and require_auto_trade:
        logger.error(
            f"{ticker}: [LIVE] REFUSED automated sell of an unmanaged {mode} position "
            f"({float(pos['shares']):.4f} sh). Close it in the Robinhood app or use the "
            f"explicit manual endpoint.")
        try:
            db.log_ui_event("unmanaged_sell_blocked", {"ticker": ticker, "mode": mode,
                                                        "reason": reason})
        except Exception as e:
            logger.warning(f"{ticker}: could not log unmanaged_sell_blocked: {e}")
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
                # §15: from close_position, the one place hold time is
                # computed. Re-deriving it here against a fresh clock reading
                # is how the same trade came to be recorded with two different
                # hold times in two different tables.
                pattern_db.close_trade(closed["pattern_id"], closed["pnl_pct"],
                                        closed.get("hold_hours", 0.0),
                                        exit_reason=f"live_{reason}")
            except Exception as e:
                logger.error(f"{ticker}: [LIVE] pattern close failed: {e}")
        logger.info(f"{ticker}: [LIVE] SOLD @ ${fill_price:.2f} ({reason}) - "
                    f"P/L ${closed.get('pnl', 0):+.2f} ({closed.get('pnl_pct', 0):+.2f}%)")
        db.log_ui_event("live_sell", {"ticker": ticker, "price": fill_price, "reason": reason,
                                       "pnl": round(closed.get("pnl", 0), 2),
                                       "pnl_pct": round(closed.get("pnl_pct", 0), 2)})

        # §9 (Phase 2): the breaker is checked after every real close. On this
        # path it matters most - a cascade here is spending actual money.
        try:
            from rules.risk_rules import trip_kill_switch_if_needed
            trip_kill_switch_if_needed(db, cfg, simulated=False)
        except Exception as e:
            logger.error(f"{ticker}: [LIVE] kill-switch check failed: {e}", exc_info=True)

        return closed
    except Exception as e:
        breaker.record(False, error=str(e)[:200])
        logger.error(f"{ticker}: [LIVE] sell failed: {e}", exc_info=True)
        return {}
