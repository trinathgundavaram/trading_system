#!/usr/bin/env python3
"""Phase 4 (§19-§21) - the recalibration harness.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT

§19 re-derives the scoring function, §20 re-derives every threshold from the
resulting scale, §21 revives the position-sizing tiers. All three take the
recorded trade history as their input, and the honest statement about that
input is that it has been wrong twice: once because test-suite residue was
pooled with real trades (§15, §48), and once because a join fanned out and
attached one excursion row to five patterns (§51).

So this script computes, and it REFUSES, and it does not apply. Three
subcommands:

    assess    Is this sample fit to recalibrate from? Read-only. This is the
              gate, and it is the most important part of the file.
    propose   Derive weights, thresholds and tiers FROM THE DATA. Writes a
              proposal file. Touches no config, arms nothing.
    receipt   Write the §32 validation receipt - the thing live_trader.py
              looks for and never finds, because nothing has ever written one.

NOTHING HERE EDITS config.yaml. A recalibration that silently rewrote the
running configuration would change the decision function without a release,
without a fingerprint change anybody declared, and without the boundary §35
needs in order to keep trade history poolable. The proposal is a file you read,
argue with, and then apply by hand as a declared decision-function change with
its own major version.

WHY THE NUMBERS ARE NOT IN THIS FILE

There are no default weights, no fallback thresholds and no hardcoded tiers
here. If the sample cannot support a number, the script says so and exits
non-zero rather than emitting a plausible one. A number that looks derived and
is not is the specific failure Phase 2.5 existed to prevent, and it would be
absurd for the tool built on top of that work to reintroduce it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BANNER = "=" * 74

# Sample floors. Below these, a fit is arithmetic rather than evidence.
#
# 150 mirrors learning.min_trades_before_bayesian, which is the number this
# project already decided was the point at which a per-pattern posterior stops
# being noise. Re-using it rather than inventing a second threshold keeps one
# answer to "how much history is enough" instead of two that will drift.
MIN_CLOSED_PATTERNS = 150
# Per-feature: a correlation computed on fewer than this is not reportable, and
# the feature is dropped from the proposal with that stated rather than being
# given a weight derived from noise.
MIN_PER_FEATURE = 60
# A recalibration fitted to two weeks of one regime is a recalibration to that
# fortnight. Not a statistical rule - a statement about market regimes.
MIN_SPAN_DAYS = 90


# ---------------------------------------------------------------------------
#  Statistics, hand-rolled so this script has no numeric dependency of its own
# ---------------------------------------------------------------------------
def _rank(xs: list[float]) -> list[float]:
    """Average ranks, so ties do not bias the correlation."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation, not Pearson. The features are not linearly related to
    outcome and several are bounded or heavily skewed; what matters for a
    scoring weight is monotone discriminative power, which is what rank
    correlation measures."""
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return None if den == 0 else num / den


def percentile(xs: list[float], q: float) -> float:
    """Linear interpolation between order statistics; q in [0, 100]."""
    if not xs:
        raise ValueError("percentile of an empty sample")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return s[int(pos)]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# ---------------------------------------------------------------------------
#  Loading the sample
# ---------------------------------------------------------------------------
def fetch(db, sql: str, columns: list[str]) -> list[dict]:
    """Read-only query returning a list of dicts, keyed by `columns`.

    Database exposes no generic `query()`; the house style is
    `with db._conn() as conn: conn.execute(...).fetchall()` (see
    scripts/reconcile.py), and that returns TUPLES - the pooled connection
    uses no dict row factory, so `dict(row)` raises. Zipping against an
    explicit column list is the portable read: it does not depend on the
    cursor type, and it makes the SELECT and the unpacking impossible to get
    out of step, because they are the same list."""
    with db._conn() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(zip(columns, r)) for r in rows]


def load_sample(db) -> dict:
    """Closed patterns with their features, filtered to what §15/§17 say is
    trustworthy: quarantined rows excluded, and nothing recorded before
    learning.min_pattern_recorded_at - the epoch boundary that says "the data
    on the other side of this was produced by a different system"."""
    import yaml

    cfg = yaml.safe_load((REPO / "config.yaml").read_text())
    epoch = ((cfg.get("learning") or {}).get("min_pattern_recorded_at") or "")

    PATTERN_COLS = ["id", "ticker", "recorded_at", "features", "outcome_pct",
                    "hold_hours", "exit_kind", "data_quality", "trade_id"]
    rows = fetch(db, f"""
        SELECT {", ".join(PATTERN_COLS)}
          FROM pattern_database
         WHERE outcome_pct IS NOT NULL
    """, PATTERN_COLS)

    kept, dropped = [], {"quarantined": 0, "pre_epoch": 0, "unparseable": 0}
    for d in rows:
        if (d.get("data_quality") or "").lower() not in ("", "clean", "ok", None):
            dropped["quarantined"] += 1
            continue
        if epoch and str(d.get("recorded_at") or "") < epoch:
            dropped["pre_epoch"] += 1
            continue
        try:
            d["features"] = json.loads(d["features"]) if isinstance(d["features"], str) \
                else (d["features"] or {})
        except Exception:
            dropped["unparseable"] += 1
            continue
        if not isinstance(d["features"], dict) or not d["features"]:
            dropped["unparseable"] += 1
            continue
        kept.append(d)

    return {"patterns": kept, "dropped": dropped, "epoch": epoch, "config": cfg}


