#!/usr/bin/env python3
"""Read-only Robinhood account sync / reconciliation CLI (2026-07-15).

Companion to confirm_fill.py. That script records what you TELL it happened;
this one checks what you told it against what your Robinhood account ACTUALLY
holds (via mcp_clients/robinhood_mcp.py - read-only, cannot place orders).

Why this exists: the local `positions` table drives sell_rules, the
ALREADY_OPEN veto, portfolio risk, and the whole learning loop - and until
now its only input was you remembering to run confirm_fill.py after every
manual fill in Claude Desktop. One forgotten fill and the platform is
reasoning about a portfolio that doesn't exist.

Usage:
    python3 robinhood_sync.py status              # portfolio value / buying power
    python3 robinhood_sync.py positions           # real holdings, from Robinhood
    python3 robinhood_sync.py reconcile           # diff Robinhood vs local DB (report only)
    python3 robinhood_sync.py reconcile --apply   # also auto-import missing BUYS

`--apply` deliberately only imports positions that exist on Robinhood but are
missing locally (a forgotten confirm_fill buy) - using the REAL average cost
from the account. It never auto-closes local positions: closing needs your
actual sell fill price, and guessing one would poison P&L learning. For those
it prints the exact confirm_fill.py command to run instead.
"""
import argparse
import sys

from mcp_clients.robinhood_mcp import RobinhoodMCP
from storage.database import Database


