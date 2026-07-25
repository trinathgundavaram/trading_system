"""Missed Opportunity Report - "sometimes your biggest gains come from
studying the trades you didn't take." Every ticker that got fully SCORED by
rules/swing_buy_rules.py (cleared all hard vetoes) but landed on HOLD because
it didn't cross the dynamic threshold is a candidate: this reconstructs the
per-bucket ✔/✘ checklist and how many points it missed the threshold by
(already persisted this session via storage/database.py's decision-context
columns - bucket_scores/threshold_breakdown - no new scoring-side plumbing
needed), then simulates what a real forward-looking return would have been
using actual yfinance price history (analytics/price_history_utils.py) - not
a guess, a real simulated outcome, same posture as the pattern-database's
own simulated closes (see engine/pattern_features.py / scheduler.py's
_close_due_patterns).

Scope note: this covers SCORED-but-below-threshold misses only, not
hard-veto rejections (those never reach score() so have no bucket data to
show a checklist for) - that older, narrower use case already has a
scaffold in analytics/opportunity_cost.py's rejected_signals path (built but
never wired into the live scan cycle). This module doesn't touch that one;
they answer related but different questions ("was the threshold too strict"
vs "was a specific hard-veto rule too strict").

Nothing here is applied automatically - same posture as every other
analytics/ module in this codebase. Call evaluate_missed_opportunities()
whenever you want a fresh report; results are cached per signal_id in
missed_opportunity_outcomes so re-running the report doesn't re-hit yfinance
for signals already simulated.

2026-07-23 addition (OXY dynamic-threshold review, Trinath: "can this be
evaluated and build a process for that evaluation and learning so such false
negatives are identified and at same time potential stocks are not missed"):
evaluate_threshold_regret() builds on the above to answer the review's exact
follow-up question - segment these same HOLD signals by the SIZE of the
dynamic-threshold adjustment that rejected them (0-3%/3-6%/6-10%/10-15%+,
see bucket_by_threshold_adjustment()) and separately isolate the
breadth-penalty double-counting concern (breadth_adjustment_comparison()),
comparing subsequent returns across groups. Same "measure, don't
auto-apply" posture - see engine/learning_loop.py's maybe_run_threshold_regret()
for the periodic (weekly, cheap, cached) automation that keeps this fed with
fresh evaluations without a human having to remember to run it, and
storage/database.py's threshold_regret_runs table for the run-over-time
history that answers "is this pattern getting stronger with more data or
was it a one-off."
"""
from datetime import date, datetime

from analytics.price_history_utils import closes_on_or_after, get_closes_series, slice_forward

DEFAULT_HOLD_DAYS = 5  # matches config.yaml's learning.pattern_hold_days convention


def find_missed_opportunities(db, limit: int = 50) -> list:
    """Static half of the report (no price simulation, no MCP calls) - pulls
    recent HOLD signals with bucket/threshold data and builds the checklist +
    threshold-missed-by figure straight from already-persisted columns.

    Also surfaces the individual rules/dynamic_thresholds.py adjustment
    components (total_threshold_adj, breadth_adj, breadth_tier, stress_adj,
    base_threshold) on each record - not used by the plain checklist report
    below, but is exactly the raw material threshold_regret_by_bucket() and
    breadth_adjustment_comparison() need to answer "did the dynamic threshold
    reject a setup that would have worked?" without re-parsing
    threshold_breakdown a second time."""
    rows = db.get_hold_signals(limit=limit)
    records = []
    for s in rows:
        buckets = s.get("bucket_scores") or []
        threshold = s.get("threshold_breakdown") or {}
        threshold_pct = threshold.get("final_threshold")
        score_pct = s.get("buy_pct")
        missed_by = (
            round(threshold_pct - score_pct, 2)
            if threshold_pct is not None and score_pct is not None else None
        )
        checklist = [{"name": b.get("name"), "qualified": bool(b.get("qualified"))} for b in buckets]
        records.append({
            "signal_id": s["id"], "ticker": s["ticker"], "timestamp": s["timestamp"],
            "price_at_signal": s.get("price"), "score_pct": score_pct, "threshold_pct": threshold_pct,
            "threshold_missed_by": missed_by, "bucket_checklist": checklist,
            "asset_class": s.get("asset_class"),
            # dynamic-threshold breakdown, straight from rules/dynamic_thresholds.py's
            # calculate() return dict (persisted verbatim in signals.threshold_breakdown).
            "base_threshold": threshold.get("base_threshold"),
            "total_threshold_adj": threshold.get("total_adj_before_cap"),
            "stress_adj": threshold.get("stress_adj"),
            "breadth_adj": threshold.get("breadth_adj"),
            "breadth_tier": threshold.get("breadth_tier"),
            "tp_adj": threshold.get("tp_adj"),
        })
    return records


