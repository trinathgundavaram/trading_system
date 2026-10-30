#!/usr/bin/env python3
"""Progress of the 2026-10-30 swing experiment (see engine/swing_experiment.py).
    python3 scripts/swing_experiment_report.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.swing_experiment import summarize
from storage.database import Database


def main():
    db = Database()
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        trades = [dict(r) for r in conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()]
    s = summarize(trades)
    print(f"Swing experiment since {s['start']}: {s['round_trips']} round trips over {s['entry_days']} entry days "
          f"(needs {s['needs_days']}).")
    if s["round_trips"]:
        pf = "n/a" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
        print(f"  avg {s['avg_pct']:+.2f}%/trade, win {s['win_pct']:.0f}%, profit factor {pf} (needs > {s['needs_pf']})")
    print(f"  verdict: {s['verdict'].upper()}")


if __name__ == "__main__":
    main()
