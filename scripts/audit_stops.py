#!/usr/bin/env python3
"""Audit open positions for impossible or missing stops (§5, Phase 1).

The 2026-07-24 evaluation found SMFL holding a stop exactly equal to its entry
price ($614.2501 / $614.2501). A zero-distance stop is either an instant exit
or a silently ignored one, and neither is a stop. Treat it as a bug in the
stop machine's handling of a missing ATR, not as a data quirk.

DELIBERATE DEVIATION from the plan's audit SQL, which flags every row where
current_stop_price >= entry_price: that predicate also flags every HEALTHY
position whose stop has legitimately advanced past entry - which is the entire
purpose of engine/stop_state_machine.py's BREAKEVEN, PROFIT_PROTECT and
TREND_FOLLOWING states. Flagging those as defects would train you to ignore
this report, which is worse than not having it. So the classification below
separates the three real defects from the one benign case:

  ZERO_DISTANCE   stop == entry exactly. Always a bug - see SMFL above.
  NO_STOP         stop is NULL or <= 0 on a managed position. Always a bug.
  STILL_ARMED     a quarantined SYNC/SEED row that still carries a live stop,
                  i.e. migrations/002 has not been applied.
  STATE_REGRESSED stop is above entry while stop_state reads INITIAL_RISK or
                  TRADE_CONFIRMING. A WARNING, not a defect - see below.
  advanced        stop above entry in a later state. Context only.

On STATE_REGRESSED (found on AES, 2026-07-24). The stop itself is correct: it
was set by the BREAKEVEN stage as entry + risk_per_share x breakeven_lock_r,
and should_advance() then correctly refused to widen it when price fell back.
What is wrong is the LABEL. calc_stop() re-derives its state from the CURRENT
profit_r every cycle with no ratchet, so the moment price drops back below
breakeven_r the state reverts to INITIAL_RISK while the price stays locked at
breakeven. State and price then describe different things.

The only behavioural consumer of stop_state is rules/sell_rules.py's
stop_urgency, which is display-only (engine/packet_builder.py prints it;
nothing executes on it), so this misreports rather than mistrades. It is
reported and not counted as a defect for that reason - fixing the ratchet
means editing engine/stop_state_machine.py, which is a decision-function
change and cannot ship in a patch release.

Read-only. Exits 1 only on a real defect, so it can gate CI or a pre-push
hook. Usage:  python3 scripts/audit_stops.py [--all]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database, is_unmanaged_mode  # noqa: E402

EARLY_STATES = {"INITIAL_RISK", "TRADE_CONFIRMING", ""}


def classify(pos: dict) -> str | None:
    entry = pos.get("entry_price") or 0
    stop = pos.get("current_stop_price")
    state = str(pos.get("stop_state") or "").upper()

    if stop is None or float(stop) <= 0:
        return "NO_STOP"
    stop = float(stop)
    if entry and stop == float(entry):
        return "ZERO_DISTANCE"
    if entry and stop >= float(entry):
        return "STATE_REGRESSED" if state in EARLY_STATES else "advanced"
    return None


def main() -> int:
    show_all = "--all" in sys.argv
    db = Database()
    rows = db.get_all_positions()

    defects, warnings, context, unmanaged = [], [], [], []
    for p in rows:
        if is_unmanaged_mode(p.get("trade_mode")):
            # Quarantined by §5 - migrations/002 NULLed their stops on
            # purpose, so "NO_STOP" here is the desired state, not a defect.
            unmanaged.append(p)
            continue
        verdict = classify(p)
        if verdict is None:
            continue
        if verdict == "advanced":
            context.append((verdict, p))
        elif verdict == "STATE_REGRESSED":
            warnings.append((verdict, p))
        else:
            defects.append((verdict, p))

    def line(verdict, p):
        stop = p.get("current_stop_price")
        return (f"  {verdict:<18} {p['ticker']:<6} "
                f"entry={float(p.get('entry_price') or 0):.4f} "
                f"stop={'NULL' if stop is None else format(float(stop), '.4f'):<12} "
                f"state={p.get('stop_state') or '-':<16} "
                f"mode={p.get('trade_mode') or '-':<6} "
                f"book={'paper' if p.get('simulated') else 'real'}")

    print(f"Open positions: {len(rows)}  "
          f"(managed {len(rows) - len(unmanaged)}, quarantined {len(unmanaged)})")

    if unmanaged:
        print(f"\nQuarantined (SYNC/SEED - §5, not this engine's to exit): "
              f"{', '.join(sorted(p['ticker'] for p in unmanaged))}")
        still_armed = [p for p in unmanaged if (p.get("current_stop_price") or 0) > 0]
        if still_armed:
            print("  WARNING: these quarantined rows STILL carry a live stop - "
                  "migrations/002 has not been applied:")
            for p in still_armed:
                print(line("STILL_ARMED", p))
            defects.extend(("STILL_ARMED", p) for p in still_armed)

    if context and show_all:
        print("\nStops advanced past entry (expected in BREAKEVEN/PROFIT_PROTECT/"
              "TREND_FOLLOWING - context only):")
        for verdict, p in context:
            print(line(verdict, p))

    if warnings:
        print(f"\nWARNINGS ({len(warnings)}) - stop price is correct, state label is stale:")
        for verdict, p in warnings:
            print(line(verdict, p))
        print("  The stop was locked by the BREAKEVEN stage and correctly never widened;")
        print("  calc_stop() then re-derived an early state from the CURRENT profit_r.")
        print("  Display-only impact (rules/sell_rules.py stop_urgency). The ratchet fix")
        print("  touches engine/stop_state_machine.py = a decision-function change.")

    if defects:
        print(f"\nDEFECTS ({len(defects)}):")
        for verdict, p in defects:
            print(line(verdict, p))
        return 1

    print("\nok: no impossible or missing stops on managed positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
