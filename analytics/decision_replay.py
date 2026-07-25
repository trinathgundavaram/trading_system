"""Decision Replay - lightweight execution/decision replay for verification
and debugging, NOT optimization. Answers "the system bought NVDA on
2026-07-14 - why?" by reconstructing everything that was computed about that
decision from data ALREADY persisted (storage/database.py's signals table,
extended by _migrate_decision_context_columns) - no live re-derivation, no
re-fetching quotes, no re-running rules. If it isn't in the database, this
says so rather than guessing.

What gets reconstructed (per the review's own list):
  - candidate list  - every signal row for this ticker on this date, i.e.
                       every scan cycle that re-evaluated it as a candidate
                       that day (the closest available proxy - this codebase
                       doesn't persist a separate "who was scanned" list per
                       cycle, only what each ticker scored).
  - buy score        - signals.buy_score / buy_pct
  - threshold         - signals.threshold_breakdown (full
                       rules/dynamic_thresholds.py calc_threshold() dict)
  - bucket contributions - signals.bucket_scores (per-bucket raw/weighted/
                       checklist breakdown, see rules/swing_buy_rules.py)
  - execution quality - signals.execution_quality
  - position size     - signals.position_size
  - exit score (if applicable) - sell_triggered_rule/sell_reason on a SELL
                       signal, plus the matching trades-table row if this
                       setup was ever actually filled (confirm_fill.py)

Signals recorded BEFORE the decision-context migration (see
storage/database.py's _migrate_decision_context_columns) simply have None
in the new columns - replay still returns what WAS captured (bucket_scores,
rules_fired/failed) rather than failing outright.
"""
from datetime import datetime


def replay_signal(db, ticker: str = None, date: str = None, signal_id: int = None) -> dict:
    """Main entry point. Either pass signal_id directly (exact row, e.g. from
    a UI link), or ticker (+ optional date, "YYYY-MM-DD") to look up the most
    recent/matching signal. Returns found=False with an explanatory note
    (never raises) when nothing is on record - a replay tool that crashes on
    a missing row is worse than one that says "nothing here"."""
    if signal_id is not None:
        signal = db.get_signal_by_id(signal_id)
        if signal:
            ticker = ticker or signal["ticker"]
            date = date or signal["timestamp"][:10]
    else:
        if not ticker:
            raise ValueError("replay_signal requires either signal_id or ticker")
        signal = db.find_signal(ticker, date)

    if not signal:
        return {
            "found": False, "ticker": ticker, "date": date,
            "note": (
                f"No stored signal found for {ticker or '(unknown ticker)'}"
                f"{' on ' + date if date else ''}. Either this ticker was never scanned on that "
                f"date, or the signal predates this database (signals are never backfilled)."
            ),
        }

    resolved_date = date or signal["timestamp"][:10]

    # Every signal row for this ticker on this date - each scan cycle
    # re-evaluated it as a "candidate," so the sequence of same-day rows is
    # the closest reconstruction of "what did the candidate list look like
    # this day" available from stored data (see module docstring).
    cycle_history = [
        s for s in db.get_recent_signals(limit=1000)
        if s["ticker"] == ticker and s["timestamp"][:10] == resolved_date
    ]
    cycle_history.sort(key=lambda s: s["timestamp"])

    exit_context = None
    if signal.get("signal") == "SELL":
        exit_context = {
            "triggered_rule": signal.get("sell_triggered_rule"),
            "reason": signal.get("sell_reason"),
        }

    related_trade = None
    try:
        for t in db.get_recent_trades(200):
            if t.get("ticker") == ticker:
                related_trade = t
                break
    except Exception:
        related_trade = None

    return {
        "found": True,
        "ticker": ticker,
        "date": resolved_date,
        "final_signal": signal,
        "cycle_history": cycle_history,
        "n_cycles_that_day": len(cycle_history),
        "exit_context": exit_context,
        "related_trade": related_trade,
        "replayed_at": datetime.utcnow().isoformat(),
    }


def _fmt_pct(v):
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "n/a"


