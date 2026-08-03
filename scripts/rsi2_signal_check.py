#!/usr/bin/env python3
"""RSI(2) SPY - daily signal-only check (2026-08-02, Trinath's "ready to
use" tier 1: "Daily signal only, no orders"). Places NO trades, touches NO
broker, and does not go through config.yaml's live-execution gates - it
answers rules/rsi2_spy_swing.py's one question ("what does the RSI(2) rule
say to do right now") and tells Trinath via engine/notifications.py's
existing notify() pipeline (same desktop/webhook/log chain the swing
engine already uses - see notifications.py, reused here rather than
inventing a second notification path).

NEEDS NETWORK ACCESS - fetches live SPY daily bars via mcp_clients.
market_data.MarketDataRouter, same provider chain (Alpaca -> FinanceQuery
-> Tiingo -> TwelveData -> defeatbeta) the rest of this platform already
uses for daily bars. Like every other data-fetching script this session,
this must be RUN ON TRINATH'S OWN MACHINE, not inside a sandboxed
assistant session with no general internet access.

STATE FILE (self-tracked, not verified against real fills). Because this
tier places no orders, there is no `positions` table row to read "are we
actually in this trade" from - the script keeps its own small JSON record
at state/rsi2_spy_signal_state.json of what it last told Trinath to do,
and assumes he followed it. If he ever deviates from a signal (skips a
buy, exits early by hand, etc.), this state file will drift from reality -
delete or hand-edit it to correct that. This is an accepted, disclosed
limitation of the no-orders tier; the paper/live tiers read real position
state from the `positions` table instead and do not have this problem.

EXIT TIMING GAP (see rules/rsi2_spy_swing.py's own docstring for the full
explanation): the validated backtest exits AT THE CLOSE that satisfies the
exit condition. A human reading a notification after that close already
happened cannot achieve that fill. This script is explicit about which
case applies each time it runs - see the EXIT_THIS_CLOSE branch below.

Usage (intended to run once/day, after the session's daily bar is final -
e.g. shortly after 4:00pm ET via cron/launchd):
    python3 scripts/rsi2_signal_check.py
    python3 scripts/rsi2_signal_check.py --dry-run   # print only, don't update state or notify
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config_dict
from engine.notifications import notify
from mcp_clients.market_data import MarketDataRouter
from rules import rsi2_spy_swing

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "rsi2_spy_signal_state.json"
TICKER = "SPY"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"in_position": False, "entry_date": None, "entry_price": None}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {**state, "last_updated": datetime.now(timezone.utc).isoformat()}
    STATE_PATH.write_text(json.dumps(state, indent=2))


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
    ap.add_argument("--dry-run", action="store_true", help="print the signal only - don't touch state or notify")
    args = ap.parse_args()

    cfg = load_config_dict()
    daily_bars = fetch_daily_bars()

    state = load_state()
    position = ({"entry_date": state["entry_date"], "entry_price": state["entry_price"]}
                if state.get("in_position") else None)

    result = rsi2_spy_swing.evaluate(daily_bars, position=position)
    print(f"[{result['reference_date']}] close={result['reference_close']:.2f} "
          f"rsi2={result['rsi2']} sma_trend={result['sma_trend']} sma_exit={result['sma_exit']} "
          f"trend_ok={result['trend_ok']} -> action={result['action']} reason={result['reason']}")

    if args.dry_run:
        print("(--dry-run: state and notifications not touched)")
        return

    action = result["action"]
    if action == "ENTER_NEXT_OPEN":
        title = "RSI-2 SPY: BUY signal"
        message = (f"RSI(2)={result['rsi2']} (< {result['rsi_threshold']}) while SPY is above its "
                    f"{rsi2_spy_swing.SMA_TREND_PERIOD}-day trend. Action: buy SPY at the NEXT session's open.")
        notify(cfg, title=title, message=message, subtitle="RSI-2 SPY sleeve (signal-only, no orders placed)")
        # Optimistically record the position as if the buy will be taken -
        # the entry_price/entry_date recorded here are placeholders (today's
        # close and today's date) until Trinath confirms the actual next-
        # open fill; correct state/rsi2_spy_signal_state.json by hand if the
        # real fill differs meaningfully.
        save_state({"in_position": True, "entry_date": result["reference_date"],
                    "entry_price": result["reference_close"]})
    elif action == "EXIT_THIS_CLOSE":
        title = f"RSI-2 SPY: EXIT signal ({result['reason']})"
        message = (f"Exit condition ({result['reason']}) met at today's close "
                    f"({result['reference_date']}, close={result['reference_close']:.2f}, "
                    f"days_held={result.get('days_held')}). The validated backtest exits AT this close - "
                    "if you're reading this after the close already happened, the closest achievable "
                    "execution is SPY's next open; run this check intraday near the close going forward "
                    "to catch the exact validated exit price.")
        notify(cfg, title=title, message=message, subtitle="RSI-2 SPY sleeve (signal-only, no orders placed)",
               severity="warning")
        save_state({"in_position": False, "entry_date": None, "entry_price": None})
    elif action == "HOLD":
        print(f"HOLD - days_held={result.get('days_held')}, no exit condition met. No notification sent "
              "(not actionable).")
    else:  # NO_SIGNAL_STAY_FLAT
        print("No signal - staying flat. No notification sent (not actionable).")


if __name__ == "__main__":
    sys.exit(main())
