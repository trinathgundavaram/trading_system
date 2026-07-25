"""FastAPI + WebSocket server - the web replacement for the Rich terminal
dashboard. Serves platform state from Postgres and offers a set of writes.
Run alongside `python3 scheduler.py` (a separate process does the actual
scanning); this process does not scan on its own.

WHAT THIS MODULE CAN DO (not what today's deployment happens to be configured
to do - see storage/banner.py's execution_posture() and /api/status for the
resolved runtime answer; §6, 2026-07-24, replaced the four paragraphs that
previously asserted runtime state here and had been wrong since 16 July):

  - Reads: signals, positions, trades, analytics, logs, config.
  - Writes to config.yaml: watchlist, trading.mode/watch_execute/auto_trade/
    max_positions, the kill switch, and the live-execution master switch.
    All token-gated via require_token; arming live execution additionally
    requires a typed confirmation phrase.
  - Places a REAL Robinhood order in exactly one place: /api/real/sell, which
    calls engine/live_trader.py's execute_sell_live() after a token check, a
    re-typed ticker confirmation, and the live-execution master switch. Every
    other route is order-free.
  - Calls out to the network in three user-triggered (never automatic)
    places: /api/ticker/validate, /api/cycle/run_now, /api/ticker/evaluate_now.

Auth: every write route uses the require_token dependency (§4). The token is
resolved by storage/secrets.py, never from config.yaml.
"""
import asyncio
import hmac
import json
import logging
import re
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml
from fastapi import (BackgroundTasks, Depends, FastAPI, Header, HTTPException,
                     Request)
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocket, WebSocketDisconnect

from storage import banner, secrets
from storage.database import Database
from storage.log_setup import setup_logging, tail_log_lines

setup_logging("server")

app = FastAPI(title="Trading Platform v8.3")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
UI_PATH = BASE_DIR / "ui" / "index.html"
EVENT_POLL_SECONDS = 3

# Guards /api/cycle/run_now against a double-click (or an ad-hoc run
# overlapping the real scheduler.py process's own scheduled cycle) firing two
# run_cycle() calls at once from this process - non-blocking acquire, so a
# second request while one is already running gets a clean 409 instead of
# queueing up or racing.
_manual_cycle_lock = threading.Lock()

# Same non-blocking-double-click guard as _manual_cycle_lock above, for the
# Learning tab's "Run Backtest Now" button (POST /api/backtest/run) - a
# historical replay across a dozen-plus tickers/months is a much longer
# operation than a scan cycle, so this matters even more here. Does NOT
# guard against engine/backtest_loop.py's weekly auto-trigger running in the
# OTHER process (scheduler.py) at the same moment - same low-risk tradeoff
# _manual_cycle_lock already accepts, plus backtest_runs.status='running'
# gives cross-process visibility (checked below) that run_cycle doesn't have
# an equivalent for.
_manual_backtest_lock = threading.Lock()


def _load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False)


# ═══ Auth (§4, Phase 1, 2026-07-24) ═════════════════════════════════════════
# Four compounding weaknesses were found in the evaluation: a 5-character
# token; stored in cleartext in a versioned file; served over plain HTTP bound
# to 0.0.0.0 so every device on the LAN could reach it; and compared with a
# plain `!=`, with no rate limiting, in nine separate endpoints. That token
# gates the kill switch, config mutation, arming live execution, and manual
# real-money sells.
#
# The token now comes from storage/secrets.py (environment -> .env -> macOS
# Keychain), NEVER from config.yaml. This also closes a latent hole opened by
# Phase 0 step 0.2: config.yaml's ui.auth_token became the literal string
# "${UI_AUTH_TOKEN}" (server.py loads the YAML raw, without config_loader's
# ${VAR} expansion), so the old _auth_token() was comparing every request
# against that placeholder - anyone sending the header "${UI_AUTH_TOKEN}"
# would have authenticated.
_fail_counts: dict[str, list] = defaultdict(list)
_fail_lock = threading.Lock()
_MAX_FAILS, _WINDOW_S, _LOCKOUT_S = 5, 300, 900


def _auth_token() -> str:
    """The expected token. Raises if it is not configured anywhere - an empty
    expected token would compare equal to a blank header and silently
    unauthenticate every write endpoint in this process."""
    return secrets.get("UI_AUTH_TOKEN")


def _throttle(key: str):
    """Refuse further attempts from a client that has failed _MAX_FAILS times
    inside _WINDOW_S. Without this, a 5-character token is enumerable in
    seconds."""
    now = time.time()
    with _fail_lock:
        recent = [t for t in _fail_counts[key] if now - t < _WINDOW_S]
        _fail_counts[key] = recent
        if len(recent) >= _MAX_FAILS:
            raise HTTPException(
                429, f"Too many failed attempts - locked {_LOCKOUT_S // 60} min")


def _record_fail(key: str):
    with _fail_lock:
        _fail_counts[key].append(time.time())


def require_token(request: Request, x_auth_token: str = Header(None)) -> bool:
    """FastAPI dependency guarding every write endpoint.

    hmac.compare_digest, not `!=`: a plain string compare short-circuits on
    the first differing byte, which leaks the token one character at a time to
    anyone who can measure response latency.

    A dependency rather than an inline `if` in each handler, because a
    dependency cannot be forgotten on a NEW endpoint the way an inline check
    can - and "the tenth write route shipped without the check" is the exact
    mistake this shape exists to prevent.
    """
    client = request.client.host if request.client else "unknown"
    _throttle(client)
    try:
        expected = _auth_token()
    except Exception as e:
        # Misconfiguration must fail CLOSED, and must say so plainly rather
        # than looking like a wrong token.
        logging.getLogger("trading").error(f"UI auth token is not configured: {e}")
        raise HTTPException(503, "UI auth token is not configured on the server - "
                                 "set UI_AUTH_TOKEN (see .env.template)")
    if not x_auth_token or not hmac.compare_digest(str(x_auth_token), expected):
        _record_fail(client)
        raise HTTPException(403, "Invalid auth token")
    return True


