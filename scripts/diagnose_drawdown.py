#!/usr/bin/env python3
"""Is the drawdown that tripped the kill switch REAL, or an accounting event?

READ-ONLY. Writes nothing, clears nothing, and deliberately cannot: the whole
point is to answer the question before anyone touches the switch.

WHY THIS EXISTS (2026-07-25 23:24:58, paper running drawdown 16.48% >= 15.0%)

§11's running drawdown is `(all_time_peak - latest) / all_time_peak`, and
storage/database.py already knows the trap: an equity series from a different
starting balance is a DIFFERENT SERIES, and comparing across the join produces
a drawdown that never happened. The comment there describes the exact incident
- a curve running at ~984 for eight days, jumping to 1491.54 on a re-seed, and
every subsequent day then reading a ~34% drawdown against a 15% cap. A running
breach trips the kill switch, so an accounting event halts trading.

The guard is `_paper_epoch_start()`: the peak is taken only from rows at or
after the current paper account's `created_at`. That fixes a RESET. It does
NOT fix a re-seed that happens WITHIN the current epoch - robinhood_sync can
change the balance without deleting the account, and §48's rebase has not been
run on this machine yet.

So the question is narrow and answerable: does the curve, SINCE THE EPOCH,
contain a jump that no trade explains? If yes, the peak is inherited from a
balance the account no longer has, and the percentage is arithmetic about two
different accounts. If no, the drawdown is real and the halt was correct.

    python3 scripts/diagnose_drawdown.py
    python3 scripts/diagnose_drawdown.py --days 60
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BANNER = "=" * 78

# WHAT COUNTS AS A RE-SEED, and why the obvious test is wrong.
#
# The first version of this flagged any large move in total_value not matched
# by realized P&L - and immediately called a perfectly ordinary 7.8% market
# decline "unexplained", because unrealized losses do not touch realized_pnl.
# That is not a detector, it is a mood.
#
# total_value = cash + market_value, so decompose the step instead:
#
#   market moves     cash 0,      market +/-      <- legitimate, and the whole
#                                                     point of holding anything
#   buy              cash -X,     market +X       <- offsetting, net ~0
#   sell             cash +X,     market -X       <- offsetting, net ~0
#   RE-SEED          cash +X,     market 0        <- cash appears from nowhere
#
# So the signature is a large CASH move that market_value does not offset.
# Market movement never triggers it (cash is flat), and neither does trading
# (the two legs cancel). That is a statement about the accounting identity
# rather than a threshold anyone has to believe.
STEP_PCT = 5.0
# How much of the cash move must go unmatched by market_value before it counts.
# 0.5 = half of it, which no buy or sell can produce.
UNMATCHED_SHARE = 0.5


def fetch(db, sql: str, columns: list[str]) -> list[dict]:
    with db._conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(zip(columns, r)) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=45,
                   help="how much of the curve to print (default 45 samples)")
    args = p.parse_args()

    from storage.database import Database

    db = Database()
    epoch = db._paper_epoch_start()

    cols = ["timestamp", "total_value", "cash", "market_value",
            "realized_pnl", "n_open"]
    rows = fetch(db, f"""
        SELECT {", ".join(cols)}
          FROM paper_equity_history
         ORDER BY timestamp
    """, cols)

    print(BANNER)
    print("  PAPER EQUITY CURVE - is the running drawdown real?")
    print(BANNER)
    print(f"  paper account epoch : {epoch or 'NONE (falls back to the whole table)'}")
    print(f"  samples in table    : {len(rows)}")
    if not rows:
        print("\n  The curve is empty, so running drawdown is 0 by construction.")
        print("  If the switch tripped anyway, the trip predates this table.")
        return 0

    in_epoch = [r for r in rows if not epoch or str(r["timestamp"]) >= str(epoch)]
    print(f"  samples since epoch : {len(in_epoch)}")
    if len(in_epoch) < 2:
        print("\n  Fewer than two samples since the epoch. A drawdown computed")
        print("  from this is not a measurement of anything.")
        return 1

    vals = [float(r["total_value"] or 0) for r in in_epoch]
    peak = max(vals)
    latest = vals[-1]
    peak_i = vals.index(peak)
    running = (peak - latest) / peak * 100 if peak > 0 else 0.0

    print()
    print(f"  peak since epoch    : {peak:,.2f}   at {in_epoch[peak_i]['timestamp']}")
    print(f"  latest              : {latest:,.2f}   at {in_epoch[-1]['timestamp']}")
    print(f"  running drawdown    : {running:.2f}%")

    # ── discontinuities ─────────────────────────────────────────────────────
    print()
    print(BANNER)
    print(f"  CASH MOVES OVER {STEP_PCT}% THAT market_value DOES NOT OFFSET")
    print(BANNER)
    jumps = []
    for a, b in zip(in_epoch, in_epoch[1:]):
        va, vb = float(a["total_value"] or 0), float(b["total_value"] or 0)
        if va <= 0:
            continue
        d_cash = float(b["cash"] or 0) - float(a["cash"] or 0)
        d_mkt = float(b["market_value"] or 0) - float(a["market_value"] or 0)
        # A buy or a sell moves these two in opposite directions by the same
        # amount, so their SUM is what survives - and only a balance change
        # makes it large.
        unmatched = d_cash + d_mkt
        if (abs(d_cash) >= STEP_PCT / 100 * va
                and abs(unmatched) >= UNMATCHED_SHARE * abs(d_cash)):
            jumps.append((a, b, (vb - va) / va * 100, d_cash, d_mkt, unmatched))

    if not jumps:
        print("  none. Every cash move in the curve is offset by market_value,")
        print("  which is what a buy or a sell looks like. No balance appeared")
        print("  or vanished on its own.")
    for a, b, pct, d_cash, d_mkt, unmatched in jumps:
        print(f"  {a['timestamp']}  total {float(a['total_value']):>10,.2f}")
        print(f"  {b['timestamp']}  total {float(b['total_value']):>10,.2f}   {pct:+.2f}%")
        print(f"      cash {d_cash:+,.2f}   market_value {d_mkt:+,.2f}   "
              f"-> {unmatched:+,.2f} unmatched")
        print()

    # ── the counterfactual ──────────────────────────────────────────────────
    print(BANNER)
    print("  VERDICT")
    print(BANNER)
    if jumps:
        # REBASE, rather than "measure from the last jump". Measuring from the
        # last jump handles a permanent re-seed and completely misses the worse
        # case: a TRANSIENT spike, where one bad balance sample becomes the
        # all-time peak and every day afterwards is measured against a number
        # the account held for five minutes. Subtracting each unmatched cash
        # movement from everything after it reconstructs one continuous series
        # - which is what §48's rebase does to the stored curve, done here
        # arithmetically and without writing anything.
        unmatched_at = {str(b["timestamp"]): u for _, b, _, _, _, u in jumps}
        adjusted, shift = [], 0.0
        for r in in_epoch:
            shift += unmatched_at.get(str(r["timestamp"]), 0.0)
            adjusted.append(float(r["total_value"] or 0) - shift)
        peak_adj = max(adjusted)
        latest_adj = adjusted[-1]
        dd_after = ((peak_adj - latest_adj) / peak_adj * 100) if peak_adj > 0 else 0.0

        print(f"  The curve contains {len(jumps)} unexplained cash step(s) since")
        print(f"  the epoch, totalling {sum(unmatched_at.values()):+,.2f}. Rebasing the")
        print("  series so it is continuous - the arithmetic §48 performs on the")
        print("  stored curve:")
        print()
        print(f"      peak (rebased)  {peak_adj:,.2f}")
        print(f"      latest          {latest_adj:,.2f}")
        print(f"      drawdown        {dd_after:.2f}%   (vs {running:.2f}% as computed)")
        print()
        if dd_after < running - 0.5:
            print("  The reported drawdown is inflated by a balance change, not by")
            print("  losses. This is the §48 case: the peak is inherited from an")
            print("  account with a different starting balance, and the percentage")
            print("  compares two series that are not comparable.")
            print()
            print("  Clearing the kill switch WITHOUT rebasing the curve leaves the")
            print("  same trap armed - it will trip again on the next cycle, for the")
            print("  same non-reason. Run the cutover (B6 reset, B7 re-baseline)")
            print("  first; scripts/backfill_drawdown.py then recomputes the whole")
            print("  series from the clean curve.")
        else:
            print("  The steps do not explain the drawdown - it is at least as large")
            print("  measured from after them. Treat the halt as real.")
    else:
        print("  No unexplained steps since the epoch. The peak and the latest value")
        print("  belong to the same series, so the drawdown is REAL and the halt was")
        print("  correct. Do not clear the switch as an accounting fix - the account")
        print(f"  is genuinely {running:.2f}% below its high.")

    # ── the tail, for eyeballing ────────────────────────────────────────────
    print()
    print(BANNER)
    print(f"  LAST {min(args.days, len(in_epoch))} SAMPLES")
    print(BANNER)
    print(f"  {'timestamp':<26} {'total':>12} {'cash':>10} {'mkt':>10} "
          f"{'realized':>10} {'open':>5}")
    for r in in_epoch[-args.days:]:
        print(f"  {str(r['timestamp']):<26} {float(r['total_value'] or 0):>12,.2f} "
              f"{float(r['cash'] or 0):>10,.2f} {float(r['market_value'] or 0):>10,.2f} "
              f"{float(r['realized_pnl'] or 0):>10,.2f} {r['n_open'] or 0:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