def _norm_positions(raw) -> list[dict]:
    """Normalizes robinhood-mcp position payloads to
    [{ticker, shares, avg_cost, equity, current_price}]. Handles both the
    robin_stocks build_holdings dict-keyed-by-ticker shape and a plain list
    of dicts, since the exact wire shape is the server's business, not ours."""
    items = []
    if isinstance(raw, dict):
        items = [{"_ticker": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    elif isinstance(raw, list):
        items = [p for p in raw if isinstance(p, dict)]

    out = []
    for p in items:
        ticker = (p.get("_ticker") or p.get("symbol") or p.get("ticker") or "").upper()
        if not ticker:
            continue

        def _f(*keys):
            for k in keys:
                if p.get(k) not in (None, ""):
                    try:
                        return float(p[k])
                    except (TypeError, ValueError):
                        pass
            return 0.0

        shares = _f("quantity", "shares", "qty")
        if shares <= 0:
            continue
        out.append({
            "ticker": ticker,
            "shares": shares,
            "avg_cost": _f("average_buy_price", "avg_cost", "average_cost", "cost_basis"),
            "equity": _f("equity", "market_value", "value"),
            "current_price": _f("price", "current_price", "last_price"),
        })
    return out


def cmd_status(rh: RobinhoodMCP):
    pf = rh.get_portfolio()
    if not pf:
        print("Could not fetch portfolio (see log warnings - credentials, "
              "first-login timeout, or breaker open). Nothing to show.")
        sys.exit(1)
    print("Robinhood account (read-only):")
    for k in ("total_value", "portfolio_value", "equity", "market_value",
              "buying_power", "cash", "day_change", "day_change_percent",
              "total_return", "total_return_percent"):
        if pf.get(k) not in (None, ""):
            print(f"  {k:24s} {pf[k]}")
    leftover = {k: v for k, v in pf.items() if k not in (
        "total_value", "portfolio_value", "equity", "market_value", "buying_power",
        "cash", "day_change", "day_change_percent", "total_return",
        "total_return_percent")}
    if leftover:
        print(f"  (other fields: {', '.join(leftover.keys())})")


def cmd_positions(rh: RobinhoodMCP):
    positions = _norm_positions(rh.get_positions())
    if not positions:
        print("No positions returned (empty account, or fetch failed - "
              "check the log to tell which).")
        return
    print(f"{'TICKER':8s} {'SHARES':>10s} {'AVG COST':>10s} {'PRICE':>10s} {'EQUITY':>12s}")
    for p in sorted(positions, key=lambda x: -x["equity"]):
        print(f"{p['ticker']:8s} {p['shares']:>10.4f} {p['avg_cost']:>10.2f} "
              f"{p['current_price']:>10.2f} {p['equity']:>12.2f}")


def cmd_seed_paper(rh: RobinhoodMCP):
    """Resets the WATCH-mode paper account and reseeds it to MIRROR the real
    Robinhood account (2026-07-16, Akhil's ask - 'my actual portfolio doesn't
    show correctly for watch'): purse cash = real buying power, and every
    real holding cloned into the simulated book at its REAL average cost.
    Read-only against Robinhood; only the local simulated book is touched
    (real `positions` rows from confirm_fill are untouched - use `reconcile`
    for those). Destructive to the PAPER book: existing paper positions,
    ledger, and equity history are wiped first, so run this to start a fresh
    mirror, not mid-experiment."""
    pf = rh.get_portfolio()
    if not pf:
        print("Robinhood portfolio fetch failed - refusing to seed from unknown state.")
        sys.exit(1)
    positions = _norm_positions(rh.get_positions())

    def _f(*keys):
        for k in keys:
            v = pf.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    buying_power = _f("buying_power", "cash")
    if buying_power is None:
        print(f"Could not read buying power from portfolio response "
              f"(fields: {', '.join(pf.keys())}) - refusing to guess.")
        sys.exit(1)

    db = Database()
    old = db.get_paper_account()
    if old:
        print(f"Wiping existing paper account (cash ${old['cash']:.2f}, "
              f"started ${old['starting_cash']:.2f}) + paper positions/ledger/history...")
    db.reset_paper_account()
    with db._lock, db._conn() as conn:
        conn.execute("DELETE FROM paper_equity_history")

    total_cost = sum(p["avg_cost"] * p["shares"] for p in positions if p["avg_cost"])
    # starting_cash records the full account value (cash + cost basis) so
    # total_return_pct measures against what the mirror actually started with.
    db.init_paper_account(buying_power + total_cost)
    db.adjust_paper_cash(-total_cost)  # holdings' cost is already deployed

    from engine.paper_trader import ensure_seeded  # noqa: F401 (documented alternative)
    for p in positions:
        db.open_position(p["ticker"], p["avg_cost"], p["shares"],
                          round(p["avg_cost"] * p["shares"], 2),
                          simulated=True, trade_mode="SEED")
        db.log_paper_trade(p["ticker"], "buy", p["avg_cost"], p["shares"],
                            round(p["avg_cost"] * p["shares"], 2),
                            reason="seeded_from_robinhood", trade_mode="SEED")

    acct = db.get_paper_account()
    print(f"\nPaper account reseeded to mirror Robinhood:")
    print(f"  cash (buying power):   ${acct['cash']:.2f}")
    print(f"  positions cloned:      {len(positions)}")
    for p in positions:
        print(f"    {p['ticker']:8s} {p['shares']:>10.4f} sh @ ${p['avg_cost']:.2f}")
    print(f"  starting value basis:  ${acct['starting_cash']:.2f}")
    print("\nThe Portfolio tab will show this immediately; the sell rules "
          "manage the cloned positions from the next scan cycle.")


def cmd_clear_seed():
    """Removes every trade_mode='SEED' position left over from a previous
    `seed-paper` run (2026-07-23, Trinath's ask: seeded holdings were
    counting against trading.max_positions and crowding out genuine WATCH
    signals - the engine now excludes SEED from that count going forward
    regardless, but this cleans up what's already sitting in the DB).
    Doesn't touch Robinhood (read-only either way) or any real `positions`
    row from confirm_fill.py - only the simulated clones this script itself
    created."""
    db = Database()
    result = db.remove_seed_positions()
    if not result["removed"]:
        print("No SEED positions found - nothing to remove.")
        return
    print(f"Removed {result['removed']} seeded position(s): {', '.join(result['tickers'])}")
    print(f"Credited ${result['cash_credited']:.2f} back to the paper account's cash "
          f"(their cost basis).")
    print("These no longer count toward trading.max_positions either way, but "
          "they're now also gone from the Portfolio tab.")


def cmd_clear_sync():
    """Removes every trade_mode='SYNC' position engine/account_sync.py
    auto-imported into the REAL book (config.yaml account.auto_sync, once
    per cycle while enabled). Doesn't touch Robinhood (account_sync.py is
    read-only against the brokerage) or place any order - only deletes the
    LOCAL tracking row, so the platform stops counting/health-scoring/
    stop-managing/rotating a position it never actually decided to enter
    itself. Does NOT disable account.auto_sync - if you don't want this to
    happen again, flip that off in config.yaml or the Control tab (it's
    false by default)."""
    db = Database()
    result = db.remove_synced_positions()
    if not result["removed"]:
        print("No SYNC positions found - nothing to remove.")
        return
    print(f"Removed {result['removed']} synced position(s): {', '.join(result['tickers'])}")
    print("Your real Robinhood account is untouched - this only removed the local tracking row.")
    print("account.auto_sync is unchanged - check config.yaml/Control tab if you want to turn it off too.")


def cmd_reconcile(rh: RobinhoodMCP, apply: bool):
    raw = rh.get_positions()
    # Critical distinction: a failed fetch must NOT read as "account is flat" -
    # otherwise --apply logic (and the human reading the report) would treat
    # every local position as stale. get_portfolio() doubles as the health probe.
    if not raw and not rh.get_portfolio():
        print("Robinhood fetch failed - refusing to reconcile against unknown "
              "state (a dead fetch is not an empty account).")
        sys.exit(1)

    rh_positions = {p["ticker"]: p for p in _norm_positions(raw)}
    db = Database()
    local = {p["ticker"].upper(): p for p in db.get_all_positions()}

    missing_local = [t for t in rh_positions if t not in local]
    stale_local = [t for t in local if t not in rh_positions]
    mismatched = [
        t for t in rh_positions if t in local
        and abs(float(local[t].get("shares") or 0) - rh_positions[t]["shares"]) > 1e-4
    ]

    if not (missing_local or stale_local or mismatched):
        print(f"In sync: {len(local)} local open position(s) match Robinhood exactly.")
        return

    if missing_local:
        print(f"\nOn Robinhood but NOT in local DB ({len(missing_local)}) - "
              f"forgotten confirm_fill buy?")
        for t in missing_local:
            p = rh_positions[t]
            print(f"  {t}: {p['shares']} shares @ avg ${p['avg_cost']:.2f}")
            if not apply:
                print(f"    -> python3 confirm_fill.py buy {t} {p['avg_cost']:.2f} {p['shares']}")

    if stale_local:
        print(f"\nIn local DB but NOT on Robinhood ({len(stale_local)}) - "
              f"forgotten confirm_fill sell? NOT auto-closed (needs your real "
              f"fill price; guessing would poison P&L learning):")
        for t in stale_local:
            print(f"  {t}: local entry ${float(local[t].get('entry_price') or 0):.2f}, "
                  f"{local[t].get('shares')} shares")
            print(f"    -> python3 confirm_fill.py sell {t} <your_actual_fill_price>")

    if mismatched:
        print(f"\nShare-count mismatches ({len(mismatched)}) - partial fill or "
              f"partial sell recorded wrong? Fix manually via confirm_fill.py:")
        for t in mismatched:
            print(f"  {t}: Robinhood {rh_positions[t]['shares']} vs "
                  f"local {local[t].get('shares')}")

    if apply and missing_local:
        print(f"\n--apply: importing {len(missing_local)} missing position(s) "
              f"via confirm_fill's own buy path (links patterns, seeds stops, "
              f"snapshots - identical to running it by hand)...")
        import confirm_fill
        for t in missing_local:
            p = rh_positions[t]
            try:
                confirm_fill.cmd_buy(t, round(p["avg_cost"], 2), p["shares"])
            except SystemExit:
                # cmd_buy exits(1) on "already open" - can't happen here since
                # we filtered to missing tickers, but a concurrent scheduler
                # cycle could theoretically race us; don't die mid-import.
                print(f"  {t}: skipped (confirm_fill refused - see message above)")
    elif apply:
        print("\n--apply: nothing importable (only missing/stale sells or "
              "mismatches, which are manual by design).")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("positions")
    sub.add_parser("seed-paper",
                   help="reset + reseed the WATCH-mode paper account to mirror "
                        "the real Robinhood account (buying power + holdings)")
    sub.add_parser("clear-seed",
                   help="remove SEED positions left over from a previous seed-paper run "
                        "(doesn't touch Robinhood or real confirm_fill.py positions)")
    sub.add_parser("clear-sync",
                   help="remove SYNC positions auto-imported by engine/account_sync.py "
                        "(doesn't touch Robinhood or disable account.auto_sync)")
    rec = sub.add_parser("reconcile")
    rec.add_argument("--apply", action="store_true",
                     help="auto-import positions missing locally (buys only)")
    args = parser.parse_args()

    # clear-seed/clear-sync are local-DB-only - no Robinhood credentials/fetch needed.
    if args.command == "clear-seed":
        cmd_clear_seed()
        return
    if args.command == "clear-sync":
        cmd_clear_sync()
        return

    rh = RobinhoodMCP()
    if not rh.configured():
        print("Robinhood credentials not configured. Add to .env:\n"
              "  ROBINHOOD_USERNAME=your_email\n"
              "  ROBINHOOD_PASSWORD=your_password\n"
              "  ROBINHOOD_TOTP_SECRET=...   # only if you use an authenticator app\n"
              "then re-run. See README 'Robinhood (read-only)'.")
        sys.exit(1)

    if args.command == "status":
        cmd_status(rh)
    elif args.command == "positions":
        cmd_positions(rh)
    elif args.command == "seed-paper":
        cmd_seed_paper(rh)
    elif args.command == "reconcile":
        cmd_reconcile(rh, apply=args.apply)


if __name__ == "__main__":
    main()