connected_clients: list = []


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """The single-page dashboard, served with caching DISABLED (v1.2.0).

    This response previously carried no cache headers, so browsers cached the
    page indefinitely. That is how v1.2.0 shipped and appeared not to work: the
    server required a token on /api/cycle/run_now while the browser was still
    running the PREVIOUS index.html, which had no authFetch(), never sent the
    header, and therefore never prompted for a token. The symptom was a
    permanent 403 that retrying could not clear - the fix looked broken when it
    was simply not loaded.

    The whole UI is one file with inline JS, so there is no asset-hash
    cache-busting to fall back on. no-store is the correct trade here: the
    document is a few hundred KB served over loopback, and a stale dashboard
    that silently disagrees with the server about authentication - or about
    which positions are open - is far more expensive than re-sending it.
    """
    if not UI_PATH.exists():
        return HTMLResponse("<h1>ui/index.html not found</h1>", status_code=500)
    return HTMLResponse(UI_PATH.read_text(), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        await _send_state(websocket)
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


async def broadcast(payload: dict):
    """Pushes to every currently-connected /ws client."""
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in connected_clients:
            connected_clients.remove(ws)


async def _event_poll_loop():
    """scheduler.py runs in a SEPARATE process from this server (see run.sh
    --ui / main.py's run_ui()), so it can't call broadcast() above directly -
    there's no shared memory between the two processes. Instead, scheduler.py
    writes rows to the ui_events table (storage/database.py) after anything
    worth pushing live (a high-conviction buy, an urgent Loop B exit, or a
    cycle finishing), and this background task polls for new rows every
    EVENT_POLL_SECONDS and broadcasts them. Bounded latency (up to
    EVENT_POLL_SECONDS old) rather than true push, but no new dependency
    (no Redis/message queue) and reuses the SQLite file both processes
    already share."""
    db = Database()
    last_id = db.get_latest_ui_event_id()
    while True:
        try:
            events = await asyncio.to_thread(db.get_ui_events_since, last_id)
            for ev in events:
                last_id = ev["id"]
                await broadcast({"type": "event", "event_type": ev["event_type"],
                                  "payload": ev["payload"], "created_at": ev["created_at"]})
        except Exception:
            pass  # a transient DB hiccup shouldn't kill the poll loop
        await asyncio.sleep(EVENT_POLL_SECONDS)


@app.on_event("startup")
async def _start_event_poll_loop():
    asyncio.create_task(_event_poll_loop())


def _market_pulse_from_logs(db) -> dict:
    """F&G/VIX (2026-07-16 fix, Akhil's 'Market Pulse shows no data' report):
    this used to regex scheduler.py's 'F&G=..., VIX=...' line out of the DB
    `logs` table - which is EMPTY in production (scheduler logs to
    output/logs/*.log files, nothing ever writes the logs table), so the
    Market Pulse panel showed 'No cycle data yet' forever. The same numbers
    have been durably persisted to `latest_regime` (fear_greed_score /
    vix_level, refreshed every cycle) since the market-mood migration - read
    them from there, with the old log-regex kept only as a legacy fallback.

    ad_ratio/mcclellan: NOT log-derived - engine/market_breadth.py is called
    directly here. It's its own independently-cached (15 min TTL) calculation
    from the 11 sector ETFs, so this process doesn't need scheduler.py to have
    run a cycle first. See README's "Market breadth" section for what these
    numbers actually measure (a sector-ETF proxy, not true NYSE-wide breadth)."""
    from engine.market_breadth import calculate as calc_breadth
    try:
        breadth = calc_breadth()
    except Exception:
        breadth = {"ad_ratio": 0.5, "mcclellan": 0.0}

    regime = db.get_latest_regime() or {}
    fg, vix = regime.get("fear_greed_score"), regime.get("vix_level")
    if fg is not None or vix is not None:
        return {
            "fg_score": fg, "vix": vix,
            "ad_ratio": breadth["ad_ratio"], "mcclellan": breadth["mcclellan"],
            "breadth_gate_ok": True,
            "macro_blackout": regime.get("blackout_reason") if regime.get("blackout_active") else None,
        }

    for ts, level, msg in reversed(db.recent_logs(50)):
        m = re.search(r"F&G=(\d+), VIX=([\d.]+)", msg)
        if m:
            return {
                "fg_score": float(m.group(1)), "vix": float(m.group(2)),
                "ad_ratio": breadth["ad_ratio"], "mcclellan": breadth["mcclellan"],
                "breadth_gate_ok": True, "macro_blackout": None,
            }
    return {"fg_score": None, "vix": None,
            "ad_ratio": breadth["ad_ratio"], "mcclellan": breadth["mcclellan"],
            "breadth_gate_ok": True, "macro_blackout": None}


def _build_state_payload(db: Database, msg_type: str = "full_state") -> dict:
    """Shared by the /ws initial send and /api/state (used by the UI to
    refresh after a cycle_complete push event, without needing a fresh /ws
    reconnect). regime comes from the DB, not engine.regime_engine's
    current_state() singleton - see storage/database.py's latest_regime
    schema comment for why (this process never calls calculate() itself)."""
    cfg = _load_config()
    return {
        "type": msg_type,
        "config": cfg,
        "regime": db.get_latest_regime(),
        "market_context": _market_pulse_from_logs(db),
        "positions": db.get_all_positions(),
        "recent_signals": db.get_recent_signals(limit=20),
        "daily_stats": db.get_daily_stats(),
        # Paper realized P/L stays out of daily_stats (real-money-only, feeds
        # the risk engine) - separate display field for the dashboard tile.
        "paper_realized_today": db.paper_realized_pnl_today(),
        "health_score": db.get_latest_health_score(),
        "portfolio_heat": db.get_portfolio_heat(),
    }


async def _send_state(ws: WebSocket):
    db = Database()
    payload = _build_state_payload(db)
    await ws.send_text(json.dumps(payload, default=str))


@app.get("/api/state")
async def get_state():
    """REST equivalent of the /ws full_state push - the UI calls this after
    receiving a 'cycle_complete' live event so it can refresh without forcing
    a full WebSocket reconnect."""
    return _build_state_payload(Database())


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/signals")
async def get_signals(limit: int = 50):
    return Database().get_recent_signals(limit)


@app.get("/api/positions")
async def get_positions(simulated: Optional[bool] = None):
    """simulated=true -> paper book only, simulated=false -> real book only,
    omitted -> both (back-compat - the /ws full_state payload and the
    Dashboard/Positions tabs still pull the unfiltered list client-side and
    split on each row's own `simulated` flag for the Paper/Real toggle;
    this query param exists for any caller that wants the server to do the
    filtering instead, e.g. the Real Portfolio tab's /api/real/summary)."""
    return Database().get_all_positions(simulated=simulated)


def _paper_prices(db, open_sim: list, live: bool = False) -> dict:
    """Prices for open paper positions. Default: ticker_info_cache last_price
    (refreshed each scan cycle - zero network). live=True: fresh quotes via
    the market_data REST router (Alpaca -> ... -> FinanceQuery, provider
    TTL-cached, at most a handful of positions) with last_price fallback -
    used by the Portfolio tab so its numbers track the market between cycles,
    not just per-cycle."""
    prices = {}
    tickers = [p["ticker"] for p in open_sim]
    if not tickers:
        return prices
    info = db.get_ticker_info_bulk(tickers)
    prices = {t: (i or {}).get("last_price") for t, i in info.items()
              if (i or {}).get("last_price")}
    if live:
        try:
            from mcp_clients.market_data import router as md_router
            for t in tickers:
                q = md_router.get_quote(t)
                if q and q[0].get("price"):
                    prices[t] = q[0]["price"]
                    # Write back so EVERY consumer of last_price (equity
                    # snapshots, dashboards) benefits - held tickers that
                    # fall off the scan list otherwise never refresh
                    # (2026-07-16, 'prices not updating' fix).
                    try:
                        db.upsert_ticker_info(t, last_price=q[0]["price"])
                    except Exception:
                        pass
        except Exception:
            pass  # cached last_price already in place
    return prices


@app.get("/api/paper/summary")
async def get_paper_summary(live: bool = False):
    """WATCH-mode paper portfolio: purse (cash left), what's bought (cost +
    market value), unrealized + realized P/L, rule-derived stop/target exit
    prices per position, and total value vs starting cash - see
    engine/paper_trader.py. live=1 pulls fresh quotes for open positions."""
    from engine.paper_trader import snapshot
    db = Database()
    open_sim = db.get_all_positions(simulated=True)
    prices = _paper_prices(db, open_sim, live=live)
    return snapshot(db, prices=prices, cfg=_load_config())


@app.post("/api/paper/sell")
async def paper_sell(body: dict, _: bool = Depends(require_token)):
    """Manual close of a paper position at the current market price,
    REGARDLESS of the sell rules (Akhil's ask: an escape hatch when you want
    out now). Token-protected like every other write endpoint. Still only
    ever touches the SIMULATED book - no real order is placed, same
    guarantee as everything else in this process."""
    ticker = (body.get("ticker") or "").upper().strip()
    if not ticker:
        raise HTTPException(400, "ticker required")
    db = Database()
    pos = db.get_open_position(ticker, simulated=True)
    if not pos:
        raise HTTPException(404, f"No open paper position for {ticker}")
    prices = _paper_prices(db, [pos], live=True)
    price = prices.get(ticker)
    if not price:
        raise HTTPException(502, f"No current price available for {ticker} - try again")
    from engine.paper_trader import execute_sell
    from learning.pattern_database import PatternDatabase
    # §D: exit_kind="manual". classify_exit() recognises "manual_fill_confirmed"
    # (confirm_fill.py's string) but never recognised "manual_ui", so every
    # Sell-button close was landing as NULL despite being the one exit whose
    # kind is least ambiguous - a human pressed a button.
    closed = execute_sell(db, ticker, float(price), reason="manual_ui",
                           pattern_db=PatternDatabase(db), cfg=_load_config(),
                           exit_kind="manual")
    if not closed:
        raise HTTPException(500, "Sell failed - see server log")
    return closed


@app.post("/api/real/sell")
async def real_sell(body: dict, _: bool = Depends(require_token)):
    """Manual REAL sell (2026-07-24, Trinath's explicit choice: the Real
    Portfolio tab's Sell button places an ACTUAL Robinhood market order, not
    just a recorded fill). Goes through engine/live_trader.py's exact
    order-placement path - same Agentic account (config.yaml account.
    robinhood_account_number), same circuit breaker, same fill-wait-then-
    cancel-if-unfilled safety every automated live sell already uses.

    Gates, in order: auth token (every write endpoint) -> the ticker must be
    RE-TYPED exactly in `confirm` (real-money click deserves more friction
    than a browser confirm() dialog) -> trading.live_execution_enabled (the
    master switch - Control tab) must be ON, or this 409s with a clear
    message instead of silently no-op'ing -> an open REAL position must
    exist for the ticker -> the order circuit breaker must be closed.
    Deliberately does NOT require watch_execute=='EXECUTE'/auto_trade to be
    armed (execute_sell_live's require_auto_trade=False) - those gate the
    SCHEDULER's automated decisions, not this explicit one-off human click.

    §5 (Phase 1): still works for a SYNC row, deliberately.
    execute_sell_live's unmanaged-position refusal is gated on
    require_auto_trade, which this path passes as False - being unable to
    manually exit a real position would itself be a risk."""
    ticker = (body.get("ticker") or "").upper().strip()
    if not ticker:
        raise HTTPException(400, "ticker required")
    confirm = (body.get("confirm") or "").strip().upper()
    if confirm != ticker:
        raise HTTPException(400, f"Confirmation text must exactly match the ticker: {ticker}")
    cfg = _load_config()
    from engine import live_trader
    if not live_trader.is_live_execution_enabled(cfg):
        raise HTTPException(
            409, "Live Execution master switch is OFF (Control tab) - enable "
                 "it before a real order can be placed from here.")
    db = Database()
    pos = db.get_open_position(ticker, simulated=False)
    if not pos:
        raise HTTPException(404, f"No open real position for {ticker}")
    if not live_trader.breaker.available():
        raise HTTPException(503, "Robinhood order circuit breaker is open "
                                  "(recent order failures) - try again later.")
    from learning.pattern_database import PatternDatabase
    closed = live_trader.execute_sell_live(
        db, cfg, ticker, reason="manual_ui", pattern_db=PatternDatabase(db),
        require_auto_trade=False, exit_kind="manual")   # §D - see the paper path
    if not closed:
        raise HTTPException(500, "Sell failed, was rejected, or didn't fill within the "
                                  "wait window - see server log for the exact error")
    return closed


@app.post("/api/portfolio/clear_seed")
async def clear_seed_positions(_: bool = Depends(require_token)):
    """Powers the Portfolio tab's "Clear synced positions" button - removes
    trade_mode='SEED' rows (robinhood_sync.py's seed-paper command, which
    clones the real Robinhood account into the paper book for display) and
    credits their cost basis back to paper_account.cash. (2026-07-23,
    Trinath's ask: these were counting toward trading.max_positions and
    crowding out genuine WATCH signals - engine/paper_trader.py now excludes
    SEED from that count regardless of whether this has been run; this is
    for actually clearing them out of the DB/UI.) Doesn't touch Robinhood
    (read-only either way) or any real confirm_fill.py position."""
    return Database().remove_seed_positions()


@app.post("/api/positions/clear_synced")
async def clear_synced_positions(_: bool = Depends(require_token)):
    """Real-book counterpart to /api/portfolio/clear_seed - powers the
    Positions tab's "Clear synced positions" button. Removes trade_mode=
    'SYNC' rows (engine/account_sync.py's auto-import of real Robinhood
    holdings, config.yaml account.auto_sync). Doesn't touch Robinhood
    (read-only either way) or place any order - only deletes the local
    tracking row. Doesn't change account.auto_sync itself."""
    return Database().remove_synced_positions()


@app.get("/api/paper/trades")
async def get_paper_trades(limit: int = 100):
    """Simulated buy/sell ledger (WATCH-mode paper trading)."""
    return Database().get_paper_trades(limit)


@app.post("/api/live_execution")
async def set_live_execution(body: dict, _: bool = Depends(require_token)):
    """Flips the LIVE EXECUTION master switch (trading.live_execution_enabled)
    - the gate that decides whether engine/live_trader.py may EVER place a
    real order. Deliberately the hardest write in the app (2026-07-17,
    Akhil's design: 'display what needs to be typed so it doesn't turn on by
    accident'): enabling requires the auth token AND the exact confirmation
    phrase typed by a human. Disabling requires only the token - turning
    live trading OFF should never have friction.

    Note that flipping this ON is necessary but NOT sufficient as of §2:
    engine/live_trader.py's is_live_mode() additionally requires a current
    validation receipt, so this switch cannot arm live trading on its own."""
    enable = bool(body.get("enable"))
    from engine.live_trader import LIVE_EXECUTION_CONFIRM_PHRASE
    if enable:
        typed = (body.get("confirm") or "").strip()
        if typed != LIVE_EXECUTION_CONFIRM_PHRASE:
            raise HTTPException(
                400, f"Confirmation phrase mismatch - type exactly: "
                     f"{LIVE_EXECUTION_CONFIRM_PHRASE}")
    cfg = _load_config()
    cfg.setdefault("trading", {})["live_execution_enabled"] = enable
    _save_config(cfg)
    logging.getLogger("trading").warning(
        f"LIVE EXECUTION master switch set to {'ON' if enable else 'OFF'} via UI")
    return {"status": "ok", "live_execution_enabled": enable}


def _robinhood_account_probe(cfg: dict) -> dict:
    """Read-only account snapshot (buying power + total equity) for whichever
    account config.yaml's account.robinhood_account_number is set to - your
    Robinhood AGENTIC account, per engine/live_trader.py's docstring - NOT
    robin_stocks' default primary account.

    2026-07-24 (Trinath: "it is taking the primary account ... should
    consider the agentic account that was setup"): the /api/robinhood/status
    and /api/real/summary endpoints used to read via mcp_clients/
    robinhood_mcp.py's get_client().get_portfolio(), which wraps robin_stocks'
    build_holdings() - that call has NO account_number parameter, so it
    always reads your PRIMARY individual account regardless of what's
    configured on the Control tab (same account-scoping gap
    engine/account_sync.py's docstring already called out for position sync).
    This instead goes through engine/live_trader.py's own login/account
    plumbing (rh.profiles.load_account_profile(account_number=...)) - the
    EXACT path that already places real orders and syncs positions against
    the configured account - so what's displayed is guaranteed to match the
    account orders would actually execute against, not a different one."""
    acct = None
    out = {"read_ok": None, "buying_power": None, "portfolio_value": None,
           "account_number": None, "account_source": "agentic_configured"}
    try:
        from engine import live_trader
        acct = live_trader._account_number(cfg)
        out["account_number"] = acct
        if acct is None:
            out["account_source"] = "primary_no_account_configured"
        if not live_trader._login():
            out["read_ok"] = False
            return out
        rh = live_trader._rh()
        profile = rh.profiles.load_account_profile(account_number=acct) or {}
        out["read_ok"] = bool(profile)
        for k in ("buying_power", "cash_available_for_withdrawal", "cash"):
            if profile.get(k) not in (None, ""):
                out["buying_power"] = float(profile[k]); break
        for k in ("equity", "portfolio_cash", "total_equity", "market_value"):
            if profile.get(k) not in (None, ""):
                out["portfolio_value"] = float(profile[k]); break
    except Exception:
        out["read_ok"] = False
    return out


@app.get("/api/robinhood/status")
async def robinhood_status():
    """Robinhood integration health for the Monitor tab (2026-07-16):
    credentials, account reachability (probe against the CONFIGURED/Agentic
    account - see _robinhood_account_probe), the order path's circuit
    breaker, and whether live trading is currently ARMED (mode=EXECUTE +
    auto_trade). robin_stocks caches its login session on disk, so this
    doesn't re-login on every call."""
    import os as _os
    cfg = _load_config()
    configured = bool(_os.getenv("ROBINHOOD_USERNAME") and _os.getenv("ROBINHOOD_PASSWORD"))
    out = {
        "configured": configured,
        "watch_execute": cfg.get("trading", {}).get("watch_execute", "WATCH"),
        "auto_trade": bool(cfg.get("trading", {}).get("auto_trade", False)),
        "read_ok": None, "buying_power": None, "portfolio_value": None,
        "orders_breaker_open": None,
    }
    from engine import live_trader
    out["live_execution_enabled"] = live_trader.is_live_execution_enabled(cfg)
    out["live_trading_armed"] = live_trader.is_live_mode(cfg)
    out["execution_path"] = ("direct_robinhood" if out["live_trading_armed"]
                              else "claude_desktop")
    out["orders_breaker_open"] = not live_trader.breaker.available()
    if configured:
        probe = _robinhood_account_probe(cfg)
        out["read_ok"] = probe["read_ok"]
        out["buying_power"] = probe["buying_power"]
        out["portfolio_value"] = probe["portfolio_value"]
        out["account_number"] = probe["account_number"]
        out["account_source"] = probe["account_source"]
    return out


@app.get("/api/real/summary")
async def get_real_summary(live: bool = False):
    """Real (live-money) portfolio - the REAL counterpart to /api/paper/summary,
    powering the Real Portfolio tab (2026-07-24, Trinath's ask for a Paper/Real
    toggle with fully separate stats). Combines: a read-only account probe
    scoped to config.yaml's account.robinhood_account_number - your Agentic
    account, NOT robin_stocks' default primary account (see
    _robinhood_account_probe's docstring - this used to silently read the
    primary account, fixed 2026-07-24) - open real positions (simulated=False,
    confirm_fill.py-managed) priced the same way the paper book is (live=1
    pulls fresh quotes), and realized P&L from daily_stats (today's row +
    all-time sum) - daily_stats is real-money-only by design, see
    storage/database.py's close_position()."""
    cfg = _load_config()
    db = Database()
    open_real = db.get_all_positions(simulated=False)
    prices = _paper_prices(db, open_real, live=live)

    # Same rule-derived (stop, target) the Paper Portfolio tab already shows -
    # engine/paper_trader.py's _exit_prices() is generic over any position
    # dict (entry_price/current_stop_price/current_target_price/
    # risk_per_share), it has no dependency on the position being simulated,
    # so it applies unchanged to real positions too (2026-07-24, Trinath:
    # "real trading doesn't show expected stop price pricing").
    from engine.paper_trader import _exit_prices

    positions_out = []
    invested_cost = 0.0
    market_value = 0.0
    priced_cost = 0.0
    for p in open_real:
        cost = p.get("dollar_amount") or ((p.get("entry_price") or 0) * (p.get("shares") or 0))
        invested_cost += cost
        cur = prices.get(p["ticker"])
        value = (p["shares"] * cur) if (cur and p.get("shares")) else None
        unreal = (value - cost) if value is not None else None
        if value is not None:
            market_value += value
            priced_cost += cost
        stop_price, target_price = _exit_prices(p, cfg)
        positions_out.append({
            "ticker": p["ticker"], "entry_price": p.get("entry_price"),
            "shares": p.get("shares"), "cost": round(cost, 2) if cost else cost,
            "current_price": cur,
            "stop_price": stop_price, "target_price": target_price,
            "market_value": round(value, 2) if value is not None else None,
            "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
            "entry_time": p.get("entry_time"), "trade_mode": p.get("trade_mode"),
        })

    import os as _os
    configured = bool(_os.getenv("ROBINHOOD_USERNAME") and _os.getenv("ROBINHOOD_PASSWORD"))
    probe = _robinhood_account_probe(cfg) if configured else {
        "read_ok": False, "buying_power": None, "portfolio_value": None,
        "account_number": None, "account_source": "not_configured",
    }
    connected = configured and bool(probe.get("read_ok"))

    daily = db.get_daily_stats()
    return {
        "connected": connected,
        "buying_power": probe.get("buying_power"),
        "account_value": probe.get("portfolio_value"),
        "account_number": probe.get("account_number"),
        "account_source": probe.get("account_source"),
        "n_open": len(open_real),
        "open_positions": positions_out,
        "invested_cost": round(invested_cost, 2),
        "market_value": round(market_value, 2),
        "unrealized_pnl": round(market_value - priced_cost, 2),
        "realized_pnl_today": round(daily.get("realized_pnl") or 0.0, 2),
        "realized_pnl_all_time": db.get_realized_pnl_all_time(),
        "trades_placed_today": daily.get("trades_placed", 0),
        "winning_trades_today": daily.get("winning_trades", 0),
    }


@app.get("/api/paper/equity_history")
async def get_paper_equity_history(limit: int = 500):
    """Portfolio value over time - one point per WATCH cycle, oldest first
    (chart-ready). Powers the Portfolio tab's equity curve."""
    return Database().get_paper_equity_history(limit)


@app.get("/api/config")
async def get_config():
    return _load_config()


@app.post("/api/config")
async def update_config(update: dict, _: bool = Depends(require_token)):
    cfg = _load_config()
    # Only allow safe, non-financial-risk updates from the UI.
    safe_top_keys = ["watchlist"]
    for k in safe_top_keys:
        if k in update:
            cfg[k] = update[k]
    if "trading" in update and isinstance(update["trading"], dict):
        cfg.setdefault("trading", {})
        for k in ("mode", "watch_execute"):
            if k in update["trading"]:
                cfg["trading"][k] = update["trading"][k]
        # auto_trade (2026-07-16, live-trading integration): the arming
        # switch for REAL order execution via engine/live_trader.py. Only
        # takes effect when watch_execute is also EXECUTE, and every
        # per-trade guard (kill switch, max positions/size/trades-per-day,
        # buying power) still applies - but it IS financial-risk config, so
        # it's coerced to a strict bool and only accepted with a valid
        # token (this whole endpoint is token-gated).
        if "auto_trade" in update["trading"]:
            cfg["trading"]["auto_trade"] = bool(update["trading"]["auto_trade"])
        # max_positions (2026-07-17, rotation follow-up - Akhil's ask: "I
        # don't need to touch the codebase directly"): portfolio cap, now
        # editable from the Control tab. Clamped 1-50; it IS financial-risk
        # config, but only in the conservative direction a clamp can't fix
        # (a too-big cap still can't buy anything the per-trade guards
        # wouldn't allow), and the endpoint is token-gated.
        if "max_positions" in update["trading"]:
            try:
                n = int(update["trading"]["max_positions"])
                cfg["trading"]["max_positions"] = max(1, min(50, n))
            except (TypeError, ValueError):
                pass
    if "account" in update and isinstance(update["account"], dict):
        # Robinhood account link (2026-07-17, agentic-account gap): which
        # account number robin_stocks reads/trades (empty = primary), and
        # whether engine/account_sync.py imports its holdings each cycle.
        cfg.setdefault("account", {})
        if "robinhood_account_number" in update["account"]:
            v = str(update["account"]["robinhood_account_number"] or "").strip()
            if len(v) <= 30 and (v == "" or v.replace("-", "").isalnum()):
                cfg["account"]["robinhood_account_number"] = v
        if "auto_sync" in update["account"]:
            cfg["account"]["auto_sync"] = bool(update["account"]["auto_sync"])
    if "rotation" in update and isinstance(update["rotation"], dict):
        # Portfolio Rotation Engine settings (engine/rotation.py) - each key
        # individually validated/clamped; unknown keys ignored.
        cfg.setdefault("rotation", {})
        rot = update["rotation"]
        if "enabled" in rot:
            cfg["rotation"]["enabled"] = bool(rot["enabled"])
        _clamps = {"min_candidate_score": (50, 100), "max_victim_health_score": (0, 75),
                   "min_hold_days": (0, 30), "max_rotations_per_week": (0, 20)}
        for k, (lo, hi) in _clamps.items():
            if k in rot:
                try:
                    v = max(lo, min(hi, float(rot[k])))
                    cfg["rotation"][k] = int(v) if k == "max_rotations_per_week" else v
                except (TypeError, ValueError):
                    pass
    if "risk_level" in update and update["risk_level"] in cfg.get("risk", {}):
        cfg["risk_level"] = update["risk_level"]
    if "screener" in update and isinstance(update["screener"], dict):
        # Auto-discovery screener toggle (engine/screener.py) - safe to expose
        # from the UI same as trading.mode/watch_execute: it only ever adds
        # candidates to be SCORED through the normal buy pipeline, it can't
        # place an order or change auto_trade (still hard-disabled in code
        # regardless). enabled: bool, max_candidates: int (per-source on/off
        # stays config.yaml-only for now - not exposed here).
        cfg.setdefault("screener", {})
        if "enabled" in update["screener"]:
            cfg["screener"]["enabled"] = bool(update["screener"]["enabled"])
        if "max_candidates" in update["screener"]:
            try:
                n = int(update["screener"]["max_candidates"])
                cfg["screener"]["max_candidates"] = max(1, min(50, n))
            except (TypeError, ValueError):
                pass
    _save_config(cfg)
    return {"status": "ok"}


@app.get("/api/ticker/names")
async def get_ticker_names():
    """{ticker: company_name} for the hover-tooltip lookup in the UI - built
    from storage/database.py's ticker_info_cache, populated opportunistically
    every scan cycle (scheduler.py) and explicitly on validation (below).
    No live MCP call here - this just reads what's already cached."""
    return Database().get_all_ticker_names()


@app.get("/api/ticker/health")
async def get_ticker_health(min_consecutive: int = 1):
    """Tickers currently on a stale/fallback-data streak (Data Provenance
    Circuit Breaker, rules/hard_vetoes.py's veto #16 + scheduler.py's
    consecutive-cycle tracking) - powers the Control tab's watchlist-chip
    warning badge, so "why does this ticker keep landing on HOLD" is visible
    at a glance instead of requiring a dig through the Signals tab."""
    return Database().get_unhealthy_tickers(min_consecutive=min_consecutive)


@app.get("/api/alerts")
async def get_alerts():
    """Open (unresolved) monitoring_alerts rows - e.g. the stale-data streak
    alert scheduler.py logs once per bad-data streak. Powers the Monitor
    tab's Data Quality Alerts panel."""
    return Database().get_open_alerts()


@app.post("/api/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, body: dict = None,
                         _: bool = Depends(require_token)):
    """Manually acknowledges/dismisses an alert (e.g. after checking the
    ticker symbol is correct, or after removing it from the watchlist) -
    never automatic, matching this codebase's posture that a human decides
    what to do about a flagged ticker, not the scheduler."""
    resolution = (body or {}).get("resolution", "dismissed from UI")
    Database().resolve_alert(alert_id, resolution)
    return {"status": "ok"}


@app.get("/api/news")
async def get_news(hours: int = 72, limit: int = 100, notable_only: bool = False):
    """News tab (2026-07-14): per-ticker headlines + market-wide 'mood'
    context, both already fetched/computed every cycle for scoring
    (SENTIMENT_MACRO bucket's news_multiplier and the regime/threshold
    calc) but never surfaced in the UI before now. Zero extra MCP calls -
    this just reads what scheduler.py already persisted via
    db.record_news_items() and the market_mood columns on latest_regime.

    notable_only=True filters out neutral-sentiment headlines, useful for
    a "what actually matters" view vs. the full feed."""
    db = Database()
    regime = db.get_latest_regime() or {}
    return {
        "news": db.get_recent_news(hours=hours, limit=limit, notable_only=notable_only),
        "market_mood": {
            "fear_greed_score": regime.get("fear_greed_score"),
            "fear_greed_rating": regime.get("fear_greed_rating"),
            "vix_level": regime.get("vix_level"),
            "hours_to_next_macro": regime.get("hours_to_next_macro"),
            "blackout_active": regime.get("blackout_active"),
            "blackout_reason": regime.get("blackout_reason"),
            "regime": regime.get("dominant_regime"),
            "as_of": regime.get("updated_at"),
        },
    }


@app.get("/api/logs")
async def get_logs(lines: int = 300, level: str = None, source: str = None):
    """Tails output/logs/scheduler.log and output/logs/server.log (see
    storage/log_setup.py) and merges them into one timestamp-sorted feed -
    powers the UI's Logs tab. Read-only, on-demand (not pushed over the
    websocket) - reading a bounded local file is cheap, so this doesn't need
    the same "don't hammer it every cycle" caution as MCP calls.

    `lines` caps how many lines are read PER source file before merging (not
    the final merged count) - e.g. lines=300 reads up to 300 from each of
    scheduler.log/server.log, then merges+sorts, so you may get up to 600
    entries back. `level`/`source` are optional client-side-style filters
    applied after reading, so the UI can narrow down without re-fetching."""
    sources = ["scheduler", "server"] if not source else [source]
    merged = []
    for proc in sources:
        for raw in tail_log_lines(proc, max_lines=lines):
            if not raw.strip():
                continue
            merged.append({"raw": raw, "source": proc})
    merged.sort(key=lambda e: e["raw"])  # asctime's default format is lexically sortable

    if level:
        level = level.upper()
        merged = [e for e in merged if f" {level} " in e["raw"]]

    return {"lines": merged, "log_dir_note": "output/logs/{scheduler,server}.log, rotated at 5MB x3"}


@app.post("/api/ticker/validate")
async def validate_ticker(body: dict, _: bool = Depends(require_token)):
    """The ONE endpoint in this file that calls an MCP directly (see module
    docstring) - a ticker you're about to add to the watchlist hasn't
    necessarily been scanned yet, so there's nothing in ticker_info_cache to
    read for it. Triggered only when you click Add in the UI - not part of
    any scan loop.

    'valid' = yfinance found real data for this symbol. This is NOT a single
    field check: yfinance's Ticker.info dict is known to omit
    regularMarketPrice/currentPrice for some perfectly valid, actively-traded
    tickers (e.g. HCA) depending on which Yahoo endpoint served the response -
    see https://github.com/ranaroussi/yfinance/issues/1519. Relying on those
    two fields alone produced false "not a valid ticker" rejections for real
    stocks. So this checks a wider set of price fields first, and if none of
    those are present, falls back to a price-history lookup (same call
    engine/market_breadth.py already uses) and takes the most recent close.
    Only if BOTH the info lookup and the price-history lookup come back empty
    is the ticker reported invalid - a typo or delisted symbol won't have
    price history either, so this stays a real rejection for genuine typos."""
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(400, "No ticker provided")

    from mcp_clients.yfinance_mcp import YFinanceMCP
    yf = YFinanceMCP()
    info = yf.get_ticker_info(ticker)
    price = (
        info.get("regularMarketPrice") or info.get("currentPrice")
        or info.get("previousClose") or info.get("open")
        or info.get("dayHigh") or info.get("dayLow")
        or info.get("bid") or info.get("ask") or info.get("navPrice")
    )
    name = info.get("longName") or info.get("shortName") or ""

    if not price:
        hist = yf.get_price_history(ticker, period="5d", interval="1d")
        rows = hist.get("data") or hist.get("history") or hist.get("prices") or [] \
            if isinstance(hist, dict) else (hist if isinstance(hist, list) else [])
        for row in reversed(rows):
            if not isinstance(row, dict):
                continue
            for key in ("close", "Close", "c"):
                if row.get(key) is not None:
                    try:
                        price = float(row[key])
                    except (TypeError, ValueError):
                        price = None
                    break
            if price:
                break

    valid = bool(price) or bool(name)

    db = Database()
    db.upsert_ticker_info(ticker, company_name=name or None, last_price=price, valid=valid)

    return {"ticker": ticker, "valid": valid, "name": name or None, "price": price}


@app.post("/api/ticker/evaluate_now")
async def evaluate_ticker_now(body: dict, _: bool = Depends(require_token)):
    """The SECOND (of two) exceptions to this file's normal MCP-free rule -
    imports and calls scheduler.py's evaluate_single_ticker(), triggered only
    right after a ticker is successfully added to the watchlist in the UI.

    Without this, a freshly-added ticker shows nothing under the Signals tab
    until the next full scheduled cycle runs - up to scan_interval_minutes
    away, or not until Monday if the market's closed. This runs one ticker
    through the exact same regime/breadth/scoring pipeline run_cycle() uses
    (see scheduler.py's _evaluate_ticker/_calc_regime_and_market_dict, shared
    by both) and writes a real signals-table row, so it shows up immediately.

    Synchronous, not backgrounded like /api/cycle/run_now - one ticker is
    cheap enough (roughly the same cost as the market-context fetch alone)
    that waiting for the real answer is simpler and more honest than a toast
    that says "started" for something this quick. Runs via asyncio.to_thread
    so the blocking MCP calls don't stall this process's event loop (and
    therefore the WebSocket/other requests) for the whole duration.

    Doesn't check kill_switch/risk limits (see evaluate_single_ticker's
    docstring) - it only ever writes a read-only signals row, never opens a
    position or places an order, so those guards don't apply."""
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(400, "No ticker provided")

    from scheduler import evaluate_single_ticker
    try:
        result = await asyncio.to_thread(evaluate_single_ticker, ticker)
    except Exception as e:
        raise HTTPException(500, f"Evaluation failed: {e}")

    if result is None:
        raise HTTPException(502, f"Couldn't evaluate {ticker} - no price data returned")

    return result


@app.post("/api/kill_switch")
async def toggle_kill_switch(_: bool = Depends(require_token)):
    cfg = _load_config()
    cfg.setdefault("risk", {})
    cfg["risk"]["kill_switch_triggered"] = not cfg["risk"].get("kill_switch_triggered", False)
    _save_config(cfg)
    return {"kill_switch": cfg["risk"]["kill_switch_triggered"]}


@app.get("/api/prompt")
async def get_prompt():
    p = BASE_DIR / "output" / "trade_prompt.md"
    return {"content": p.read_text() if p.exists() else None}


@app.post("/api/prompt/copy")
async def copy_prompt(_: bool = Depends(require_token)):
    p = BASE_DIR / "output" / "trade_prompt.md"
    if not p.exists():
        raise HTTPException(404, "No prompt ready")
    try:
        subprocess.run(["pbcopy"], input=p.read_bytes(), check=True, timeout=5)
        return {"status": "copied"}
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"status": "error", "message": "pbcopy not available (not on macOS?)"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "pbcopy didn't respond in time"}