def simulate_forward_outcome(ticker: str, signal_timestamp: str, hold_days: int, yf_client=None) -> dict:
    """Real forward-price simulation via yfinance (analytics/
    price_history_utils.py). Returns a dict with status "ok" (full hold_days
    window elapsed - would_have_returned_pct/peak/trough all populated),
    "still_pending" (signal is too recent - only a partial window available,
    peak/trough reflect what HAS happened so far), or "unavailable" (no
    price data at all - bad ticker, MCP unreachable, or the signal predates
    available history). Never raises."""
    try:
        signal_date = datetime.fromisoformat(signal_timestamp).date()
    except (ValueError, TypeError):
        return {"status": "unavailable"}

    days_ago = (date.today() - signal_date).days
    if days_ago < 0:
        return {"status": "unavailable"}

    series = get_closes_series(ticker, days_ago_needed=days_ago + hold_days + 5, yf_client=yf_client)
    closes = series["closes"]
    if not closes:
        return {"status": "unavailable"}

    entry_price = closes_on_or_after(closes, signal_date)
    if not entry_price:
        return {"status": "unavailable"}

    forward = slice_forward(closes, signal_date, hold_days)
    if not forward:
        return {"status": "still_pending", "entry_price": entry_price}

    peak_idx = max(range(len(forward)), key=lambda i: forward[i])
    trough_idx = min(range(len(forward)), key=lambda i: forward[i])
    result = {
        "entry_price": entry_price,
        "peak_return_pct": round((forward[peak_idx] - entry_price) / entry_price * 100, 2),
        "peak_at_days": peak_idx + 1,
        "trough_return_pct": round((forward[trough_idx] - entry_price) / entry_price * 100, 2),
        "trough_at_days": trough_idx + 1,
    }
    if len(forward) < hold_days:
        result["status"] = "still_pending"
        return result

    exit_price = forward[hold_days - 1]
    result["status"] = "ok"
    result["would_have_returned_pct"] = round((exit_price - entry_price) / entry_price * 100, 2)
    return result


def evaluate_missed_opportunities(db, cfg: dict = None, limit: int = 50, hold_days: int = None,
                                   yf_client=None, force_resim: bool = False) -> list:
    """Main entry point - find_missed_opportunities() + simulate_forward_outcome()
    per record, with per-signal caching in missed_opportunity_outcomes so a
    repeated call only re-simulates records that are new or were
    still_pending last time (force_resim=True bypasses the cache entirely,
    e.g. to refresh still-pending records once more time has passed)."""
    hold_days = hold_days or (cfg or {}).get("learning", {}).get("pattern_hold_days", DEFAULT_HOLD_DAYS)
    records = find_missed_opportunities(db, limit=limit)

    for r in records:
        cached = None if force_resim else db.get_missed_opportunity_outcome(r["signal_id"])
        if cached and not cached.get("still_pending"):
            r.update({
                "would_have_returned_pct": cached["would_have_returned_pct"],
                "peak_return_pct": cached["peak_return_pct"], "peak_at_days": cached["peak_at_days"],
                "trough_return_pct": cached["trough_return_pct"], "trough_at_days": cached["trough_at_days"],
                "still_pending": False,
            })
            continue

        sim = simulate_forward_outcome(r["ticker"], r["timestamp"], hold_days, yf_client=yf_client)
        if sim["status"] == "unavailable":
            r.update({"would_have_returned_pct": None, "peak_return_pct": None, "peak_at_days": None,
                      "trough_return_pct": None, "trough_at_days": None, "still_pending": None})
            continue

        still_pending = sim["status"] == "still_pending"
        r.update({
            "would_have_returned_pct": sim.get("would_have_returned_pct"),
            "peak_return_pct": sim.get("peak_return_pct"), "peak_at_days": sim.get("peak_at_days"),
            "trough_return_pct": sim.get("trough_return_pct"), "trough_at_days": sim.get("trough_at_days"),
            "still_pending": still_pending,
        })
        db.save_missed_opportunity_outcome(
            signal_id=r["signal_id"], ticker=r["ticker"], hold_days=hold_days,
            entry_price=sim.get("entry_price"), would_have_returned_pct=sim.get("would_have_returned_pct"),
            peak_return_pct=sim.get("peak_return_pct"), peak_at_days=sim.get("peak_at_days"),
            trough_return_pct=sim.get("trough_return_pct"), trough_at_days=sim.get("trough_at_days"),
            still_pending=still_pending,
        )
    return records


