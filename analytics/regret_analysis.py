"""Regret Analysis - "now you can identify targets too conservative, stops
too tight, exit score too aggressive, time stops too early - without
changing the strategy immediately." For every CLOSED pattern_database trade
(the same "closed trade" this codebase's whole learning stack already uses -
walk_forward.py, bayesian_updater.py, feature_importance.py, EV engine -
mode=SWING, is_closed=1), this looks at what the ticker did AFTER the exit
(real yfinance price history, analytics/price_history_utils.py) and scores
how much upside was left on the table (regret) vs. how much downside was
avoided by exiting when it did (the honest flip side - a high-regret exit
isn't automatically a bad one if it also dodged a big drop).

HONESTY NOTE on classification: pattern_database rows currently only ever
close via two real paths - scheduler.py's time-based auto-close
("time_based_close") or confirm_fill.py's manual fill confirmation
("manual_fill_confirmed") - see storage/database.py's exit_reason column.
Neither rules/exit_scorer.py's exit-score triggers nor a real stop-loss/
profit-target ever write their OWN distinct exit_reason into pattern_database
today (they close real POSITIONS, not pattern-database rows - two separate
tables, see engine/position_management.py). _classify_regret() below matches
on keywords ("stop", "target", "exit_score", "time") so classification is
ready to sharpen automatically if/when this codebase starts passing more
granular exit_reason strings into close_trade() - it isn't dead logic, just
under-fed by today's exit_reason vocabulary. Until then, expect most real
rows to classify as "time_stop_too_early" (if regret is material) or
"well_timed_exit" - that's an accurate reflection of today's data, not a bug
in the classifier.

Nothing here is applied automatically. Call build_regret_report() whenever
you want a fresh view; results are cached per pattern_id in the
regret_analysis table so re-running doesn't re-hit yfinance for trades
already fully evaluated (still_maturing=True rows - the exit was too recent
for a full forward window - get re-checked on the next call).
"""
from datetime import date, datetime, timedelta

from analytics.price_history_utils import get_closes_series, slice_forward

DEFAULT_FORWARD_WINDOW_DAYS = 20
DEFAULT_MATERIAL_REGRET_PCT = 5.0  # below this, an exit is "well timed" regardless of exit_reason


def _classify_regret(exit_reason: str, regret_pct: float, downside_avoided_pct: float, cfg: dict = None) -> str:
    material = (cfg or {}).get("regret_analysis", {}).get(
        "material_regret_pct", DEFAULT_MATERIAL_REGRET_PCT
    )
    reason = (exit_reason or "").lower()

    if regret_pct < material:
        if downside_avoided_pct >= material:
            return "well_timed_exit_avoided_downside"
        return "well_timed_exit"

    # Regret is material - attribute it via exit_reason keywords (see module
    # docstring's HONESTY NOTE on which of these are actually reachable today).
    if "stop" in reason:
        return "stop_too_tight"
    if "target" in reason or "profit" in reason:
        return "target_too_conservative"
    if "exit_score" in reason or "exitscore" in reason:
        return "exit_score_too_aggressive"
    if "time" in reason:
        return "time_stop_too_early"
    if "manual" in reason:
        return "manual_exit_left_upside"
    return "unclassified_material_regret"


def analyze_trade_regret(pattern: dict, forward_window_days: int = DEFAULT_FORWARD_WINDOW_DAYS,
                          cfg: dict = None, yf_client=None) -> dict:
    """pattern: a closed row from db.get_patterns(closed_only=True) - needs
    features["_entry_price"], outcome_pct, hold_hours, recorded_at, ticker,
    exit_reason, id. Returns None (skip, don't guess) if entry price is
    missing or the trade isn't actually closed yet. Returns a record with
    classification="still_maturing" if the exit is too recent for ANY
    forward price data yet (distinct from partial-window data, which still
    gets a real classification based on what's available so far)."""
    entry_price = (pattern.get("features") or {}).get("_entry_price")
    outcome_pct = pattern.get("outcome_pct")
    if not entry_price or outcome_pct is None:
        return None

    try:
        recorded = datetime.fromisoformat(pattern["recorded_at"])
    except (ValueError, TypeError, KeyError):
        return None

    exit_dt = recorded + timedelta(hours=pattern.get("hold_hours") or 0)
    exit_date = exit_dt.date()
    exit_price = entry_price * (1 + outcome_pct / 100)
    days_ago = max(0, (date.today() - exit_date).days)

    series = get_closes_series(
        pattern["ticker"], days_ago_needed=days_ago + forward_window_days + 5, yf_client=yf_client
    )
    closes = series["closes"]
    base = {
        "pattern_id": pattern["id"], "ticker": pattern["ticker"], "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2), "exit_reason": pattern.get("exit_reason"),
        "forward_window_days": forward_window_days,
    }
    if not closes:
        return None  # no price data at all - don't fabricate a regret score

    forward = slice_forward(closes, exit_date, forward_window_days)
    if not forward:
        return {
            **base, "highest_afterwards": None, "lowest_afterwards": None,
            "regret_pts": None, "regret_pct": None, "downside_avoided_pts": None, "downside_avoided_pct": None,
            "classification": "still_maturing", "still_maturing": True,
        }

    highest, lowest = max(forward), min(forward)
    regret_pts = max(0.0, highest - exit_price)
    regret_pct = round(regret_pts / entry_price * 100, 2)
    downside_avoided_pts = max(0.0, exit_price - lowest)
    downside_avoided_pct = round(downside_avoided_pts / entry_price * 100, 2)
    classification = _classify_regret(pattern.get("exit_reason"), regret_pct, downside_avoided_pct, cfg)

    return {
        **base, "highest_afterwards": round(highest, 2), "lowest_afterwards": round(lowest, 2),
        "regret_pts": round(regret_pts, 2), "regret_pct": regret_pct,
        "downside_avoided_pts": round(downside_avoided_pts, 2), "downside_avoided_pct": downside_avoided_pct,
        "classification": classification, "still_maturing": len(forward) < forward_window_days,
    }