@app.get("/api/trades")
async def get_trades(limit: int = 100):
    return Database().get_recent_trades(limit)


@app.get("/api/learning/runs")
async def get_learning_runs(limit: int = 20):
    """History of scheduler.py's automated engine.learning_loop.maybe_run()
    calls - walk-forward rule attribution + any champion/challenger
    evaluations, run on a trigger (every N closed trades / M days from
    config.yaml's learning.walk_forward_trigger_* settings) instead of
    needing a manual Python-shell invocation. Nothing here is auto-applied -
    see engine/learning_loop.py's docstring."""
    return Database().get_recent_learning_runs(limit)


def _run_manual_backtest():
    """Spawns run_backtest.py as a separate OS process rather than calling
    engine/backtest_engine.py's run_and_persist() in-thread. It used to run
    in-thread via FastAPI BackgroundTasks, which kept the async event loop
    itself unblocked but NOT free of GIL contention - a multi-minute,
    CPU-heavy replay running on a background thread still competes for the
    GIL with every other concurrent request this server handles (this is
    why the Signals tab and others were slow to load while a backtest was
    running). See engine/backtest_loop.py's spawn_backtest_subprocess()
    docstring for the full rationale. This function just builds the args
    and hands off - it returns almost immediately either way."""
    from datetime import date, timedelta

    from config_loader import load_config_dict
    from engine.backtest_loop import resolve_backtest_tickers, spawn_backtest_subprocess

    try:
        cfg = load_config_dict()
        bcfg = cfg.get("backtest", {}) or {}
        # resolve_backtest_tickers (2026-07-24): honors backtest.ticker_source -
        # "static" (default, backtest.tickers verbatim) or "screener_discovered"
        # (auto-pulls the live-discovered universe from screener_candidates,
        # see that function's docstring for why it's ordered by discovery
        # frequency, never by past score).
        db_for_tickers = None
        try:
            from storage.database import Database
            db_for_tickers = Database()
        except Exception as e:
            logging.getLogger(__name__).warning(f"Couldn't reach DB for ticker resolution, using static list: {e}")
        tickers = resolve_backtest_tickers(cfg, db_for_tickers)
        months = bcfg.get("months", 12)
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=int(months * 30.44))).isoformat()
        spawn_backtest_subprocess(
            tickers, start, end,
            warmup_days=bcfg.get("warmup_days", 260), max_hold_days=bcfg.get("max_hold_days", 20),
            triggered_by="manual",
        )
    except Exception:
        logging.getLogger(__name__).exception("Manual backtest run failed to start")
    finally:
        _manual_backtest_lock.release()


