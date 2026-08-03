#!/usr/bin/env python3
"""RSI(2) SPY - PAPER trading daily cycle (2026-08-02, tier 2 of Trinath's
"ready to use" deployment). Places SIMULATED orders only, against the
sleeve's own dedicated cash ledger (engine/rsi2_paper_trader.py, storage/
database.py's rsi2_account/rsi2_positions) - never touches paper_trader.py's
shared paper_account/positions, and never touches a broker.

GATED OFF BY DEFAULT: config.yaml's rsi2_sleeve.paper_enabled must be
explicitly set to true. This script checks that gate itself (not just
relying on nobody scheduling it) so that adding a cron/launchd entry for it
is not, by itself, enough to start trading - the same "explicit opt-in,
not merely absence of a blocker" posture config.yaml's trading.auto_trade
already uses for the swing engine.

Same daily cadence and data source as scripts/rsi2_signal_check.py (see
that file for the full explanation of why this needs to run once/day near
the close, not on swing's 5-15 minute cycle, and why it needs network
access this sandbox doesn't have - run on Trinath's own machine).

Usage (intended to run once/day, after the session's daily bar is final):
    python3 scripts/rsi2_paper_cycle.py
    python3 scripts/rsi2_paper_cycle.py --dry-run   # print only, no trades/notifications
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config_dict
from engine import rsi2_paper_trader
from engine.notifications import notify
from mcp_clients.market_data import MarketDataRouter
from rules import rsi2_spy_swing
from storage.database import Database

TICKER = "SPY"


def fetch_daily_bars() -> pd.DataFrame:
    router = MarketDataRouter()
    result = router.get_daily_bars(TICKER)
    if not result:
        raise RuntimeError(f"MarketDataRouter.get_daily_bars({TICKER!r}) returned nothing - "
                            "no configured provider had data available. Check API keys / .env.")
    bars, provider_name = result
    df = pd.DataFrame(bars)
    print(f"Fetched {len(df)} daily bars for {TICKER} from provider={provider_name}")
    return df.sort_values("date").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the signal only - no trades, no notifications")
    args = ap.parse_args()

    cfg = load_config_dict()
    if not cfg.get("rsi2_sleeve", {}).get("paper_enabled", False):
        print("rsi2_sleeve.paper_enabled is false in config.yaml - not running. "
              "Set it to true explicitly to start paper-trading this sleeve.")
        return

    db = Database()
    rsi2_paper_trader.ensure_seeded(db, cfg)
    daily_bars = fetch_daily_bars()

    db_position = db.get_open_rsi2_position()
    position = ({"entry_date": db_position["entry_date"], "entry_price": db_position["entry_price"]}
                if db_position else None)

    result = rsi2_spy_swing.evaluate(daily_bars, position=position)
    print(f"[{result['reference_date']}] close={result['reference_close']:.2f} "
          f"rsi2={result['rsi2']} sma_trend={result['sma_trend']} sma_exit={result['sma_exit']} "
          f"trend_ok={result['trend_ok']} -> action={result['action']} reason={result['reason']}")

    if args.dry_run:
        print("(--dry-run: no trades or notifications)")
        return

    action = result["action"]
    if action == "ENTER_NEXT_OPEN":
        # This tier runs once/day near the close and evaluates off that same
        # close - it does not have tomorrow's real open yet, so it uses
        # today's close as the paper fill price. This is a disclosed,
        # slightly-optimistic simplification versus the backtest's real
        # next-open fill (see rules/rsi2_spy_swing.py's docstring) - close
        # to open on SPY is typically a small gap, but it is not zero.
        fill_price = result["reference_close"]
        trade = rsi2_paper_trader.execute_buy(db, cfg, price=fill_price, entry_date=result["reference_date"])
        if trade:
            notify(cfg, title="RSI-2 SPY (paper): BUY executed",
                   message=f"{trade['shares']:.4f} shares @ ${trade['entry_price']:.2f} "
                           f"(${trade['dollar_amount']:.2f}) on {trade['entry_date']}",
                   subtitle="RSI-2 SPY sleeve - PAPER, simulated fill only")
    elif action == "EXIT_THIS_CLOSE":
        fill_price = result["reference_close"]
        trade = rsi2_paper_trader.execute_sell(db, cfg, price=fill_price, reason=result["reason"])
        if trade:
            notify(cfg, title=f"RSI-2 SPY (paper): EXIT executed ({trade['reason']})",
                   message=f"Sold @ ${trade['exit_price']:.2f} (${trade['proceeds']:.2f}, "
                           f"pnl=${trade['realized_pnl']:+.2f}) on {trade['exit_date']}",
                   subtitle="RSI-2 SPY sleeve - PAPER, simulated fill only",
                   severity="warning" if trade["realized_pnl"] < 0 else "info")
    elif action == "HOLD":
        print(f"HOLD - days_held={result.get('days_held')}, no exit condition met.")
    else:  # NO_SIGNAL_STAY_FLAT
        print("No signal - staying flat.")

    snap = rsi2_paper_trader.snapshot(db, current_price=result["reference_close"])
    print(f"Sleeve snapshot: cash=${snap['cash']:.2f} realized_pnl=${snap['realized_pnl']:+.2f} "
          f"total_value=${snap['total_value']:.2f} total_return={snap['total_return_pct']}%")


if __name__ == "__main__":
    sys.exit(main())