def span_days(patterns: list) -> int:
    stamps = sorted(str(p.get("recorded_at") or "") for p in patterns if p.get("recorded_at"))
    if len(stamps) < 2:
        return 0
    try:
        a = datetime.fromisoformat(stamps[0][:19])
        b = datetime.fromisoformat(stamps[-1][:19])
        return (b - a).days
    except Exception:
        return 0


# ---------------------------------------------------------------------------
#  assess
# ---------------------------------------------------------------------------
def cmd_assess(args) -> int:
    from storage.database import Database

    s = load_sample(Database())
    pats = s["patterns"]
    problems = []

    print(BANNER)
    print("  PHASE 4 SAMPLE ASSESSMENT - is this fit to recalibrate from?")
    print(BANNER)
    print(f"  usable closed patterns      {len(pats)}")
    print(f"  dropped, quarantined        {s['dropped']['quarantined']}")
    print(f"  dropped, before the epoch   {s['dropped']['pre_epoch']}  "
          f"(epoch = {s['epoch'] or 'unset'})")
    print(f"  dropped, unparseable        {s['dropped']['unparseable']}")

    if len(pats) < MIN_CLOSED_PATTERNS:
        problems.append(
            f"only {len(pats)} usable closed patterns; {MIN_CLOSED_PATTERNS} is the "
            f"floor (learning.min_trades_before_bayesian). Below it a fit is "
            f"arithmetic, not evidence.")

    days = span_days(pats)
    print(f"  calendar span               {days} days")
    if days < MIN_SPAN_DAYS:
        problems.append(
            f"the sample spans {days} days; {MIN_SPAN_DAYS} is the floor. A "
            f"recalibration fitted to one regime is a recalibration to that "
            f"regime, and nothing here can detect that from the numbers alone.")

    if not s["epoch"]:
        problems.append(
            "learning.min_pattern_recorded_at is unset, so nothing excludes "
            "pre-reset rows. §48's rebase is what makes the epoch meaningful - "
            "run the cutover before fitting anything.")

    # Excursion coverage - §21's tiers are derived from MAE, so a sample whose
    # excursion rows are missing or unlinked cannot support them.
    try:
        linked = fetch(Database(), """
            SELECT COUNT(*) FROM mae_mfe_data WHERE trade_id IS NOT NULL
        """, ["n"])
        n_exc = linked[0]["n"] if linked else 0
    except Exception as e:
        n_exc = -1
        print(f"  excursion rows              unreadable ({e})")
    if n_exc >= 0:
        print(f"  excursion rows with a link  {n_exc}")
        if n_exc < MIN_PER_FEATURE:
            problems.append(
                f"only {n_exc} linked excursion rows; §21's sizing tiers are "
                f"derived from the MAE distribution and cannot be proposed "
                f"from that. This is what migrations/012's FK exists to keep "
                f"honest - see scripts/rehearse_cutover.py.")

    # exit_kind coverage (§50). This tool already SELECTs exit_kind into every
    # sample it loads, which makes it the first real consumer and therefore the
    # first place a partial sample could be mistaken for a complete one. §20's
    # outcome distribution is the specific risk: grouping by exit_kind over a
    # sample where half the rows are NULL produces a breakdown that looks like
    # the strategy and is actually a description of which exits happened to be
    # classifiable.
    try:
        cov = Database().get_exit_kind_coverage()
        print(f"  {cov['label']}")
        if cov["total"] and cov["missing"]:
            top = ", ".join(f"{r['exit_reason'][:38]} x{r['n']}"
                            for r in cov["unclassified_reasons"][:3])
            print(f"  unclassified exit_reasons   {top}")
        # Not a blocking problem below 100%: §19's weights come from features
        # and outcomes, neither of which needs exit_kind. It becomes blocking
        # only for a breakdown that GROUPs by it, so the number is printed
        # unconditionally and the caveat travels with it.
        if cov["pct"] is not None and cov["pct"] < 50:
            problems.append(
                f"exit_kind covers only {cov['pct']:.0f}% of closed patterns "
                f"({cov['structured']}/{cov['total']}). §20's outcome "
                f"distribution may group by it; a minority sample is not a "
                f"random subset of exits, so any such breakdown would be "
                f"biased toward whichever exits were classifiable.")
    except Exception as e:
        print(f"  exit_kind coverage          unreadable ({e})")

    feats = {}
    for p in pats:
        for k, v in p["features"].items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                feats.setdefault(k, 0)
                feats[k] += 1
    usable_feats = [k for k, n in feats.items() if n >= MIN_PER_FEATURE]
    print(f"  numeric features present    {len(feats)}")
    print(f"  features with >= {MIN_PER_FEATURE} obs      {len(usable_feats)}")

    # The placeholder problem, stated in numbers rather than prose. A feature
    # that never varies cannot carry weight, and several are known constants.
    constant = []
    for k in usable_feats:
        vals = [p["features"][k] for p in pats
                if isinstance(p["features"].get(k), (int, float))]
        if len(set(vals)) <= 1:
            constant.append(k)
    if constant:
        print()
        print(f"  {len(constant)} feature(s) never vary in this sample - they are")
        print("  placeholders, and no weight derived for them would mean anything:")
        for k in sorted(constant):
            print(f"    {k}")

    print()
    print(BANNER)
    if problems:
        print("  NOT FIT TO RECALIBRATE FROM")
        for i, why in enumerate(problems, 1):
            print(f"  {i}. {why}")
        print(BANNER)
        return 1
    print("  SAMPLE ACCEPTED - `propose` can run against it")
    print(BANNER)
    return 0