def missed_opportunity_summary(db, cfg: dict = None, limit: int = 200) -> dict:
    """Aggregate view: average missed upside, win rate of the misses, and
    which bucket most often is the one that blocked an otherwise-good
    candidate - complements analytics/opportunity_cost.py's by-reject-stage
    rollup (that one is scoped to hard-veto rejections, this one to
    threshold misses)."""
    records = evaluate_missed_opportunities(db, cfg=cfg, limit=limit)
    evaluated = [r for r in records if r.get("would_have_returned_pct") is not None]
    n = len(evaluated)
    if n == 0:
        return {"n_evaluated": 0,
                "note": "No missed opportunities with a completed forward-return simulation yet."}

    avg_return = sum(r["would_have_returned_pct"] for r in evaluated) / n
    avg_peak = sum(r["peak_return_pct"] for r in evaluated) / n
    winners = sum(1 for r in evaluated if r["would_have_returned_pct"] > 0)

    fail_counts = {}
    for r in evaluated:
        for b in r["bucket_checklist"]:
            if not b["qualified"]:
                fail_counts[b["name"]] = fail_counts.get(b["name"], 0) + 1

    return {
        "n_evaluated": n,
        "avg_would_have_returned_pct": round(avg_return, 2),
        "avg_peak_return_pct": round(avg_peak, 2),
        "pct_would_have_won": round(winners / n * 100, 1),
        "most_common_unqualified_buckets": sorted(fail_counts.items(), key=lambda x: -x[1]),
    }


DEFAULT_ADJ_BINS = (0.0, 3.0, 6.0, 10.0, 15.0, float("inf"))


def _bin_label(lo: float, hi: float) -> str:
    if hi == float("inf"):
        return f"{lo:.0f}%+"
    return f"{lo:.0f}-{hi:.0f}%"


def _bucket_stats(evaluated: list) -> dict:
    """Shared aggregate math for one group of already-simulated records
    (would_have_returned_pct populated, still-pending/unavailable excluded
    upstream) - n, average realized return, average peak upside left on the
    table, and win rate. Returns None for an empty group rather than
    fabricating a 0.0 that would be indistinguishable from a real result."""
    n = len(evaluated)
    if n == 0:
        return None
    avg_return = sum(r["would_have_returned_pct"] for r in evaluated) / n
    avg_peak = sum(r["peak_return_pct"] for r in evaluated) / n
    winners = sum(1 for r in evaluated if r["would_have_returned_pct"] > 0)
    return {
        "n": n,
        "avg_would_have_returned_pct": round(avg_return, 2),
        "avg_peak_return_pct": round(avg_peak, 2),
        "pct_would_have_won": round(winners / n * 100, 1),
    }


def bucket_by_threshold_adjustment(records: list, bins: tuple = DEFAULT_ADJ_BINS) -> list:
    """Answers the exact question the OXY review raised: 'when threshold
    adjustments add 0-3%, what's the subsequent return? 3-6%? 6-10%?
    10-15%+?' Groups already-simulated records (from
    evaluate_missed_opportunities(), which has total_threshold_adj +
    would_have_returned_pct on each) by the SIZE of the dynamic-threshold
    adjustment that rejected them, not by score or ticker.

    If a bucket's rejected setups perform just as well (or better) than
    setups with a smaller/no adjustment, that's evidence the adjustment size
    in that range is costing real expectancy without buying real protection -
    the threshold is too strict there. If rejected setups in a bucket perform
    materially worse, the adjustment is earning its keep. This function only
    computes the comparison; it doesn't draw the conclusion - that call
    needs enough n per bucket to mean anything (see min_n note in
    evaluate_threshold_regret's docstring)."""
    evaluated = [
        r for r in records
        if r.get("would_have_returned_pct") is not None and r.get("total_threshold_adj") is not None
    ]
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        group = [r for r in evaluated if lo <= r["total_threshold_adj"] < hi]
        stats = _bucket_stats(group)
        out.append({
            "bucket": _bin_label(lo, hi), "adj_range": [lo, hi if hi != float("inf") else None],
            **(stats or {"n": 0, "avg_would_have_returned_pct": None,
                          "avg_peak_return_pct": None, "pct_would_have_won": None}),
        })
    return out