@app.post("/api/backtest/run")
async def run_backtest_now(background_tasks: BackgroundTasks,
                            _: bool = Depends(require_token)):
    """Powers the Learning tab's "Run Backtest Now" button - triggers
    engine/backtest_engine.py's Stage 1 historical replay on demand using
    config.yaml's backtest.tickers/months/warmup_days/max_hold_days, same
    scope/tickers the weekly automatic trigger (engine/backtest_loop.py,
    scheduler.py) uses. Runs as a background task so the HTTP request
    returns immediately - the UI polls /api/backtest/status to show
    progress and /api/backtest/latest once it's done."""
    db = Database()
    running = db.get_running_backtest_run()
    if running is not None:
        raise HTTPException(409, f"A backtest is already running (started {running['started_at']})")
    if not _manual_backtest_lock.acquire(blocking=False):
        raise HTTPException(409, "A manual backtest is already running")
    background_tasks.add_task(_run_manual_backtest)
    return {"status": "started"}


@app.get("/api/backtest/status")
async def get_backtest_status():
    """Poll target for the "Run Backtest Now" button - cross-process visible
    (reads backtest_runs.status, not just this process's in-memory lock) so
    the UI shows 'running' correctly whether the in-progress run was started
    by this button or by scheduler.py's weekly auto-trigger in the other
    process."""
    db = Database()
    running = db.get_running_backtest_run()
    return {"running": running is not None, "run": running}


