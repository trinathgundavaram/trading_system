#!/usr/bin/env python3
"""Raise (or lower) the paper account's capital base to match config.yaml's
``paper_trading.starting_cash``, WITHOUT resetting anything.

Why this exists (2026-07-26, Akhil: "set the paper portfolio to 10000 so that
it does more trades and has a lot more data sooner"):

``engine/paper_trader.ensure_seeded()`` returns early when the purse row
already exists, so editing ``paper_trading.starting_cash`` in config.yaml has
NO effect on a live account - it only applies to the next seed. The two
options that do take effect are:

  1. ``db.reset_paper_account()`` (what robinhood_sync.py's seed-paper does) -
     which deletes the purse, the paper_trades ledger, the equity curve and
     every simulated position. Correct for a genuinely fresh start, and
     wrong here: the whole reason for raising the balance is to accumulate
     more data, and step one would have been to throw away the ten days of it
     that already exist.
  2. This script - an in-place capital contribution. The ledger, the equity
     curve, the open simulated positions and realized_pnl all survive.

WHAT "SET THE PORTFOLIO TO 10000" MEANS HERE
--------------------------------------------
The target is the account's BASIS (starting_cash), not its cash balance. With
$135.25 currently deployed in CPRX and AES, a $10,000 basis means $9,864.75
cash + $135.25 at work, not $10,000 sitting idle plus positions on top. Basis
is the right target because it is what ``total_return_pct`` is measured
against and what reconcile.py's cash invariant is stated in terms of - setting
the CASH to a round number would leave the basis at an odd one and make every
return figure slightly harder to reason about.

The contribution is applied via Database.credit_paper_capital(), which moves
starting_cash and cash by the same amount in a single statement so that
``cash == starting_cash - net_buys`` continues to hold. See that method's
docstring for why moving cash alone is a bug rather than a shortcut.

USAGE
-----
    python3 scripts/topup_paper_account.py            # dry run, prints the plan
    python3 scripts/topup_paper_account.py --apply    # actually writes

Reads the live database (Postgres, per storage/database.py) - not the stale
output/trading.db sqlite file, which has been unused since the 2026-07-21
migration.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config_loader import load_config_dict  # noqa: E402
from storage.database import Database  # noqa: E402


def _net_buys(db) -> float:
    """Net cash the ledger says has left the purse: buys minus sell proceeds.

    Same expression scripts/reconcile.py's "paper cash disagrees with the
    trade ledger" check uses, deliberately - if the two ever disagree about
    what the ledger means, this script would happily 'fix' the balance into a
    state reconcile.py then reports as broken.
    """
    with db._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN side = 'buy' THEN dollar_amount "
            "ELSE -dollar_amount END), 0) FROM paper_trades").fetchone()
    return float(row[0] or 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default is a dry run)")
    ap.add_argument("--target", type=float, default=None,
                    help="override the target basis; default is config.yaml's "
                         "paper_trading.starting_cash")
    args = ap.parse_args()

    cfg = load_config_dict()
    target = args.target if args.target is not None else float(
        (cfg.get("paper_trading", {}) or {}).get("starting_cash", 1000.0))

    db = Database()
    acct = db.get_paper_account()
    if not acct:
        print("No paper account exists yet - nothing to top up.")
        print("The next WATCH-mode cycle will seed one at "
              f"${target:,.2f} via engine/paper_trader.ensure_seeded().")
        return 0

    basis = float(acct["starting_cash"])
    cash = float(acct["cash"])
    net = _net_buys(db)
    delta = target - basis

    # Refuse to operate on a purse that is ALREADY inconsistent. Adding capital
    # on top of existing drift preserves the drift and buries its cause under a
    # deliberate adjustment, making the eventual diagnosis much harder.
    drift = cash - (basis - net)
    if abs(drift) > 0.01:
        print(f"REFUSING: the purse does not reconcile with its own ledger.")
        print(f"  cash            ${cash:,.2f}")
        print(f"  starting_cash   ${basis:,.2f}")
        print(f"  net buys        ${net:,.2f}")
        print(f"  expected cash   ${basis - net:,.2f}   (drift ${drift:,.2f})")
        print("\nRun scripts/reconcile.py and resolve that first - topping up "
              "now would fold this discrepancy into the new balance.")
        return 1

    print(f"  current basis   ${basis:,.2f}")
    print(f"  current cash    ${cash:,.2f}  (${net:,.2f} deployed in open positions)")
    print(f"  realized P&L    ${float(acct.get('realized_pnl') or 0):,.2f}  (untouched)")
    print(f"  target basis    ${target:,.2f}   <- config.yaml paper_trading.starting_cash")
    print(f"  contribution    ${delta:+,.2f}")

    if abs(delta) < 0.01:
        print("\nAlready at target - nothing to do.")
        return 0

    print(f"\n  after: basis ${target:,.2f}, cash ${cash + delta:,.2f}, "
          f"realized P&L unchanged")
    print("  (a capital contribution, NOT a gain - total_return_pct is measured "
          "against the new basis, so this does not show up as performance)")

    if not args.apply:
        print("\nDRY RUN - re-run with --apply to write.")
        return 0

    db.credit_paper_capital(delta)
    after = db.get_paper_account()
    print(f"\nDONE: basis ${float(after['starting_cash']):,.2f}, "
          f"cash ${float(after['cash']):,.2f}")
    print("Verify with: python3 scripts/reconcile.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