def breadth_adjustment_comparison(records: list) -> dict:
    """Isolates the specific double-counting concern the OXY review flagged:
    MARKET_BREADTH is already an 11%-weighted scoring bucket AND weak breadth
    separately raises the dynamic threshold (rules/dynamic_thresholds.py's
    _breadth_adj). Splits already-simulated records into 'breadth penalized
    this signal's threshold' (breadth_adj > 0, i.e. weak/very_weak/panic
    tier) vs 'breadth was neutral-or-better' (breadth_adj <= 0, i.e.
    good/excellent tier) and compares subsequent returns between the two
    groups.

    If the penalized group performs no worse (or better) than the
    neutral-or-better group, the breadth threshold penalty isn't buying
    anything beyond what the MARKET_BREADTH bucket's own score already
    captures - straightforward double-counting. If the penalized group
    performs materially worse, the extra penalty is doing real work on top
    of the bucket score."""
    evaluated = [
        r for r in records
        if r.get("would_have_returned_pct") is not None and r.get("breadth_adj") is not None
    ]
    penalized = [r for r in evaluated if r["breadth_adj"] > 0]
    neutral_or_better = [r for r in evaluated if r["breadth_adj"] <= 0]
    by_tier = {}
    for r in evaluated:
        by_tier.setdefault(r.get("breadth_tier") or "unknown", []).append(r)

    return {
        "breadth_penalized": _bucket_stats(penalized) or {"n": 0},
        "breadth_neutral_or_better": _bucket_stats(neutral_or_better) or {"n": 0},
        "by_tier": {
            tier: _bucket_stats(recs) or {"n": 0}
            for tier, recs in sorted(by_tier.items(), key=lambda kv: -len(kv[1]))
        },
    }


def evaluate_threshold_regret(db, cfg: dict = None, limit: int = 200, hold_days: int = None,
                               yf_client=None, force_resim: bool = False,
                               min_n_for_verdict: int = 10) -> dict:
    """Main entry point for the process the OXY review asked for: 'collect
    every signal that looks like this - high-quality stock score but
    rejected because of dynamic threshold adjustments - and analyze their
    subsequent returns. If rejected stocks consistently outperform, the
    threshold system is too strict. If they consistently underperform,
    that's precisely the behavior you want.'

    Builds on evaluate_missed_opportunities() (same yfinance simulation,
    same per-signal cache in missed_opportunity_outcomes - calling this does
    NOT re-hit yfinance for signals already simulated) and adds the two
    breakdowns the review specifically requested: by adjustment-size bucket
    and by breadth-penalty isolation.

    still_pending signals (not enough calendar time elapsed yet) are
    reported separately (n_still_pending) rather than silently dropped or
    counted as losses - the whole point is not to draw a conclusion before
    the evidence exists.

    min_n_for_verdict gates the plain-English verdict per bucket: below that
    many evaluated signals, the verdict is "insufficient_data" rather than a
    real read either way - a 2-signal bucket is noise, not evidence. Doesn't
    change the underlying numbers, just whether this function is willing to
    characterize them."""
    records = evaluate_missed_opportunities(
        db, cfg=cfg, limit=limit, hold_days=hold_days, yf_client=yf_client, force_resim=force_resim,
    )
    n_still_pending = sum(1 for r in records if r.get("still_pending"))
    n_unavailable = sum(1 for r in records if r.get("would_have_returned_pct") is None and not r.get("still_pending"))
    evaluated = [r for r in records if r.get("would_have_returned_pct") is not None]

    overall = _bucket_stats(evaluated) or {"n": 0}
    adj_buckets = bucket_by_threshold_adjustment(records)
    breadth = breadth_adjustment_comparison(records)

    for b in adj_buckets:
        if b["n"] < min_n_for_verdict:
            b["verdict"] = "insufficient_data"
        elif overall.get("n", 0) >= min_n_for_verdict and b["avg_would_have_returned_pct"] >= overall["avg_would_have_returned_pct"]:
            b["verdict"] = "threshold_may_be_too_strict_here"
        else:
            b["verdict"] = "threshold_appears_justified_here"

    return {
        "n_signals": len(records), "n_evaluated": len(evaluated),
        "n_still_pending": n_still_pending, "n_unavailable": n_unavailable,
        "overall": overall,
        "by_adjustment_bucket": adj_buckets,
        "breadth_isolation": breadth,
        "min_n_for_verdict": min_n_for_verdict,
    }