@app.get("/api/backtest/latest")
async def get_latest_backtest():
    """Most recent backtest_runs row (any status) for the Learning tab's
    summary panel."""
    db = Database()
    return db.get_last_backtest_run() or {}


@app.get("/api/backtest/runs")
async def get_backtest_runs(limit: int = 10):
    """Recent backtest_runs history for the Learning tab."""
    db = Database()
    return db.get_recent_backtest_runs(limit)


@app.get("/api/strategy")
async def get_strategy():
    """Powers the Strategy tab: (1) the CURRENT rule set - buy-side catalog
    plus sell-side catalog (engine/rules_catalog.py, a maintained description
    of rules/swing_buy_rules.py / rules/hard_vetoes.py / rules/exit_scorer.py -
    see that module's docstring for why it's a catalog, not runtime
    introspection) plus the remaining hard-exit sell rules read LIVE from
    config.yaml (rules/sell_rules.py's hard exits are still config-driven -
    what's in config.yaml right now IS the rule; the graduated Exit Score
    buckets are NOT config-driven, same as the buy side, so those come from
    the catalog instead); (2) how the strategy has evolved - recent
    learning_runs (walk-forward attribution/stability), bayesian_weight_history
    (rule-weight change proposals - likely empty, see
    engine/learning_loop.py's docstring on why Bayesian proposals aren't
    auto-generated yet), and champion_challenger history (any promoted/
    discarded challenger configs)."""
    from engine.rules_catalog import get_strategy_catalog
    db = Database()
    cfg = _load_config()
    catalog = get_strategy_catalog()
    scfg = cfg.get("screener", {}) or {}
    gate_cfg = scfg.get("quality_gate", {}) or {}
    learn_cfg = scfg.get("learning", {}) or {}
    max_candidates = scfg.get("max_candidates", 0)
    risk_cfg = cfg.get("risk", {}) or {}
    current_level_cfg = risk_cfg.get(cfg.get("risk_level"), {}) or {}
    return {
        "buy_rules": catalog["buy_rules"],
        "dynamic_thresholds": catalog["dynamic_thresholds"],
        "hard_vetoes": catalog["hard_vetoes"],
        "sell_rules": cfg.get("sell_rules", {}),
        "sell_rules_catalog": catalog["sell_rules"],
        "risk_levels": cfg.get("risk", {}),
        "current_risk_level": cfg.get("risk_level"),
        # 2026-07-22: ATR stop machine (Trinath's review) - catalog describes
        # WHAT each state does (mode-agnostic), live_config is the ACTUAL
        # mode-specific numbers in effect right now for the current risk
        # level (same "catalog vs live" split every other panel on this tab
        # uses). day_pct/swing_pct caps come from the SAME risk.<level> keys
        # engine/stop_state_machine.py itself reads, so this can't drift
        # from what the machine is actually enforcing.
        "stop_machine": {
            "catalog": catalog["stop_machine"],
            "live_config": {
                "DAY": (cfg.get("stop_machine", {}) or {}).get("DAY", {}),
                "SWING": (cfg.get("stop_machine", {}) or {}).get("SWING", {}),
                "atr_spike": (cfg.get("stop_machine", {}) or {}).get("atr_spike", {}),
                "stop_loss_day_pct": current_level_cfg.get("stop_loss_day_pct"),
                "stop_loss_swing_pct": current_level_cfg.get("stop_loss_swing_pct"),
            },
        },
        # 2026-07-14: "all the filtering criteria before scoring" on one page -
        # catalog describes WHAT each of the 3 screener stages does, live_config
        # is the ACTUAL numbers in effect right now (mirrors the sell_rules
        # catalog-vs-live split above). screener_enabled lets the UI show
        # "screener is off, none of this runs" instead of a misleading list.
        "screener_filters": {
            "catalog": catalog["screener_filters"],
            "screener_enabled": scfg.get("enabled", False),
            "live_config": {
                "max_candidates": "uncapped" if max_candidates is None or max_candidates <= 0 else max_candidates,
                "dynamic_by_regime": scfg.get("dynamic_by_regime", True),
                "quality_gate_enabled": gate_cfg.get("enabled", True),
                "min_price": gate_cfg.get("min_price", 10.0),
                "max_price": gate_cfg.get("max_price", 1000.0),
                "min_avg_volume": gate_cfg.get("min_avg_volume", 1_000_000),
                "exclude_unhealthy_tickers": learn_cfg.get("exclude_unhealthy_tickers", True),
                "unhealthy_min_consecutive": learn_cfg.get("unhealthy_min_consecutive", 3),
                "unhealthy_recheck_cooldown_minutes": learn_cfg.get("unhealthy_recheck_cooldown_minutes", 30),
                "exclude_low_quality_tickers": learn_cfg.get("exclude_low_quality_tickers", True),
                "min_track_record": learn_cfg.get("min_track_record", 5),
                "max_qualify_rate_to_exclude": learn_cfg.get("max_qualify_rate_to_exclude", 0.05),
            },
        },
        # 2026-07-23: full-framework Strategy-tab audit (Trinath's "every rule
        # ever used" ask) - these eight were fully live in production/config
        # but had no catalog entry or route exposure before this pass. Same
        # "catalog describes WHAT, live_config is the ACTUAL numbers in
        # effect right now" split every other panel above already uses.
        "market_gate": {
            "catalog": catalog["market_gate"],
            "live_config": cfg.get("market_filters", {}) or {},
        },
        "trading_modes": {
            "catalog": catalog["trading_modes"],
            "live_config": {
                "current_mode": (cfg.get("trading", {}) or {}).get("mode"),
                "max_day_positions": (cfg.get("trading", {}) or {}).get("max_day_positions"),
                "max_positions": (cfg.get("trading", {}) or {}).get("max_positions"),
                "day_eod_flatten_enabled": (cfg.get("trading", {}) or {}).get("day_eod_flatten_enabled"),
                "day_eod_flatten_time_et": (cfg.get("trading", {}) or {}).get("day_eod_flatten_time_et"),
                "day_size_multiplier": (cfg.get("position_sizing", {}) or {}).get("day_size_multiplier"),
            },
        },
        "account_risk": {
            "catalog": catalog["account_risk"],
            "live_config": cfg.get("risk", {}) or {},
        },
        "portfolio_risk": {
            "catalog": catalog["portfolio_risk"],
            "live_config": cfg.get("portfolio_risk", {}) or {},
        },
        "execution_quality": {
            "catalog": catalog["execution_quality"],
            "live_config": cfg.get("execution_quality", {}) or {},
        },
        "position_sizing": {
            "catalog": catalog["position_sizing"],
            "live_config": cfg.get("position_sizing", {}) or {},
        },
        "probabilistic_decision": {
            "catalog": catalog["probabilistic_decision"],
            "live_config": cfg.get("probabilistic_decision", {}) or {},
        },
        "regime_engine": {
            "catalog": catalog["regime_engine"],
            "live_config": {
                "regime_algorithm_version": (cfg.get("learning", {}) or {}).get("regime_algorithm_version"),
            },
        },
        "rotation": {
            "catalog": catalog["rotation"],
            "live_config": cfg.get("rotation", {}) or {},
        },
        "evolution": {
            "learning_runs": db.get_recent_learning_runs(10),
            "bayesian_history": db.get_bayesian_history(limit=50),
            "challenges": db.get_all_challenges(limit=20),
        },
    }


