#!/usr/bin/env python3
"""§40 - run the same backtest in two versions and say whether they agree.

DEFERRED SINCE v1.3.0, and it is the piece everything downstream leans on.
Phase 3's exit criterion is "build two images from two tags, run the same
backtest window in both, confirm the shared code paths produce identical
numbers". Phase 4's entire justification is a measured before-and-after. Both
sentences describe THIS script, and until it existed both were assertions.

WHAT "AGREE" MEANS, AND WHY IT IS TWO DIFFERENT QUESTIONS

The comparison is not one test, it is two, and which one applies is decided by
the config fingerprint - the same hash learning/pattern_database.py stamps on
every recorded pattern, covering only values that alter a decision.

  SAME fingerprint    The two versions should produce IDENTICAL numbers.
                      Any divergence is a reproducibility defect: unpinned
                      numeric libraries, a different pandas, an indicator
                      computed by ta_fallback.py in one and pandas_ta in the
                      other (§13). This is the Phase 3 exit criterion, and
                      here a difference is a FAILURE.

  DIFFERENT fingerprint
                      The decision function moved, so the numbers are SUPPOSED
                      to differ, and the interesting output is the size and
                      shape of the difference. This is the Phase 4 measurement,
                      and here a difference is the RESULT, not a fault. What
                      would be a fault is no difference at all - a
                      recalibration that changes nothing measurable has not
                      been demonstrated to do anything.

Conflating those two is how a reproducibility bug gets filed as "expected,
we changed the scoring" and how a no-op recalibration gets declared a success.

TRADE-LEVEL, NOT JUST SUMMARY-LEVEL

Two runs can agree on n_trades, win_rate and profit factor while disagreeing
about which trades those were. The summary is a lossy projection; agreement
there is necessary and nowhere near sufficient. So the trades are compared as
a set keyed on (ticker, entry_date), and the report names what only A took,
what only B took, and what both took at different prices.

USAGE

    # via tp (each version already installed, its own venv and database)
    scripts/compare_versions.py v2.1.0 v2.2.0 --tickers AAPL MSFT NVDA \\
        --start 2025-01-01 --end 2025-06-30

    # against results.json files produced any other way - the two containers
    # of §42.3, for instance, which is the Phase 3 exit criterion verbatim
    scripts/compare_versions.py --results-a a/results.json --results-b b/results.json

Exit code is 0 when the verdict is what the fingerprints predict, 1 when it is
not - so this is usable as a gate, which is the point of writing it as a script
rather than as a paragraph in a runbook.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Summary metrics compared one by one. Ordered as a person reads them, not
# alphabetically. exit_reason_counts is handled separately - it is a dict.
METRICS = [
    ("n_trades", "trades taken"),
    ("win_rate", "win rate %"),
    ("avg_outcome_pct", "avg outcome %"),
    ("avg_win_pct", "avg win %"),
    ("avg_loss_pct", "avg loss %"),
    ("profit_factor", "profit factor"),
    ("avg_hold_days", "avg hold days"),
]


# ---------------------------------------------------------------------------
#  Running a backtest inside a version
# ---------------------------------------------------------------------------
def run_backtest_in(tag: str, args, out_dir: Path) -> Path:
    """`tp run <tag> --backtest`, which supplies that version's venv, its own
    database and TP_FORCE_PAPER. Deliberately NOT `python3 run_backtest.py`
    against the working tree: the whole question is what the TAGGED code does,
    and a working-tree run answers a different one."""
    cmd = [
        str(REPO / "scripts" / "tp"), "run", tag, "--backtest",
        "--out-dir", str(out_dir), "--no-db",
        "--tickers", *args.tickers,
        "--warmup-days", str(args.warmup_days),
        "--max-hold-days", str(args.max_hold_days),
    ]
    if args.start:
        cmd += ["--start", args.start]
    if args.end:
        cmd += ["--end", args.end]

    print(f"  running backtest in {tag} ...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    results = out_dir / "results.json"
    if not results.exists():
        sys.stderr.write(r.stdout[-3000:] + "\n" + r.stderr[-3000:] + "\n")
        sys.exit(f"backtest in {tag} produced no results.json (exit {r.returncode})")
    return results


# ---------------------------------------------------------------------------
#  Comparison
# ---------------------------------------------------------------------------
def fingerprint_of(result: dict) -> str | None:
    """The fingerprint the run recorded, if it recorded one. Falls back to
    hashing the decision-relevant config the result carries, so a comparison
    against an older results.json still classifies correctly rather than
    silently taking the SAME-fingerprint branch by default."""
    cfg = result.get("config") or {}
    fp = cfg.get("config_fingerprint")
    if fp:
        return fp
    try:
        from learning.pattern_database import config_fingerprint

        return config_fingerprint(cfg)
    except Exception:
        return None


def trade_key(t: dict):
    return (t.get("ticker"), str(t.get("entry_date") or t.get("entry_ts") or ""))


def compare_trades(a: list, b: list) -> dict:
    ka = {trade_key(t): t for t in a}
    kb = {trade_key(t): t for t in b}
    only_a = sorted(set(ka) - set(kb))
    only_b = sorted(set(kb) - set(ka))
    shared_differing = []
    for k in sorted(set(ka) & set(kb)):
        ta, tb = ka[k], kb[k]
        diffs = {
            f: (ta.get(f), tb.get(f))
            for f in ("entry_price", "exit_price", "outcome_pct",
                      "exit_reason", "hold_days", "shares")
            if f in ta or f in tb
            if ta.get(f) != tb.get(f)
        }
        if diffs:
            shared_differing.append((k, diffs))
    return {"only_a": only_a, "only_b": only_b,
            "shared_differing": shared_differing,
            "n_shared": len(set(ka) & set(kb))}


def render(label_a, label_b, ra, rb) -> bool:
    """Prints the report. Returns True when the outcome matches what the
    fingerprints predict."""
    bar = "=" * 74
    fa, fb = fingerprint_of(ra), fingerprint_of(rb)
    same_decision = fa is not None and fa == fb

    print(bar)
    print(f"  A  {label_a}")
    print(f"  B  {label_b}")
    print(bar)
    print(f"  config_fingerprint A: {fa or 'unknown'}")
    print(f"  config_fingerprint B: {fb or 'unknown'}")
    if fa is None or fb is None:
        print("  WARNING: at least one run recorded no fingerprint. Treating this")
        print("           as a decision-function CHANGE, which is the conservative")
        print("           reading - it means divergence will not be reported as a")
        print("           reproducibility failure when it might be one.")
    print(f"  => {'SAME decision function' if same_decision else 'DIFFERENT decision function'}")
    print()

    sa = ra.get("summary") or {}
    sb = rb.get("summary") or {}

    print("  " + "-" * 70)
    print(f"  {'metric':<20} {'A':>14} {'B':>14}   {'':<12}")
    print("  " + "-" * 70)
    n_diff = 0
    for key, label in METRICS:
        va, vb = sa.get(key), sb.get(key)
        differs = va != vb
        n_diff += differs
        mark = "" if not differs else "  <-- differs"
        print(f"  {label:<20} {str(va):>14} {str(vb):>14}{mark}")

    va, vb = ra.get("n_scored"), rb.get("n_scored")
    if va != vb:
        n_diff += 1
    print(f"  {'tickers scored':<20} {str(va):>14} {str(vb):>14}"
          f"{'' if va == vb else '  <-- differs'}")

    # Veto counts: a change here explains a change in n_trades, so it is the
    # first place to look when the trade count moved.
    vca = ra.get("veto_counts") or {}
    vcb = rb.get("veto_counts") or {}
    veto_keys = sorted(set(vca) | set(vcb))
    veto_diffs = [(k, vca.get(k, 0), vcb.get(k, 0))
                  for k in veto_keys if vca.get(k, 0) != vcb.get(k, 0)]
    if veto_diffs:
        print()
        print("  vetoes that fired a different number of times:")
        for k, x, y in veto_diffs:
            print(f"    {k:<40} {x:>6} -> {y:>6}")
        n_diff += 1

    td = compare_trades(ra.get("trades") or [], rb.get("trades") or [])
    print()
    print("  " + "-" * 70)
    print("  trade-level (a summary can agree while the trades do not)")
    print("  " + "-" * 70)
    print(f"    taken by both:        {td['n_shared']}")
    print(f"    only A:               {len(td['only_a'])}")
    print(f"    only B:               {len(td['only_b'])}")
    print(f"    same trade, different numbers: {len(td['shared_differing'])}")
    for k in td["only_a"][:10]:
        print(f"      only A: {k[0]} {k[1]}")
    for k in td["only_b"][:10]:
        print(f"      only B: {k[0]} {k[1]}")
    for k, diffs in td["shared_differing"][:10]:
        detail = ", ".join(f"{f}: {x} -> {y}" for f, (x, y) in diffs.items())
        print(f"      {k[0]} {k[1]}: {detail}")
    trades_differ = bool(td["only_a"] or td["only_b"] or td["shared_differing"])

    identical = (n_diff == 0) and not trades_differ

    print()
    print(bar)
    if same_decision and identical:
        print("  VERDICT: IDENTICAL, and the decision function did not change.")
        print("  This is the Phase 3 exit criterion met: same inputs, same code")
        print("  paths, same numbers. The pinning holds.")
        ok = True
    elif same_decision and not identical:
        print("  VERDICT: REPRODUCIBILITY FAILURE.")
        print("  The fingerprints match, so these two runs were supposed to be")
        print("  the same computation - and they were not. Suspect an unpinned")
        print("  numeric library, a pandas/numpy version difference, or one side")
        print("  falling back to ta_fallback.py (§13). Check both runs'")
        print("  ta_backend before looking anywhere else.")
        ok = False
    elif not same_decision and identical:
        print("  VERDICT: THE CHANGE HAD NO MEASURABLE EFFECT.")
        print("  The fingerprints differ, so the decision function moved - and")
        print("  not one trade, veto or score changed on this window. Either the")
        print("  window does not exercise what changed, or the change is inert.")
        print("  Do not report a recalibration as validated on this basis.")
        ok = False
    else:
        print("  VERDICT: MEASURED DIFFERENCE, as expected.")
        print("  The decision function changed and the numbers moved with it.")
        print("  This is the before-and-after Phase 4 is justified by. Note that")
        print("  trades either side of this boundary must NOT be pooled (§35).")
        ok = True
    print(bar)
    return ok


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tags", nargs="*", metavar="TAG",
                   help="two installed tags, e.g. v2.1.0 v2.2.0")
    p.add_argument("--results-a", help="an existing results.json instead of running A")
    p.add_argument("--results-b", help="an existing results.json instead of running B")
    p.add_argument("--tickers", nargs="+",
                   default=["AAPL", "MSFT", "NVDA", "AMD", "GOOGL"])
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--warmup-days", type=int, default=260)
    p.add_argument("--max-hold-days", type=int, default=20)
    p.add_argument("--keep", action="store_true",
                   help="keep the backtest output directories")
    args = p.parse_args()

    if args.results_a and args.results_b:
        ra = json.loads(Path(args.results_a).read_text())
        rb = json.loads(Path(args.results_b).read_text())
        return 0 if render(args.results_a, args.results_b, ra, rb) else 1

    if len(args.tags) != 2:
        p.error("give two tags, or --results-a and --results-b")

    tag_a, tag_b = args.tags
    if tag_a == tag_b:
        p.error("comparing a version with itself proves only that it is deterministic")

    tmp = Path(tempfile.mkdtemp(prefix="tp-compare-"))
    try:
        pa = run_backtest_in(tag_a, args, tmp / tag_a)
        pb = run_backtest_in(tag_b, args, tmp / tag_b)
        ra = json.loads(pa.read_text())
        rb = json.loads(pb.read_text())
        print()
        ok = render(tag_a, tag_b, ra, rb)
        if args.keep:
            print(f"\n  output kept in {tmp}")
        return 0 if ok else 1
    finally:
        if not args.keep:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
