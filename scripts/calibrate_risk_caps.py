#!/usr/bin/env python3
"""Derive the drawdown caps from the equity curve instead of guessing them (§52).

    python3 scripts/calibrate_risk_caps.py
    python3 scripts/calibrate_risk_caps.py --percentile 95
    python3 scripts/calibrate_risk_caps.py --min-days 10

WHY THIS IS A SEPARATE SCRIPT FROM backfill_drawdown.py

backfill_drawdown.py WRITES daily_stats and prints the distribution as a side
effect. This one writes nothing and answers a different question: given the
distribution, what should the caps in config.yaml actually be? Keeping them
apart means the calibration can be re-run and argued with at any time without
touching the database, which matters because the answer is a judgement and
judgements get revisited.

RUN IT AFTER §48. The current curve spans a purse re-seed and the 2026-07-25
accounting incident. v1.3.1 exists because of what a mid-day re-seed does to
this arithmetic - a 1491 -> 1000 step reads as a 33% intraday drawdown. The
epoch guard keeps that out of the LIVE figures; this script recomputes from raw
curve points and deliberately does not apply it, so that a discontinuity shows
up as an implausible day you can see rather than a silently excluded one. If a
day here reads in the tens of percent, that is the accounting event, not a
trading loss, and the calibration is not ready.

WHAT IT WILL NOT DO

Recommend a number from a handful of days. Four observations do not have a 99th
percentile. Below --min-days it reports the distribution and says so rather than
producing a figure that looks derived.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile. No interpolation, deliberately: with a dozen
    observations, interpolating between two of them invents a precision the
    sample does not have. The nearest rank is always a day that actually
    happened."""
    if not sorted_values:
        return 0.0
    k = max(1, int(round(pct / 100.0 * len(sorted_values))))
    return sorted_values[min(k, len(sorted_values)) - 1]


def _daily_curves(rows: list[dict]) -> dict[str, list[float]]:
    """Equity points grouped by LOCAL day, matching update_drawdown()'s window
    exactly - the same local-day conversion backfill_drawdown.py uses, so the
    calibration and the enforced figure cannot disagree about what a day is."""
    offset = datetime.utcnow() - datetime.now()
    by_day: dict[str, list[float]] = {}
    for r in rows:
        tv = r.get("total_value")
        if tv is None:
            continue
        try:
            day = (datetime.fromisoformat(r["timestamp"]) - offset).date().isoformat()
        except (TypeError, ValueError):
            continue
        by_day.setdefault(day, []).append(float(tv))
    return by_day


def _intraday_drawdowns(by_day: dict[str, list[float]]) -> tuple[list[float], int]:
    """Worst peak-to-trough within each day. Days with a single point are
    counted separately, not charged as 0% - one sample is a level, not a
    curve, and "no drawdown" is a claim it cannot support."""
    observed, singles = [], 0
    for day in sorted(by_day):
        eq = by_day[day]
        if len(eq) < 2:
            singles += 1
            continue
        peak, dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak * 100)
        observed.append(dd)
    return observed, singles