@app.get("/api/analytics/performance")
async def get_performance():
    """Small summary built from analytics/performance.py's existing functions
    (not modified - see README) rather than a bespoke get_performance_summary()
    that doesn't exist there."""
    from analytics.performance import profit_factor, sharpe_ratio, win_rate_by
    db = Database()
    patterns = db.get_patterns(mode=None, closed_only=True)
    outcomes = [p["outcome_pct"] for p in patterns if p.get("outcome_pct") is not None]
    return {
        "n_closed_patterns": len(outcomes),
        "profit_factor": profit_factor(outcomes) if outcomes else 0.0,
        "sharpe_ratio": sharpe_ratio(outcomes) if outcomes else 0.0,
        "win_rate_by_regime": win_rate_by(patterns, "regime"),
    }


@app.get("/api/analytics/attribution")
async def get_trade_attribution():
    """Surfaces analytics/trade_attribution.py (2026-07-22, added while
    implementing Trinath's ATR stop-machine review's suggestion #6: 'log and
    review stop-machine performance... proportion of losing trades that
    stopped out within <=1 ATR of entry and later went >2R in favor (too
    tight) vs. hit the full cap and never moved in favor (too loose)... split
    by DAY vs SWING'). That module already existed - classifies every closed
    pattern_database trade via a real MAE/MFE join (STOP_TOO_TIGHT: MFE ran
    well past the eventual loss before reversing; ENTRY_TOO_EARLY: MAE ran
    hard against the trade almost immediately with little MFE - the exact
    'too tight vs too loose' distinction from the review) - but had no route
    or UI before this.

    Distinct from /api/analytics/regret (also real, already wired): regret
    looks at price action AFTER the exit (was there more room the trade
    left on the table); this looks at price action DURING the trade itself
    (MAE/MFE) to say whether the STOP specifically was the problem. Two
    different data sources answering two different questions about the same
    trades, not a duplicate opinion.

    Returns per-mode breakdowns (attribute_all() takes one mode at a time) so
    the UI can show the DAY vs SWING split the review asked for directly."""
    from analytics.trade_attribution import attribute_all
    db = Database()
    cfg = _load_config()
    return {
        "DAY": attribute_all(db, mode="DAY", cfg=cfg),
        "SWING": attribute_all(db, mode="SWING", cfg=cfg),
    }


