"""Per-cycle Robinhood account sync (2026-07-17, Akhil's ask: "the account
linked is not the Robinhood Agentic account... how to mitigate that gap. Also
yes, I want the portfolio to be pulled and analyzed").

THE ACCOUNT GAP THIS SOLVES: Robinhood's official Trading MCP
(https://agent.robinhood.com/mcp/trading, launched May 2026) gives agents
READ access to every account under your login but only lets them TRADE in
the dedicated Agentic account. Meanwhile the third-party `robinhood-mcp`
this platform uses for reads wraps robin_stocks' build_holdings(), which has
NO account_number parameter - it only ever sees the PRIMARY individual
account. So neither integration, as-is, could pull the Agentic account's
positions into the local book.

robin_stocks itself, however, takes account_number on every call that
matters (get_open_stock_positions / load_account_profile / order_*), so this
module reads the CONFIGURED account (config.yaml `account.
robinhood_account_number` - set it to your Agentic account number) directly,
same credential path engine/live_trader.py already uses. Empty account
number = your primary account (robin_stocks' default).

WHAT A SYNC DOES (config `account.auto_sync`, default false, Control tab
toggle):
  1. Pulls the configured account's open stock positions + portfolio value.
  2. Imports positions that exist on Robinhood but are MISSING from the
     local real book, at the account's real average cost (same semantics as
     `robinhood_sync.py reconcile --apply`). From the moment they're
     imported, Loop B (engine/position_management.py) analyzes them every
     cycle - health score, unified exit score, stop state machine - which
     is what "pulled and analyzed" means here: the analysis engines already
     exist, they just need the positions in the book.
  3. NEVER auto-closes local positions the account no longer holds -
     closing needs your real sell fill price, and guessing one would poison
     P&L learning (same posture as robinhood_sync.py). Those raise a
     monitoring alert telling you the confirm_fill.py command to run.
  4. Logs a portfolio snapshot ui_event so the dashboard shows the real
     account value next to the paper book.

Runs at most once per SYNC_INTERVAL_SECONDS regardless of cycle cadence -
politeness against an unofficial API is account safety.
"""
import logging
import time
from datetime import datetime

logger = logging.getLogger("trading")

SYNC_INTERVAL_SECONDS = 15 * 60
_last_sync = {"at": 0.0}


def _account_number(cfg: dict) -> str | None:
    """Delegates to engine/live_trader.py's version (2026-07-26).

    This used to be a duplicate two-liner that read the config value straight,
    which meant it inherited the ``${RH_ACCOUNT_NUMBER}`` placeholder bug when
    called with server.py's raw-loaded config - see live_trader._account_number's
    docstring. Two copies of "which account are we talking to" is exactly the
    kind of thing that drifts, and the answer must be identical here and on the
    order path or sync reconciles against a different account than it trades."""
    from engine.live_trader import _account_number as _resolve
    return _resolve(cfg)


def enabled(cfg: dict) -> bool:
    return bool((cfg.get("account", {}) or {}).get("auto_sync", False))


def _fetch_remote_positions(rh, account_number: str | None) -> list | None:
    """[{ticker, shares, avg_cost}] for the configured account, or None on
    failure (None = unknown, [] = genuinely empty - callers must not treat
    a failed read as 'account is flat')."""
    try:
        raw = rh.account.get_open_stock_positions(account_number=account_number)
    except Exception as e:
        logger.warning(f"account_sync: positions read failed: {e}")
        return None
    if raw is None:
        return None
    out = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            shares = float(p.get("quantity") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        if shares <= 0:
            continue
        ticker = (p.get("symbol") or "").upper()
        if not ticker and p.get("instrument"):
            try:
                ticker = (rh.stocks.get_symbol_by_url(p["instrument"]) or "").upper()
            except Exception as e:
                logger.warning(f"account_sync: symbol lookup failed: {e}")
                continue
        if not ticker:
            continue
        try:
            avg = float(p.get("average_buy_price") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        out.append({"ticker": ticker, "shares": shares, "avg_cost": avg})
    return out


def apply_remote_positions(db, remote: list) -> dict:
    """Diffs remote holdings against the local REAL book and imports the
    missing ones. Pure DB logic (no network) - unit-testable. Returns
    {imported: [..], missing_remotely: [..]}."""
    local = {p["ticker"]: p for p in db.get_all_positions(simulated=False)}
    remote_by_ticker = {r["ticker"]: r for r in remote}

    imported = []
    for ticker, r in remote_by_ticker.items():
        if ticker in local:
            continue
        if r["avg_cost"] <= 0:
            logger.warning(f"account_sync: {ticker} has no average cost - not imported")
            continue
        db.open_position(ticker, r["avg_cost"], r["shares"],
                          round(r["avg_cost"] * r["shares"], 2),
                          simulated=False, trade_mode="SYNC")
        imported.append(ticker)
        logger.info(f"account_sync: imported {ticker} - {r['shares']:.4f} sh @ "
                    f"${r['avg_cost']:.2f} (held on Robinhood, missing locally)")

    missing_remotely = [t for t in local if t not in remote_by_ticker]
    for ticker in missing_remotely:
        # Report-only, never auto-close (see module docstring #3).
        db.log_alert(
            f"sync_missing_{ticker}_{datetime.utcnow().strftime('%Y%m%d')}",
            "ACCOUNT_SYNC", "MEDIUM",
            f"{ticker} is open in the local book but NOT held in the synced "
            f"Robinhood account. If you sold it, record the fill: "
            f"python3 confirm_fill.py {ticker} sell <price>")
    return {"imported": imported, "missing_remotely": missing_remotely}


def run(db, cfg: dict) -> dict | None:
    """Called once per cycle by scheduler.py. Returns a summary dict, or None
    when disabled/not due/unavailable."""
    if not enabled(cfg):
        return None
    now = time.time()
    if now - _last_sync["at"] < SYNC_INTERVAL_SECONDS:
        return None

    from engine import live_trader
    if not live_trader._login():
        logger.warning("account_sync: Robinhood login unavailable - sync skipped "
                       "(run robinhood_login_test.py once if this persists)")
        return None
    _last_sync["at"] = now

    rh = live_trader._rh()
    acct = _account_number(cfg)
    remote = _fetch_remote_positions(rh, acct)
    if remote is None:
        return None

    result = apply_remote_positions(db, remote)

    portfolio = {}
    try:
        profile = rh.profiles.load_account_profile(account_number=acct) or {}
        for k in ("equity", "portfolio_cash", "buying_power", "cash"):
            if profile.get(k) not in (None, ""):
                portfolio[k] = float(profile[k])
    except Exception as e:
        logger.warning(f"account_sync: portfolio read failed: {e}")

    summary = {
        "account": acct or "primary",
        "remote_positions": len(remote),
        "imported": result["imported"],
        "missing_remotely": result["missing_remotely"],
        **{f"portfolio_{k}": v for k, v in portfolio.items()},
    }
    db.log_ui_event("account_sync", summary)
    logger.info(f"account_sync: {len(remote)} position(s) in account "
                f"{acct or 'primary'}, imported {len(result['imported'])}, "
                f"{len(result['missing_remotely'])} local-only")
    return summary