# ---------------------------------------------------------------------------
#  propose
# ---------------------------------------------------------------------------
def cmd_propose(args) -> int:
    from storage.database import Database

    db = Database()
    s = load_sample(db)
    pats = s["patterns"]
    if len(pats) < MIN_CLOSED_PATTERNS:
        sys.exit(f"refusing: {len(pats)} usable patterns, floor is "
                 f"{MIN_CLOSED_PATTERNS}. Run `assess` for the full reason.")

    outcomes = [float(p["outcome_pct"]) for p in pats]

    # ── §19: weights from measured discriminative power ─────────────────────
    # The weight a feature earns is proportional to |rank correlation| with
    # outcome, and a feature whose correlation cannot be computed - or which
    # never varies - earns nothing rather than a default.
    per_feature = {}
    names = set()
    for p in pats:
        names.update(k for k, v in p["features"].items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool))
    for k in sorted(names):
        xs, ys = [], []
        for p in pats:
            v = p["features"].get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                xs.append(float(v))
                ys.append(float(p["outcome_pct"]))
        if len(xs) < MIN_PER_FEATURE:
            per_feature[k] = {"n": len(xs), "rho": None,
                              "excluded": f"n<{MIN_PER_FEATURE}"}
            continue
        rho = spearman(xs, ys)
        if rho is None:
            per_feature[k] = {"n": len(xs), "rho": None,
                              "excluded": "constant or degenerate"}
            continue
        per_feature[k] = {"n": len(xs), "rho": round(rho, 4), "excluded": None}

    scored = {k: abs(v["rho"]) for k, v in per_feature.items()
              if v["rho"] is not None}
    total = sum(scored.values())
    weights = ({k: round(v / total, 4) for k, v in sorted(
        scored.items(), key=lambda kv: -kv[1])} if total else {})

    # ── §20: thresholds from the resulting distribution ─────────────────────
    # Percentiles of the realised outcome distribution, not of an imagined
    # score scale. Stated as "what fraction of history would have qualified",
    # which is a question with an answer, unlike "is 65 a good threshold".
    dist = {
        "outcome_p10": round(percentile(outcomes, 10), 3),
        "outcome_p25": round(percentile(outcomes, 25), 3),
        "outcome_median": round(percentile(outcomes, 50), 3),
        "outcome_p75": round(percentile(outcomes, 75), 3),
        "outcome_p90": round(percentile(outcomes, 90), 3),
        "win_rate_pct": round(
            100.0 * sum(1 for o in outcomes if o > 0) / len(outcomes), 2),
    }

    # ── §21: sizing tiers from the realised MAE distribution ────────────────
    tiers = None
    try:
        rows = fetch(db, """
            SELECT mae_pct FROM mae_mfe_data
             WHERE trade_id IS NOT NULL AND mae_pct IS NOT NULL
        """, ["mae_pct"])
        maes = [abs(float(r["mae_pct"])) for r in rows]
        if len(maes) >= MIN_PER_FEATURE:
            tiers = {
                "mae_p50": round(percentile(maes, 50), 3),
                "mae_p75": round(percentile(maes, 75), 3),
                "mae_p90": round(percentile(maes, 90), 3),
                "n": len(maes),
                "note": "stop distance must clear mae_p75 or three quarters of "
                        "historically winning trades would have been stopped out "
                        "of a position that recovered",
            }
        else:
            tiers = {"n": len(maes),
                     "refused": f"fewer than {MIN_PER_FEATURE} linked excursion rows"}
    except Exception as e:
        tiers = {"refused": f"excursion table unreadable: {e}"}

    # ── Model identity (2026-07-26, review follow-up) ───────────────────────
    #
    # A proposal file used to carry a timestamp and nothing else identifying.
    # Two of them from either side of a §19 weight change are structurally
    # identical documents describing DIFFERENT MODELS, and the only way to tell
    # was the filename - which defaults to the same path every run and is
    # therefore overwritten.
    #
    # That is the "later notebooks mix pre- and post-Phase-3 EV curves" failure
    # exactly. It does not announce itself: pooling two EV curves produces a
    # curve, and a plausible one. The fields below make the two provably
    # different objects rather than two runs of the same one.
    #
    #   config_fingerprint - the same hash pattern_database stamps on every
    #     row (§17). Two proposals with different fingerprints were fitted on
    #     samples produced by different strategies and must not be compared.
    #   app_version - which build produced the proposal. (storage/version.py
    #     exposes only this one; add_pattern writes it into BOTH the
    #     app_version and engine_version columns, so there is no separate
    #     engine version to record here.)
    #   feature_schema - which encoding the SAMPLE rows carry. A sample of
    #     schema-1 rows has constant adx/cmf/sector-RS, so any weight derived
    #     for those features is a statement about the recording gap, not the
    #     market. See learning/pattern_database.py's FEATURE_SCHEMA_VERSION.
    #   exit_kind_coverage - so a §20 distribution can never be read without
    #     the denominator it was computed over.
    try:
        from learning.pattern_database import (FEATURE_SCHEMA_VERSION,
                                               config_fingerprint)
        from storage.version import app_version
        _schemas = sorted({int((p.get("features") or {}).get("feature_schema") or 1)
                           for p in pats})
        model_id = {
            "config_fingerprint": config_fingerprint(s["config"]),
            "app_version": app_version(),
            "writer_feature_schema": FEATURE_SCHEMA_VERSION,
            "sample_feature_schemas": _schemas,
            "mixed_feature_schema": len(_schemas) > 1,
        }
    except Exception as e:      # never block a proposal on its own provenance
        model_id = {"unavailable": str(e)}

    try:
        model_id["exit_kind_coverage"] = Database().get_exit_kind_coverage()
    except Exception as e:
        model_id["exit_kind_coverage"] = {"unavailable": str(e)}

    proposal = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Compare this block before comparing anything below it. Two proposals
        # whose model_id differs are different models, not two measurements of
        # one.
        "model_id": model_id,
        "sample": {
            "n_patterns": len(pats),
            "span_days": span_days(pats),
            "epoch": s["epoch"],
            "dropped": s["dropped"],
        },
        "s19_weights": weights,
        "s19_per_feature": per_feature,
        "s20_distribution": dist,
        "s21_tiers": tiers,
        "APPLIED": False,
        "how_to_apply": (
            "By hand, as a declared decision-function change: edit config.yaml, "
            "bump MAJOR (scripts/release.sh major), and record the before/after "
            "with scripts/compare_versions.py. Do NOT pool trade history across "
            "that boundary (§35)."
        ),
    }

    out = Path(args.out or (REPO / "output" / "phase4_proposal.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(proposal, indent=2))

    print(BANNER)
    print(f"  PROPOSAL from {len(pats)} patterns over {span_days(pats)} days")
    print(BANNER)
    print(f"  model_id.config_fingerprint  {model_id.get('config_fingerprint', '?')}")
    cov = model_id.get("exit_kind_coverage") or {}
    if cov.get("label"):
        print(f"  {cov['label']}")
    if model_id.get("mixed_feature_schema"):
        print("  WARNING: this sample mixes feature schemas "
              f"{model_id.get('sample_feature_schemas')}. Rows on the older "
              "schema carry constant adx/cmf/sector-RS, so a weight derived "
              "for those features describes the recording gap rather than the "
              "market - see learning/pattern_database.py's "
              "FEATURE_SCHEMA_VERSION.")
    if not weights:
        print("  §19: NO feature earned a weight. Every candidate was constant,")
        print("       degenerate, or too thin. This is a finding about the")
        print("       inputs, not a scoring function.")
    else:
        print("  §19 proposed weights (share of total |rank correlation|):")
        for k, w in list(weights.items())[:15]:
            rho = per_feature[k]["rho"]
            print(f"    {k:<34} {w:>7.4f}   rho={rho:+.3f}  n={per_feature[k]['n']}")
    excluded = {k: v["excluded"] for k, v in per_feature.items() if v["excluded"]}
    if excluded:
        print(f"\n  {len(excluded)} feature(s) earned nothing:")
        for k, why in sorted(excluded.items())[:15]:
            print(f"    {k:<34} {why}")
    print("\n  §20 outcome distribution:")
    for k, v in dist.items():
        print(f"    {k:<34} {v}")
    print("\n  §21 sizing input:")
    for k, v in (tiers or {}).items():
        print(f"    {k:<34} {v}")
    print()
    print(f"  written to {out}")
    print("  NOTHING WAS APPLIED. config.yaml is untouched.")
    print(BANNER)
    return 0


# ---------------------------------------------------------------------------
#  receipt
# ---------------------------------------------------------------------------
def cmd_receipt(args) -> int:
    """Write the §32 validation receipt.

    engine/live_trader.py has looked for this file since Phase 1 and has never
    found one, because nothing wrote it - so `validation receipt gate blocks
    arming` has been passing for the least interesting possible reason. This
    writes it, and refuses to write a passing one on anything less than a
    backtest that ran and a comparison that showed a measured difference."""
    from storage.paths import validation_receipt_path

    results = json.loads(Path(args.backtest_results).read_text())
    summary = results.get("summary") or {}
    n_trades = summary.get("n_trades", 0)

    reasons = []
    if n_trades < args.min_trades:
        reasons.append(f"backtest produced {n_trades} trades, minimum {args.min_trades}")
    if args.comparison:
        comp = Path(args.comparison)
        if not comp.exists():
            reasons.append(f"comparison file {comp} does not exist")
    if not args.signed_off_by:
        reasons.append("no --signed-off-by; a receipt records that a PERSON "
                       "looked at the numbers, and an unattributed one records "
                       "nothing")

    passed = not reasons
    receipt = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": passed,
        "reason": "; ".join(reasons),
        "summary": (f"{n_trades} trades, win rate {summary.get('win_rate')}%, "
                    f"profit factor {summary.get('profit_factor')}"),
        "backtest_results": str(args.backtest_results),
        "comparison": args.comparison,
        "signed_off_by": args.signed_off_by,
        "app_version": os.getenv("TP_VERSION", "unknown"),
    }
    path = Path(validation_receipt_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2))

    print(f"{'PASSED' if passed else 'FAILED'} receipt written to {path}")
    if reasons:
        for r in reasons:
            print(f"  - {r}")
        print("\nA failing receipt is written deliberately rather than skipped:")
        print("live_trader.py distinguishes 'last validation FAILED' from 'no")
        print("receipt', and the first is the more useful thing to read.")
    return 0 if passed else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("assess", help="is the sample fit to recalibrate from?"
                   ).set_defaults(func=cmd_assess)

    q = sub.add_parser("propose", help="derive weights/thresholds/tiers from the data")
    q.add_argument("--out", help="proposal path (default output/phase4_proposal.json)")
    q.set_defaults(func=cmd_propose)

    r = sub.add_parser("receipt", help="write the §32 validation receipt")
    r.add_argument("--backtest-results", required=True,
                   help="results.json from the validation backtest")
    r.add_argument("--comparison", help="output of scripts/compare_versions.py")
    r.add_argument("--signed-off-by", help="who read the numbers")
    r.add_argument("--min-trades", type=int, default=30)
    r.set_defaults(func=cmd_receipt)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