@app.get("/api/analytics/regret")
async def get_regret_analysis(force_recompute: bool = False, limit: int = 25):
    """Surfaces analytics/regret_analysis.py (2026-07-17, Akhil: "is it
    possible to build a process that monitors the sold stocks for stop loss
    and see how they behave for a few weeks and learn from the behaviour").
    That module already existed - forward-simulates real price history after
    every closed trade and classifies the exit (stop_too_tight, well_timed,
    etc.) - but had no route calling it anywhere in the codebase.

    mode=None (not the function's own default "SWING") to match the
    /api/analytics/performance precedent above and include every trading
    mode, since paper trades are what this account mostly has right now.

    2026-07-23 fix #1: wrapped in asyncio.to_thread (Trinath: "the tabs in
    the Journal page just say loading and never loads") - build_regret_report()
    calls real yfinance history lookups for any pattern not already cached
    in regret_analysis, and this process runs a single uvicorn worker (see
    main.py) - a blocking synchronous call here froze the ENTIRE server
    (every other tab, every WebSocket update) for as long as those network
    calls took, not just this one request. Same fix, same rationale as
    /api/ticker/evaluate's own asyncio.to_thread above.

    2026-07-23 fix #2: `limit` dropped from a hardcoded 100 to a
    caller-settable default of 25. to_thread stopped this from freezing
    OTHER tabs, but this endpoint's OWN request was still slow on a cold
    cache - each uncached pattern is one sequential ~2s yfinance round trip
    (build_regret_report calls analyze_trade_regret() one pattern at a time,
    no concurrency - see that module's own docstring for why it isn't
    touched here), so 100 patterns on a never-before-run cache is 3+ minutes
    for this one tab, which looks identical to "stuck" from the UI even
    though server.log shows it steadily progressing. 25 bounds a cold first
    load to roughly a minute; every record it evaluates is saved to
    regret_analysis immediately (not just at the end), so nothing already
    computed is repeated - a second call (or the "Refresh" button, which can
    pass a higher &limit=) picks up wherever the last one left off."""
    from analytics.regret_analysis import build_regret_report
    from scheduler import load_config
    db = Database()
    cfg = load_config()
    report = await asyncio.to_thread(
        build_regret_report, db, mode=None, limit=limit, cfg=cfg, force_recompute=force_recompute
    )
    return report


@app.get("/api/analytics/missed_opportunities")
async def get_missed_opportunities(limit: int = 50, force_resim: bool = False):
    """Surfaces analytics/missed_opportunity.py (existed since before this
    route was added but had no caller anywhere in the codebase - same gap
    /api/analytics/regret's docstring above found for regret_analysis.py).
    Every HOLD signal that cleared hard-vetoes and got fully scored but
    missed the dynamic threshold, with the bucket checklist + a real
    yfinance-simulated forward outcome so you can see whether the miss was
    actually a good call.

    asyncio.to_thread wrapped (2026-07-23, see /api/analytics/regret's
    docstring above for why this matters on a single-worker uvicorn
    process) - evaluate_missed_opportunities() hits yfinance for any HOLD
    signal not already cached in missed_opportunity_outcomes."""
    from analytics.missed_opportunity import evaluate_missed_opportunities, missed_opportunity_summary
    from scheduler import load_config
    db = Database()
    cfg = load_config()
    records = await asyncio.to_thread(
        evaluate_missed_opportunities, db, cfg=cfg, limit=limit, force_resim=force_resim
    )
    summary = await asyncio.to_thread(missed_opportunity_summary, db, cfg=cfg, limit=limit)
    return {"records": records, "summary": summary}


@app.get("/api/analytics/threshold_regret")
async def get_threshold_regret(limit: int = 200, force_resim: bool = False, use_cached_run: bool = True):
    """2026-07-23 addition (OXY dynamic-threshold review, Trinath: 'build a
    process for that evaluation and learning so such false negatives are
    identified and at same time potential stocks are not missed'). Segments
    the same HOLD signals as /api/analytics/missed_opportunities above by
    the SIZE of the dynamic-threshold adjustment that rejected them
    (0-3%/3-6%/6-10%/10-15%+) and isolates the market-breadth double-counting
    concern the review specifically flagged (MARKET_BREADTH is both an
    11%-weighted scoring bucket AND a separate threshold penalty).

    use_cached_run=True (default) returns the latest engine/learning_loop.py
    maybe_run_threshold_regret() snapshot from threshold_regret_runs
    (instant - no yfinance calls) if one exists.

    2026-07-23 fix: when NO cached run exists yet (the common case right
    after this feature ships - nothing has triggered the weekly automatic
    run or the manual button yet), this used to silently fall through to a
    live, uncached evaluate_threshold_regret() call - potentially up to
    `limit` sequential yfinance lookups - run SYNCHRONOUSLY inside this
    async route handler. On this process's single uvicorn worker (main.py
    has no workers= argument) that froze the entire server - every other
    tab and the WebSocket feed - for as long as that took, every single
    time the Journal tab was opened (Trinath: "the tabs in the Journal page
    just say loading and never loads"). A plain page load should be fast;
    kicking off potentially-slow, real-network evaluation work belongs
    behind an explicit action. So now: no cached run + force_resim=False ->
    returns immediately with evaluated=False and a clear "not run yet"
    message instead of computing anything - use the Journal tab's "Run Now"
    button (POST /api/analytics/threshold_regret/run) or wait for the
    weekly automatic trigger. Passing force_resim=True still computes live
    (still asyncio.to_thread-wrapped, so it no longer blocks other tabs
    while it runs) for callers who explicitly want that."""
    from analytics.missed_opportunity import evaluate_threshold_regret
    from scheduler import load_config
    db = Database()
    cfg = load_config()

    if use_cached_run and not force_resim:
        cached = db.get_last_threshold_regret_run()
        if cached:
            return cached["report"]
        if not force_resim:
            return {
                "n_signals": 0, "n_evaluated": 0, "n_still_pending": 0, "n_unavailable": 0,
                "overall": {"n": 0}, "by_adjustment_bucket": [], "breadth_isolation": {},
                "not_yet_run": True,
                "message": "No threshold-regret run yet. Click \"Run Now\" or wait for the "
                           "weekly automatic evaluation (engine/learning_loop.py).",
            }

    return await asyncio.to_thread(evaluate_threshold_regret, db, cfg=cfg, limit=limit, force_resim=force_resim)


@app.post("/api/analytics/threshold_regret/run")
async def run_threshold_regret_now(limit: int = 200, force_resim: bool = False,
                                    _: bool = Depends(require_token)):
    """Powers a "Run Now" button for the threshold-regret report, matching
    the Learning tab's "Run Backtest Now" precedent (POST /api/backtest/run)
    - Trinath asked for parity after learning the automatic version only
    fires weekly, inside scheduler.py's own cycle.

    Unlike the backtest button, this returns the result directly in the
    response rather than a background task + polling loop:
    evaluate_threshold_regret() isn't the multi-minute CPU-bound replay
    backtest is (see engine/backtest_loop.py's spawn_backtest_subprocess
    docstring for why THAT one needs a subprocess) - it's mostly cached DB
    reads plus yfinance calls only for signals not already in
    missed_opportunity_outcomes. force_resim=True bypasses that cache too
    (re-hits yfinance for every signal, not just new/still-pending ones) if
    you want a full refresh rather than an incremental one.

    asyncio.to_thread wrapped (2026-07-23 fix - see /api/analytics/regret's
    docstring above for the full story) so the request itself can still take
    a while on a cold cache, but it no longer freezes every other tab and
    the WebSocket feed while it runs.

    Persists the result to threshold_regret_runs (trigger_reason='manual')
    so it shows up in the run history the same as an automatic weekly run -
    the only difference from the automatic path is WHEN it ran, not what it
    computed or whether it's remembered."""
    from analytics.missed_opportunity import evaluate_threshold_regret
    from scheduler import load_config
    db = Database()
    cfg = load_config()
    report = await asyncio.to_thread(evaluate_threshold_regret, db, cfg=cfg, limit=limit, force_resim=force_resim)
    db.log_threshold_regret_run("manual", report)
    return report


@app.get("/api/analytics/threshold_regret/runs")
async def get_threshold_regret_runs(limit: int = 20):
    """Run history (automatic weekly + manual) for the Journal tab panel -
    same shape as /api/backtest/runs."""
    db = Database()
    return db.get_recent_threshold_regret_runs(limit)


@app.get("/api/status")
async def get_status():
    """Market-hours + last-cycle info so the UI can show a helpful empty state
    ('market closed, opens Mon 9:30am ET') instead of a blank dashboard.
    Imports scheduler.py's pure functions (is_market_open/load_config/NYSE
    holiday list) WITHOUT starting its BlockingScheduler - that only happens
    inside scheduler.py's own `if __name__ == "__main__"` block, never on
    import, so this is safe to call from a process that isn't running the
    scan loop itself."""
    from datetime import datetime, timedelta
    import pytz
    from scheduler import is_market_open, load_config, NYSE_HOLIDAYS_2026, _effective_scan_interval

    cfg = load_config()
    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)
    market_open = is_market_open(cfg)

    next_open = None
    if not market_open:
        # open_buf now EXTENDS the scan window earlier (premarket), so the
        # next scan-window open is 9:30 ET MINUS the buffer, not plus - see
        # scheduler.py's is_market_open() docstring for why this flipped.
        open_buf = cfg["trading"].get("market_open_buffer_minutes", 30)
        probe = now
        for _ in range(10):
            probe = (probe + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
            if probe.weekday() < 5 and probe.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS_2026:
                probe -= timedelta(minutes=open_buf)
                next_open = probe.isoformat()
                break
        # same-day case: market hasn't opened yet today
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0) - timedelta(minutes=open_buf)
        if now.weekday() < 5 and now < today_open and now.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS_2026:
            next_open = today_open.isoformat()

    db = Database()
    last_cycle = db.get_last_cycle()
    return {
        "market_open": market_open,
        "server_time_et": now.isoformat(),
        "next_open_et": next_open,
        "last_cycle": last_cycle,
        "kill_switch": cfg.get("risk", {}).get("kill_switch_triggered", False),
        # §6 (Phase 1, 2026-07-24): the same resolved posture the terminal
        # banner prints, so the dashboard cannot show a friendlier answer
        # than the process actually behaves. Derived from
        # engine/live_trader.py's gate functions, never from prose.
        "execution_posture": banner.execution_posture(cfg),
        # actual running cadence - 5 min for DAY/HYBRID modes, otherwise
        # trading.scan_interval_minutes (see scheduler.py's
        # _effective_scan_interval()). Note this reflects config.yaml as of
        # THIS request, not necessarily what the running scheduler.py
        # process is actually using if trading.mode changed since it last
        # started - the cron trigger itself needs a restart to pick that up.
        "scan_interval_minutes": _effective_scan_interval(cfg),
        "manual_cycle_running": _manual_cycle_lock.locked(),
        # Cross-process version of the above - true whether the in-progress
        # cycle is scheduler.py's own cron-triggered run (a SEPARATE process
        # this in-memory lock can't see) or a manual one from this process.
        # See storage/database.py's cycle_status table / scheduler.py's
        # run_cycle() wrapper.
        **db.get_cycle_status(),
    }