def build_regret_report(db, mode: str = "SWING", limit: int = 100,
                         forward_window_days: int = DEFAULT_FORWARD_WINDOW_DAYS, cfg: dict = None,
                         yf_client=None, force_recompute: bool = False) -> dict:
    """Main entry point. Iterates the most recent `limit` closed trades,
    reusing cached regret_analysis rows unless still_maturing (worth
    re-checking as more time passes) or force_recompute=True. Returns
    {"records": [...], "summary": {classification: {n, avg_regret_pct}}}."""
    patterns = db.get_patterns(mode=mode, closed_only=True)
    patterns = sorted(patterns, key=lambda p: p.get("recorded_at", ""), reverse=True)[:limit]

    records = []
    for p in patterns:
        cached = None if force_recompute else db.get_regret_analysis(p["id"])
        if cached and not cached.get("still_maturing"):
            records.append(cached)
            continue

        rec = analyze_trade_regret(p, forward_window_days=forward_window_days, cfg=cfg, yf_client=yf_client)
        if rec is None:
            continue
        records.append(rec)
        db.save_regret_analysis(
            pattern_id=rec["pattern_id"], ticker=rec["ticker"], entry_price=rec["entry_price"],
            exit_price=rec["exit_price"], exit_reason=rec["exit_reason"],
            forward_window_days=rec["forward_window_days"], highest_afterwards=rec["highest_afterwards"],
            lowest_afterwards=rec["lowest_afterwards"], regret_pts=rec["regret_pts"], regret_pct=rec["regret_pct"],
            downside_avoided_pts=rec["downside_avoided_pts"], downside_avoided_pct=rec["downside_avoided_pct"],
            classification=rec["classification"], still_maturing=rec["still_maturing"],
        )

    by_class = {}
    for r in records:
        if r.get("regret_pct") is None:
            continue
        by_class.setdefault(r["classification"], []).append(r["regret_pct"])
    summary = {
        c: {"n": len(v), "avg_regret_pct": round(sum(v) / len(v), 2)}
        for c, v in sorted(by_class.items(), key=lambda kv: -len(kv[1]))
    }

    return {"records": records, "summary": summary, "n_evaluated": len(records)}


def render_regret_report(report: dict) -> str:
    """Markdown rendering matching the review's own ASCII-art example
    (Bought / Exited / Highest afterwards / Regret), plus the classification
    label and a summary rollup."""
    records = report.get("records", [])
    lines = ["# Regret Analysis", ""]
    if not records:
        lines.append("No closed trades with enough forward price history to evaluate yet.")
        return "\n".join(lines) + "\n"

    summary = report.get("summary", {})
    if summary:
        lines.append("## Summary by Classification")
        for cls, s in summary.items():
            lines.append(f"- {cls}: {s['n']} trade(s), avg regret {s['avg_regret_pct']:+.1f}%")
        lines.append("")

    for r in records:
        lines.append(f"## {r['ticker']} (pattern #{r['pattern_id']})")
        lines.append(f"- Bought: ${r['entry_price']}")
        lines.append(f"- Exited: ${r['exit_price']} ({r.get('exit_reason') or 'unknown reason'})")
        if r.get("classification") == "still_maturing":
            lines.append("- Still maturing - not enough time has passed since exit to evaluate regret yet.")
        else:
            lines.append(f"- Highest afterwards: ${r['highest_afterwards']}")
            lines.append(f"- Lowest afterwards: ${r['lowest_afterwards']}")
            lines.append(f"- Regret: {r['regret_pts']} points ({r['regret_pct']:+.1f}% of entry)")
            lines.append(f"- Downside avoided: {r['downside_avoided_pts']} points ({r['downside_avoided_pct']:+.1f}% of entry)")
            lines.append(f"- Classification: {r['classification']}")
        lines.append("")

    return "\n".join(lines)