def render_threshold_regret_report(report: dict) -> str:
    """Markdown rendering of evaluate_threshold_regret()'s output - the
    'is the dynamic threshold too strict, and specifically is breadth
    double-counted' report."""
    lines = ["# Threshold Regret Analysis", ""]
    lines.append(
        f"{report['n_evaluated']} of {report['n_signals']} HOLD signals fully evaluated "
        f"({report['n_still_pending']} still pending, {report['n_unavailable']} unavailable)."
    )
    lines.append("")

    overall = report.get("overall", {})
    if overall.get("n"):
        lines.append(
            f"**Overall**: avg would-have-returned {overall['avg_would_have_returned_pct']:+.1f}%, "
            f"win rate {overall['pct_would_have_won']:.0f}% (n={overall['n']})"
        )
        lines.append("")

    lines.append("## By threshold-adjustment size")
    lines.append("")
    lines.append("| Adjustment | n | Avg return | Win rate | Verdict |")
    lines.append("|---|---|---|---|---|")
    for b in report.get("by_adjustment_bucket", []):
        ret = f"{b['avg_would_have_returned_pct']:+.1f}%" if b["avg_would_have_returned_pct"] is not None else "-"
        win = f"{b['pct_would_have_won']:.0f}%" if b["pct_would_have_won"] is not None else "-"
        lines.append(f"| {b['bucket']} | {b['n']} | {ret} | {win} | {b['verdict']} |")
    lines.append("")

    br = report.get("breadth_isolation", {})
    lines.append("## Breadth-penalty isolation")
    lines.append("")
    pen, neu = br.get("breadth_penalized", {}), br.get("breadth_neutral_or_better", {})
    if pen.get("n"):
        lines.append(f"- Breadth penalized this signal's threshold: n={pen['n']}, "
                      f"avg return {pen['avg_would_have_returned_pct']:+.1f}%, win rate {pen['pct_would_have_won']:.0f}%")
    else:
        lines.append("- Breadth penalized this signal's threshold: no evaluated signals yet")
    if neu.get("n"):
        lines.append(f"- Breadth neutral-or-better: n={neu['n']}, "
                      f"avg return {neu['avg_would_have_returned_pct']:+.1f}%, win rate {neu['pct_would_have_won']:.0f}%")
    else:
        lines.append("- Breadth neutral-or-better: no evaluated signals yet")
    lines.append("")

    return "\n".join(lines) + "\n"


def render_missed_opportunity_report(records: list) -> str:
    """Markdown rendering matching the review's own ASCII-art example
    format (TICKER / Not purchased / Reasons checklist / threshold missed
    by / would have returned)."""
    lines = ["# Missed Opportunity Report", ""]
    if not records:
        lines.append("No missed opportunities on record yet.")
        return "\n".join(lines) + "\n"

    for r in records:
        lines.append(f"## {r['ticker']} — {r['timestamp'][:10]}")
        lines.append("")
        lines.append("Not purchased")
        lines.append("")
        lines.append("Reasons:")
        for b in r["bucket_checklist"]:
            mark = "✔" if b["qualified"] else "✘"
            lines.append(f"- {b['name']}: {mark}")

        if r.get("threshold_missed_by") is not None:
            lines.append(f"\nThreshold missed by: {r['threshold_missed_by']:.1f} points "
                          f"({r['score_pct']:.1f}% vs {r['threshold_pct']:.0f}% required)")

        if r.get("still_pending"):
            note = ""
            if r.get("peak_return_pct") is not None:
                note = f" (so far: peak {r['peak_return_pct']:+.1f}% at day {r['peak_at_days']})"
            lines.append(f"\nWould have returned: still pending - not enough time elapsed yet{note}")
        elif r.get("would_have_returned_pct") is not None:
            lines.append(
                f"\nWould have returned: {r['would_have_returned_pct']:+.1f}% "
                f"(peak {r['peak_return_pct']:+.1f}% at day {r['peak_at_days']}, "
                f"trough {r['trough_return_pct']:+.1f}% at day {r['trough_at_days']})"
            )
        else:
            lines.append("\nWould have returned: price data unavailable")
        lines.append("")

    return "\n".join(lines)