def _running_drawdowns(rows: list[dict]) -> list[float]:
    """Distance from the all-time high at each point, across the whole curve.
    Unlike the intraday figure this is a current distance rather than a
    high-water mark, so it recovers on its own."""
    peak, out = 0.0, []
    for r in rows:
        tv = r.get("total_value")
        if tv is None:
            continue
        v = float(tv)
        peak = max(peak, v)
        if peak > 0:
            out.append((peak - v) / peak * 100)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--percentile", type=float, default=99.0,
                    help="percentile of observed drawdowns to set the cap at "
                         "(default 99 - the cap should be reachable but rare)")
    ap.add_argument("--min-days", type=int, default=10,
                    help="refuse to recommend a number below this many days")
    args = ap.parse_args()

    from config_loader import load_config_dict
    from storage.database import Database

    db = Database()
    cfg = load_config_dict()
    risk = cfg.get("risk", {}) or {}

    rows = db.get_paper_equity_history(limit=100000)
    if not rows:
        print("paper_equity_history is empty - nothing to calibrate against.")
        print("The caps currently in config.yaml are therefore unvalidated. "
              "That is not the same as wrong; it is unknown.")
        return 0

    by_day = _daily_curves(rows)
    intraday, singles = _intraday_drawdowns(by_day)
    running = _running_drawdowns(rows)

    print(f"{len(rows)} equity points across {len(by_day)} local day(s)"
          + (f", {singles} of them single-point and skipped" if singles else ""))

    # ── Sanity check for accounting discontinuities ─────────────────────────
    #
    # A trading loss of 10%+ in a day on this book size would be remarkable. A
    # purse re-seed producing one is routine. Say which is more likely rather
    # than feeding it into a percentile.
    implausible = [d for d in intraday if d >= 10.0]
    if implausible:
        print(f"\n⚠  {len(implausible)} day(s) show an intraday drawdown >= 10% "
              f"(worst {max(implausible):.1f}%).")
        print("   On this book size that is far more likely to be a purse re-seed or")
        print("   the 2026-07-25 accounting incident than a trading loss - a downward")
        print("   step in starting_cash reads as a drawdown. Finish §48 (reset and")
        print("   re-seed, then re-run backfill_drawdown.py) before trusting anything")
        print("   below.")

    # ── Intraday ────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("INTRADAY  (risk.max_intraday_drawdown_pct - blocks new entries today)")
    print("=" * 66)
    if intraday:
        s = sorted(intraday)
        for label, pct in (("median", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
            print(f"  {label:<8} {_percentile(s, pct):>8.3f}%")
        print(f"  {'worst':<8} {s[-1]:>8.3f}%   over {len(s)} day(s)")
        print(f"\n  configured: {float(risk.get('max_intraday_drawdown_pct', 0) or 0):.2f}%")
        if len(s) >= args.min_days:
            rec = _percentile(s, args.percentile)
            print(f"  RECOMMENDED: {rec:.2f}%  (p{args.percentile:g} of observed)")
            print("  A cap at a high percentile binds on the days that are genuinely")
            print("  unusual and stays out of the way on the rest - which is the only")
            print("  behaviour that makes it a control rather than documentation.")
        else:
            print(f"  NO RECOMMENDATION: {len(s)} day(s) < --min-days {args.min_days}.")
            print("  A percentile of four observations is arithmetic, not evidence.")
            print("  Keep the configured value, mark it provisional, and re-run this")
            print("  once the curve has a few weeks in it.")
    else:
        print("  No day had more than one equity point - nothing to measure.")

    # ── Running ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 66)
    print("RUNNING  (risk.max_running_drawdown_pct - trips the kill switch)")
    print("=" * 66)
    if running:
        s = sorted(running)
        for label, pct in (("median", 50), ("p95", 95), ("p99", 99)):
            print(f"  {label:<8} {_percentile(s, pct):>8.3f}%")
        print(f"  {'worst':<8} {s[-1]:>8.3f}%   over {len(s)} point(s)")
        print(f"\n  configured: {float(risk.get('max_running_drawdown_pct', 0) or 0):.2f}%")
        print("  This one is deliberately NOT set from a percentile. It answers")
        print("  'has the strategy stopped working', and the honest input is how much")
        print("  loss from the high you are willing to sit through before a human")
        print("  looks - not what has happened so far. The figures above tell you")
        print("  whether the configured value has ever been approached.")
    else:
        print("  No usable curve points.")

    # ── The interaction the caps have with each other ───────────────────────
    print("\n" + "=" * 66)
    print("INTERACTION  (read this before changing either number)")
    print("=" * 66)
    dd_cap = float(risk.get("max_intraday_drawdown_pct", 0) or 0)
    loss_pct = float(risk.get("max_daily_loss_pct", 0) or 0)
    print(f"  risk.max_daily_loss_pct         {loss_pct:.2f}%   (REALISED P&L)")
    print(f"  risk.max_intraday_drawdown_pct  {dd_cap:.2f}%   (peak-to-trough, incl. unrealised)")
    if loss_pct and dd_cap and dd_cap <= loss_pct:
        print("\n  ⚠  These are the same number, or the drawdown cap is tighter.")
        print("  Intraday peak-to-trough includes unrealised P&L, so for any given")
        print("  session it is always >= the realised loss. Set this way, the")
        print("  drawdown gate fires first in essentially every scenario and the")
        print("  realised daily-loss limit - the one §8 was written to give a real")
        print("  input, and the ONLY one that escalates to the kill switch - becomes")
        print("  close to unreachable.")
        print("\n  That is a design decision either way. Just make it deliberately:")
        print("  either widen the drawdown cap so the realised limit can be reached,")
        print("  or accept that the daily-loss limit is now effectively dead and stop")
        print("  describing it as the primary control.")

    # ── Scale ───────────────────────────────────────────────────────────────
    try:
        acct = db.get_paper_account() or {}
        deployed = sum(float(p.get("dollar_amount") or 0)
                       for p in db.get_all_positions(simulated=True))
        equity = float(acct.get("cash", 0) or 0) + deployed
    except Exception:
        equity = 0.0
    if equity > 0 and dd_cap:
        size = float((cfg.get("trading", {}) or {}).get("trade_size_usd", 0) or 0)
        print("\n" + "=" * 66)
        print("SCALE  (what the cap is worth in dollars right now)")
        print("=" * 66)
        print(f"  equity at cost      ${equity:,.2f}")
        print(f"  {dd_cap:.2f}% of it        ${equity * dd_cap / 100:,.2f}")
        if size:
            print(f"  one position        ${size:,.2f}")
            print(f"\n  The cap is worth {equity * dd_cap / 100 / size:.1f} position(s) of "
                  f"adverse movement.")
        print("  Both caps are percentages, so they scale with the book automatically -")
        print("  but at this size the dollar figure is small enough that ordinary")
        print("  intraday noise on one holding can approach it. Record in the README")
        print("  that these become materially binding as equity grows; a reader")
        print("  should not have to infer that from the percentage alone.")

    print("\nNothing was written. Edit config.yaml yourself, and leave a comment")
    print("on each cap saying the date, the sample size and the percentile used -")
    print("a calibrated number that does not say what it was calibrated from")
    print("becomes a guess again the moment you stop remembering.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
