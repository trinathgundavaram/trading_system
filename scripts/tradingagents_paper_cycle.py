#!/usr/bin/env python3
"""Daily TradingAgents advisory run. Gated by tradingagents_advisor.enabled
(default false). Logs ratings only - places no orders.
    python3 scripts/tradingagents_paper_cycle.py [--dry-run]
"""
import argparse
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config_dict
from engine import tradingagents_advisor as adv
from storage.database import Database


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_config_dict()
    if not cfg.get("tradingagents_advisor", {}).get("enabled", False):
        print("tradingagents_advisor.enabled is false in config.yaml - not running.")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set - not running (no key is ever read from config.yaml).")
        return 1
    db = Database()
    today = date.today().isoformat()
    names = adv.shortlist(cfg, db)
    print(f"[{today}] shortlist: {names}")
    if a.dry_run or not names:
        return
    import yfinance as yf
    for t in names:
        try:
            rating, raw = adv.analyze(t, today, cfg)
        except ImportError as e:
            print(e); return 1
        except Exception as e:                       # one bad ticker must not stop the rest
            print(f"  {t}: failed ({e})"); continue
        px = float(yf.Ticker(t).history(period="5d")["Close"].iloc[-1])
        db.log_ta_decision(today, t, rating, raw, px)
        print(f"  {t}: {rating} @ {px:.2f}")


if __name__ == "__main__":
    sys.exit(main())