def _run_manual_cycle():
    """Runs on a FastAPI BackgroundTasks thread, not the request-handling
    event loop - scheduler.run_cycle() is a long synchronous chain of MCP
    calls (potentially minutes for a full watchlist), and would block every
    other request on this server for that whole time if run inline."""
    from scheduler import run_cycle
    try:
        run_cycle(force=True)
    except Exception:
        logging.getLogger(__name__).exception("Manual cycle run failed")
    finally:
        _manual_cycle_lock.release()


@app.post("/api/cycle/run_now")
async def run_cycle_now(background_tasks: BackgroundTasks,
                         _: bool = Depends(require_token)):
    """On-demand scan cycle, independent of the Mon-Fri 9:30-16:00 ET
    schedule - for testing, or to populate the dashboard without waiting for
    the next scheduled window. Imports and calls scheduler.py's run_cycle()
    directly with force=True (bypasses ONLY the is_market_open() gate - kill
    switch and risk limits still apply, see run_cycle's docstring), the same
    "safe to import scheduler.py's functions without starting its
    BlockingScheduler" pattern /api/status already uses.

    Runs as a background task so the HTTP request returns immediately; the
    UI finds out it's done the same way it finds out about any other cycle -
    the existing 'cycle_complete' ui_event / WebSocket push (see
    _event_poll_loop below), no new plumbing needed. _manual_cycle_lock stops
    a double-click (or a second request while one's still running) from
    firing two overlapping runs from this process; it does NOT prevent
    overlapping with scheduler.py's own scheduled run in the OTHER process -
    low-risk (worst case is a duplicate signals-table row, not a bad trade)
    and not worth cross-process locking infrastructure for."""
    if not _manual_cycle_lock.acquire(blocking=False):
        raise HTTPException(409, "A manual cycle is already running")
    background_tasks.add_task(_run_manual_cycle)
    return {"status": "started"}


@app.get("/api/sources")
async def get_data_sources():
    """Data-source health for the Monitor tab (2026-07-15 - Trinath: "show
    me which MCPs are active and which have issues"). Merges: live health
    rows written by every circuit breaker + yfinance (source_health table,
    cross-process), provider key configuration from the environment, and a
    derived status per source:
      OK              recent success, breaker closed
      DEGRADED        failures accumulating but breaker still closed
      DOWN            breaker open (skipped) or repeated failures
      NOT_CONFIGURED  optional provider with no API key in .env
      NO_DATA_YET     nothing has reported since the last restart
    """
    import os as _os
    import time as _time
    db = Database()
    health = {h["name"]: h for h in db.get_source_health()}

    from mcp_clients import market_data as _md
    provider_keys = {
        "alpaca": bool(_os.getenv("ALPACA_API_KEY") and _os.getenv("ALPACA_API_SECRET")),
        "finnhub": bool(_os.getenv("FINNHUB_API_KEY")),
        "tiingo": bool(_os.getenv("TIINGO_API_KEY")),
        "twelvedata": bool(_os.getenv("TWELVEDATA_API_KEY")),
        "alphavantage": bool(_os.getenv("ALPHAVANTAGE_API_KEY")),
        "fmp": bool(_os.getenv("FMP_API_KEY")),
        "robinhood": bool(_os.getenv("ROBINHOOD_USERNAME") and _os.getenv("ROBINHOOD_PASSWORD")),
        "robinhood-orders": bool(_os.getenv("ROBINHOOD_USERNAME") and _os.getenv("ROBINHOOD_PASSWORD")),
        # 2026-07-22 (unlimited free data source research): both keyless -
        # "configured" means "usable at all", not "env var present". defeatbeta
        # is configured only when the optional package actually imported (see
        # DefeatBetaProvider.key); edgar needs no package/key, just the stdlib.
        "defeatbeta": bool(_md.router.defeatbeta.key),
        "edgar": True,
    }

    CATALOG = [
        # name, kind, role, optional
        ("yfinance", "MCP (uvx yfmcp)", "Quotes/bars/info/news - CORE FALLBACK", False),
        ("maverick", "MCP (HTTP :8003)", "Technicals + news sentiment (EXTERNAL bucket, 12 pts)", True),
        ("finviz", "pip pkg (finviz.com scrape)", "Ratings/analyst/short float/sector (EXTERNAL bucket)", True),
        ("stock-scanner", "MCP (npx)", "Insider trades/short interest/analyst ratings", True),
        ("alpaca", "REST API", "PRIMARY real-time quotes + 1y bars + intraday VWAP", True),
        ("finnhub", "REST API", "Quote backup + real company news", True),
        ("tiingo", "REST API", "EOD bars fallback + IEX quote backup", True),
        ("twelvedata", "REST API", "Last-resort bars/quote (8 credits/min free)", True),
        ("alphavantage", "REST API", "Daily movers discovery + universe symbol listing (~25 req/day budget)", True),
        ("fmp", "REST API", "Movers discovery + stock directory for universe sweep (250 req/day free, 200/day self-cap)", True),
        ("defeatbeta", "pip pkg (HF dataset via DuckDB)", "Keyless, unlimited daily-bars fallback (last in bars chain - see market_data.py)", True),
        ("edgar", "REST API (data.sec.gov, keyless)", "Direct SEC Form 4 insider transactions (preferred over stock-scanner's edgar_insider_trades)", True),
        ("robinhood", "MCP (uvx robinhood-mcp)", "REAL account state: positions/portfolio/orders (read-only)", True),
        ("robinhood-orders", "robin_stocks (direct)", "Live order execution - active ONLY when the Live Execution master switch (typed-phrase protected) + EXECUTE mode + Auto-Trade are ALL on; otherwise execution is Claude-Desktop-only", True),
    ]

    out = []
    now = _time.time()
    for name, kind, role, optional in CATALOG:
        h = health.get(name)
        if name in provider_keys and not provider_keys[name]:
            status = "NOT_CONFIGURED"
            note = "No API key in .env - add it to activate (see .env.template)"
        elif not h:
            status = "NO_DATA_YET"
            note = "No health report since last restart - will appear after the next cycle touches it"
        else:
            breaker_open = (h.get("breaker_open_until") or 0) > now
            fails = h.get("consecutive_failures") or 0
            if breaker_open:
                status = "DOWN"
                mins = ((h["breaker_open_until"] - now) / 60)
                note = (f"Circuit breaker OPEN for another {mins:.0f} min - "
                        f"{h.get('last_error') or 'repeated failures'}")
            elif fails >= 3:
                status = "DOWN"
                note = h.get("last_error") or "repeated failures"
            elif fails > 0:
                status = "DEGRADED"
                note = f"{fails} recent consecutive failure(s): {h.get('last_error') or ''}"
            else:
                status = "OK"
                note = f"Last success {h.get('last_success_at', '?')}"
        out.append({
            "name": name, "kind": kind, "role": role, "optional": optional,
            "status": status, "note": note,
            "last_success_at": (h or {}).get("last_success_at"),
            "last_failure_at": (h or {}).get("last_failure_at"),
            "last_error": (h or {}).get("last_error"),
            "consecutive_failures": (h or {}).get("consecutive_failures", 0),
        })
    return {"sources": out}


@app.post("/api/cycle/cancel")
async def cancel_running_cycle(_: bool = Depends(require_token)):
    """HARD kill switch for a runaway cycle (2026-07-22 - Trinath: "the
    cancel run tab should be able to do all this as well", i.e. the same
    immediate process-group SIGKILL the 15-min auto-kill uses - see
    engine/cycle_supervisor.py's module docstring for the full incident
    writeup). Used to be cooperative-only (set a flag, wait for in-flight
    tickers to finish on their own); now reads the running cycle's CHILD PID
    straight out of cycle_status (recorded by cycle_supervisor.run_supervised()
    right after it spawns the child) and kills that whole process group
    directly, no matter which process actually started it - pid is a
    cross-process handle, the same pattern cycle_status already used for
    next_run_at / is_running. Still also sets the legacy cancel_requested
    flag for anything else that reads it, though the hard kill below makes
    that effectively redundant now.

        curl -X POST http://localhost:8080/api/cycle/cancel
    """
    from engine.cycle_supervisor import kill_current_cycle
    db = Database()
    db.request_cycle_cancel()
    killed = kill_current_cycle(reason="user_cancel")
    if not killed:
        return {"status": "no_cycle_running",
                "note": "Nothing to cancel - no cycle is currently running."}
    return {"status": "killed",
            "note": "Cycle force-killed immediately (the process and every subprocess it spawned)."}


@app.get("/api/analytics/regime_performance")
async def get_regime_performance():
    """Performance broken down by market regime - analytics/performance.py's
    existing performance_by_regime() is exactly this, unmodified."""
    from analytics.performance import performance_by_regime
    db = Database()
    patterns = db.get_patterns(mode=None, closed_only=True)
    return performance_by_regime(patterns)