def render_replay(record: dict) -> str:
    """Renders replay_signal()'s output as readable markdown - same
    plain-text style as engine/packet_builder.py's other ### sections, so it
    drops straight into a chat reply or a saved .md file."""
    if not record.get("found"):
        return f"# Decision Replay: {record.get('ticker') or '?'}\n\n{record['note']}\n"

    s = record["final_signal"]
    lines = [
        f"# Decision Replay: {record['ticker']} — {record['date']}",
        "",
        f"Final decision this day: **{s.get('signal')}** at {s.get('timestamp')} "
        f"(price ${s.get('price')})" if s.get("price") else f"Final decision this day: **{s.get('signal')}**",
        f"{record['n_cycles_that_day']} scan cycle(s) evaluated this ticker on {record['date']}.",
        "",
        "## Buy Score",
        f"- Score: {_fmt_pct(s.get('buy_score'))} raw / {_fmt_pct(s.get('buy_pct'))} normalized"
        if s.get("buy_score") is not None else "- Not scored this cycle (vetoed or already an open position).",
    ]

    # 2026-07-15: THE decision basis, replacing the old score-vs-threshold
    # cliff whenever the pattern DB had enough history - see
    # rules/probabilistic_decision.py. Placed right after Buy Score since
    # this (not the Threshold section below) is what actually decided
    # should_buy for any signal logged after that migration.
    pd = s.get("probabilistic_decision")
    lines.append("\n## Probabilistic Decision")
    if pd:
        lines.append(f"- Mode: **{pd.get('mode')}**")
        if pd.get("mode") == "probabilistic":
            lines.append(f"- {pd.get('headline')}")
            lines.append(
                f"- P(win) {_fmt_pct((pd.get('probability_of_success') or 0) * 100)}, "
                f"P(>{pd.get('target_gain_pct')}% gain) "
                f"{_fmt_pct((pd.get('p_target_gain') or 0) * 100)}, "
                f"P(stop-loss range loss) {_fmt_pct((pd.get('p_stop_loss') or 0) * 100)}"
            )
            hold_h = pd.get("expected_hold_hours")
            hold_text = f"{hold_h:.0f}h" if hold_h is not None else "n/a"
            lines.append(
                f"- Expected return {_fmt_pct(pd.get('expected_return_pct'))}, "
                f"expected drawdown {_fmt_pct(pd.get('expected_drawdown_pct'))} (proxy, see "
                f"engine/ev_engine.py's HONESTY NOTE), "
                f"expected hold {hold_text}"
            )
            lines.append(f"- Based on {pd.get('n_matches')} similar closed trades (confidence: {pd.get('confidence')})")
            if pd.get("threshold_would_have_passed") != pd.get("should_buy"):
                lines.append(
                    f"- NOTE: the old score-vs-threshold method would have said "
                    f"{'PASS' if pd.get('threshold_would_have_passed') else 'FAIL'} - the two methods disagreed here."
                )
        else:
            lines.append(f"- {pd.get('reason')}")
    else:
        lines.append("- Not recorded for this signal (predates the probabilistic-decision migration, or this "
                      "signal was vetoed/already-open before scoring ran).")

    tb = s.get("threshold_breakdown")
    lines.append("\n## Threshold")
    if tb:
        lines.append(f"- Final threshold: {tb.get('final_threshold')}% "
                      f"(confidence {tb.get('confidence')}% {tb.get('confidence_level')})")
        lines.append(f"- {tb.get('breakdown')}")
    else:
        lines.append("- No threshold data stored for this signal.")

    bs = s.get("bucket_scores")
    lines.append("\n## Bucket Contributions")
    if bs:
        for b in bs:
            qualified = "PASS" if b.get("qualified") else "below min"
            lines.append(
                f"- {b['name']}: {b['points']}/{b['max_points']} pts ({qualified}), "
                f"weight {b['weight']*100:.0f}%, qual_mult {b.get('qual_mult', 1.0):.2f}, "
                f"contributes {b.get('contribution_pct', 0):.2f}pp"
            )
    else:
        lines.append("- No bucket breakdown stored for this signal.")

    eq = s.get("execution_quality")
    lines.append("\n## Execution Quality")
    if eq:
        lines.append(f"- Score: {eq.get('total_score')}/100 ({eq.get('tier')}), "
                      f"score adjustment {eq.get('score_adjustment_pct', 0):+.1f}pts, "
                      f"size multiplier {eq.get('size_multiplier')}x")
    else:
        lines.append("- Not computed for this signal.")

    ps = s.get("position_size")
    lines.append("\n## Suggested Position Size")
    if ps:
        lines.append(f"- ${ps.get('suggested_dollar_amount')} ({ps.get('suggested_size_pct')}% of base size, "
                      f"{ps.get('tier_label')} conviction)")
    else:
        lines.append("- Not computed for this signal.")

    pr = s.get("portfolio_risk")
    lines.append("\n## Portfolio Risk")
    if pr:
        lines.append(f"- Allowed: {pr.get('allowed')}, size multiplier {pr.get('size_multiplier')}x, "
                      f"reasons: {pr.get('reasons') or 'none'}")
    else:
        lines.append("- Not computed for this signal.")

    rs = s.get("regime_snapshot")
    lines.append("\n## Regime at Decision Time")
    if rs:
        lines.append(f"- {rs.get('dominant_regime')} (bull {rs.get('bull_pct')}% / bear {rs.get('bear_pct')}% / "
                      f"choppy {rs.get('choppy_pct')}%, transition {rs.get('transition_probability')}%)")
    else:
        lines.append("- Not recorded for this signal.")

    lines.append(f"\n## Asset Class\n- {s.get('asset_class') or 'not recorded'}")

    if record.get("exit_context"):
        ec = record["exit_context"]
        lines.append(f"\n## Exit Score\n- Triggered rule: {ec.get('triggered_rule')}\n- Reason: {ec.get('reason')}")

    if record.get("related_trade"):
        t = record["related_trade"]
        lines.append(f"\n## Related Trade Record\n- id {t.get('id')}, status {t.get('status')}, "
                      f"side {t.get('side')}, shares {t.get('shares')}, fill price ${t.get('fill_price')}")

    if record["n_cycles_that_day"] > 1:
        lines.append(f"\n## Same-Day Cycle History ({record['n_cycles_that_day']} cycles)")
        for c in record["cycle_history"]:
            lines.append(f"- {c['timestamp']}: {c['signal']} @ {_fmt_pct(c.get('buy_pct'))}")

    return "\n".join(lines) + "\n"
