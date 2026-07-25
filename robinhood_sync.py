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
import os
import subprocess
import sys
from pathlib import Path

from mcp_clients.robinhood_mcp import RobinhoodMCP
from storage.database import Database

# §C2: `seed-paper` calls reset_paper_account(), which unconditionally deletes
# the purse, the ledger, every simulated position and - since §48 - the equity
# curve. That is the single most destructive operation reachable from a CLI in
# this repository, and until now it ran on the statement AFTER the one that
# printed what it was about to destroy.
#
# The confirmation phrase mirrors engine/live_trader.py's
# LIVE_EXECUTION_CONFIRM_PHRASE, and for the same reason: a y/n prompt is
# answered reflexively, a phrase you have to read and type is not. The two
# phrases are deliberately different so that muscle memory from one cannot
# satisfy the other.
SEED_PAPER_CONFIRM_PHRASE = "RESET PAPER ACCOUNT"


def _require_backup(db: Database, skip: bool = False) -> None:
    """Take a verified backup before a destructive paper-book operation, or
    refuse to proceed.

    scripts/tp backup already does the hard part - it dumps, then reads the
    dump back before reporting success, because an unverified backup is a
    belief rather than a backup. This just makes it non-optional on the path
    that needs it most.

    --skip-backup exists for the test suite and for the case where you have
    just taken one by hand. It prints loudly, because the whole point is that
    skipping is a decision someone made rather than a default nobody noticed.
    """
    if skip:
        print("  [--skip-backup] Proceeding WITHOUT a backup. This is "
              "unrecoverable if it goes wrong.")
        return

    tp = Path(__file__).resolve().parent / "scripts" / "tp"
    if not tp.exists():
        print(f"  Cannot find {tp} - refusing to run a destructive reset "
              f"without a backup. Take a pg_dump by hand and re-run with "
              f"--skip-backup.")
        sys.exit(1)

    print("  Taking a verified backup first (scripts/tp backup)...")
    try:
        r = subprocess.run([str(tp), "backup", "pre-seed-paper"],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  Backup command failed to run: {e}\n"
              f"  Refusing to reset. Take a pg_dump by hand and re-run with "
              f"--skip-backup.")
        sys.exit(1)

    if r.returncode != 0:
        # Deliberately NOT offering to continue anyway. A backup that failed
        # is the one condition under which this operation must not proceed,
        # and prompting here would just relocate the mistake.
        print(f"  Backup FAILED (exit {r.returncode}):\n{r.stdout}\n{r.stderr}\n"
              f"  Refusing to reset.")
        sys.exit(1)
    print("  Backup verified.")


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


def cmd_seed_paper(rh: RobinhoodMCP, assume_yes: bool = False,
                   skip_backup: bool = False):
    """Resets the WATCH-mode paper account and reseeds it to MIRROR the real
    Robinhood account (2026-07-16, Akhil's ask - 'my actual portfolio doesn't
    show correctly for watch'): purse cash = real buying power, and every
    real holding cloned into the simulated book at its REAL average cost.
    Read-only against Robinhood; only the local simulated book is touched
    (real `positions` rows from confirm_fill are untouched - use `reconcile`
    for those). Destructive to the PAPER book: existing paper positions,
    ledger, and equity history are wiped first, so run this to start a fresh
    mirror, not mid-experiment.

    §C2 (2026-07-25 review): now gated on a verified backup and a typed
    confirmation phrase. It previously printed what it was about to destroy
    and destroyed it on the next statement, with no way to stop in between -
    every other destructive path in this repo (repair_test_damage.py's
    --apply, live_trader's confirm phrase, tp backup's read-back check) has a
    gate, and this one wipes the equity curve that every drawdown figure is
    computed from.
    """
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

    # ── The gate ────────────────────────────────────────────────────────────
    # Everything above this point is read-only (Robinhood fetches and
    # arithmetic). Everything below it is irreversible. Say plainly what goes.
    print("\n" + "=" * 68)
    print("DESTRUCTIVE: reset_paper_account() is about to delete, permanently:")
    if old:
        print(f"  - the paper purse            cash ${old['cash']:.2f}, "
              f"started ${old['starting_cash']:.2f}")
    else:
        print("  - the paper purse            (none currently)")
    _sim = db.get_all_positions(simulated=True) or []
    n_open = sum(1 for p in _sim if p.get("status") == "open")
    n_curve = len(db.get_paper_equity_history(limit=100000) or [])
    print(f"  - every simulated position   {n_open} open, {len(_sim)} total")
    print(f"  - the whole paper ledger     (paper_trades)")
    print(f"  - the equity curve           {n_curve} point(s)")
    print("")
    print("The equity curve is the input to every drawdown figure. Deleting it")
    print("is correct HERE - a mirror of a different account is a new epoch and")
    print("should not inherit the old curve - but it is not recoverable without")
    print("the backup below.")
    print("")
    print("NOT touched: pattern_database (the learning record), mae_mfe_data,")
    print("and every real (non-simulated) position from confirm_fill.py.")
    print("=" * 68)

    if assume_yes:
        print(f"  [--yes] Confirmation phrase skipped.")
    else:
        try:
            typed = input(f"\nType {SEED_PAPER_CONFIRM_PHRASE!r} to proceed "
                          f"(anything else aborts): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted - nothing was changed.")
            sys.exit(1)
        if typed != SEED_PAPER_CONFIRM_PHRASE:
            print("Phrase did not match. Aborted - nothing was changed.")
            sys.exit(1)

    _require_backup(db, skip=skip_backup)

    print("  Resetting...")
    # §48 made reset_paper_account() clear paper_equity_history itself, so the
    # separate DELETE that used to sit here (reaching into db._lock/db._conn -
    # private API, from a top-level script) is gone. It was harmless but
    # actively misleading: a reader seeing it here would reasonably conclude
    # the reset does NOT clear the curve, and add the same compensating delete
    # somewhere else too.
    db.reset_paper_account()

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
    seed = sub.add_parser("seed-paper",
                          help="DESTRUCTIVE: reset + reseed the WATCH-mode paper "
                               "account to mirror the real Robinhood account "
                               "(buying power + holdings). Wipes the purse, "
                               "ledger, simulated positions and equity curve.")
    seed.add_argument("--yes", action="store_true",
                      help="skip the typed confirmation phrase (for scripted use "
                           "- the backup is still taken)")
    seed.add_argument("--skip-backup", action="store_true",
                      help="skip the automatic `tp backup`. Only if you have "
                           "just taken one by hand.")
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
        cmd_seed_paper(rh, assume_yes=args.yes, skip_backup=args.skip_backup)
    elif args.command == "reconcile":
        cmd_reconcile(rh, apply=args.apply)


if __name__ == "__main__":
    main()
