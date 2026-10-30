"""Re-evaluation tracker: every setting we changed on a hunch or a thin sample is registered here with the evidence
that would settle it. The daily maintenance job runs each check on its own, loads fresh data, stores the result in
reeval_log (history, so trends are visible) and sends one notice when an item becomes ready or suggests a change.

Statuses: waiting (not enough data yet), ok (evidence supports the current setting), ready (enough data, a human
should look), adjust (evidence suggests a change; the headline says which)."""
import json
import logging
import sqlite3
from datetime import date, datetime

logger = logging.getLogger("trading")

SINCE = "2026-10-30"   # when the current rotation / sizing / risk-cap settings went in

ITEMS = [
    {"id": "swing_forward_test", "title": "Swing engine forward test",
     "why": "Is the swing engine worth running? Needs 30 entry days and profit factor above 1.3.",
     "knob": "swing_experiment.decision_date"},
    {"id": "risk_cap_blocks", "title": "Sector / correlation blocks",
     "why": "Do buys refused by the sector, theme or correlation caps beat the ones we took? If yes the caps cost money.",
     "knob": "portfolio_risk.*"},
    {"id": "rotation", "title": "Rotation (swap weak for strong)",
     "why": "Do rotations fire, and were the sold positions really the weak ones?",
     "knob": "rotation.max_rotations_per_week / min_candidate_score"},
    {"id": "position_cap", "title": "Position cap",
     "why": "Is the book pinned at its cap (then raise it or rotate more) or has headroom?",
     "knob": "trading.max_positions / max_day_positions"},
    {"id": "second_opinion", "title": "Second-opinion checklist",
     "why": "Do trades the checklist flagged lose more than the ones it agreed with? Only then does it earn a veto.",
     "knob": "engine/second_opinion.py"},
    {"id": "risk_sizing", "title": "Risk-based sizing",
     "why": "Do trades sized since the change pay off (profit factor, average result)?",
     "knob": "position_sizing.risk_per_trade_pct / min_size_pct"},
    {"id": "entry_quality", "title": "Entry score bar (60 swing / 65 day)",
     "why": "After raising the bar from 50/55, do trades entered since still lose at the low end of the score range, and does CHOPPY still lose?",
     "knob": "risk.TURBO.buy_score_threshold_pct"},
    {"id": "sleeves_vs_spy", "title": "Momentum sleeves vs SPY",
     "why": "Do the three paper sleeves beat buy-and-hold SPY after 60 daily marks?",
     "knob": "*_sleeve.paper_enabled"},
]


def _swing_forward_test(db, cfg, today):
    from engine.strategy_overview import swing_experiment_status
    ex = swing_experiment_status(db, cfg, today) or {}
    pf = ex.get("profit_factor")
    m = {"entry_days": ex.get("entry_days"), "needs_days": ex.get("needs_days"), "profit_factor": pf,
         "decision_date": ex.get("decision_date")}
    dd = ex.get("decision_date")
    if dd and today < date.fromisoformat(dd):
        return "waiting", f"{ex.get('entry_days', 0)}/{ex.get('needs_days', 30)} entry days, PF {('%.2f' % pf) if pf else 'n/a'}; decide {dd}", m
    v = ex.get("verdict")
    return ("ok" if v == "pass" else "adjust"), f"decision day: forward test {v}", m


def _next_day_returns(rows):
    """Counterfactual: price at rejection -> next close, from free data. rows: (ticker, day, price)."""
    import pandas as pd
    import yfinance as yf
    tick = sorted({r[0] for r in rows})
    if not tick:
        return []
    d = yf.download(tick, start=min(r[1] for r in rows), progress=False, auto_adjust=True)["Close"]
    if getattr(d, "ndim", 2) == 1:
        d = d.to_frame(tick[0])
    out = []
    for t, day, px in rows:
        if t not in d:
            continue
        s = d[t].dropna()
        idx = s.index[s.index > pd.Timestamp(day)]
        if len(idx):
            out.append((float(s[idx[0]]) / px - 1) * 100)
    return out


def _risk_cap_blocks(db, cfg, today):
    with db._conn() as c:
        rows = c.execute("SELECT ticker, timestamp, price_at_rejection FROM rejected_signals "
                         "WHERE reject_stage = 'portfolio_risk' AND timestamp >= ? AND price_at_rejection > 0",
                         (SINCE,)).fetchall()
        before = c.execute("SELECT COUNT(*) FROM rejected_signals WHERE reject_stage = 'portfolio_risk' "
                           "AND timestamp < ? AND timestamp >= '2026-10-16'", (SINCE,)).fetchone()[0]
    uniq = {}
    for t, ts, px in rows:
        uniq.setdefault((t, str(ts)[:10]), float(px))
    days = max(1, (today - date.fromisoformat(SINCE)).days)
    m = {"blocked_ticker_days": len(uniq), "days_since_change": days, "blocks_14d_before": before}
    if len(uniq) < 25:
        return "waiting", f"{len(uniq)}/25 blocked buys since {SINCE} to judge", m
    r = _next_day_returns([(t, d, p) for (t, d), p in uniq.items()])
    if len(r) < 20:
        return "waiting", f"{len(r)}/20 blocked buys have a next-day price yet", m
    avg, win = sum(r) / len(r), 100 * sum(1 for x in r if x > 0) / len(r)
    m.update(n=len(r), avg_next_day_pct=round(avg, 2), win_pct=round(win))
    if avg > 0.5 and win >= 58:
        return "adjust", f"blocked buys went +{avg:.2f}% next day ({win:.0f}% up): the caps cost money, loosen them", m
    return "ok", f"blocked buys {avg:+.2f}% next day, {win:.0f}% up: caps are not costing", m


def _paper_trades(db):
    with db._conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()]


def _rotation(db, cfg, today):
    from engine.swing_experiment import round_trips
    with db._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM rotation_log WHERE executed_at >= ?", (SINCE,)).fetchone()[0]
    victims = [r["pnl_pct"] for r in round_trips(_paper_trades(db))
               if "rotation" in str(r.get("reason", "")).lower() and str(r["exit"])[:10] >= SINCE]
    m = {"rotations": n, "victims_closed": len(victims)}
    if n < 3:
        return "waiting", f"{n}/3 rotations since {SINCE}", m
    avg = sum(victims) / len(victims) if victims else 0.0
    m["victim_avg_pct"] = round(avg, 2)
    return "ready", f"{n} rotations; sold positions averaged {avg:+.2f}%: check the bar and weekly budget", m


def _position_cap(db, cfg, today):
    t = cfg.get("trading", {}) or {}
    cap = int(t.get("max_positions", 10))
    pos = db.get_all_positions(simulated=True)
    n = len(pos)
    day_cap = int(t.get("max_day_positions", cap))
    day_n = sum(1 for p in pos if str(p.get("trade_mode") or "").upper() == "DAY")
    m = {"open": n, "cap": cap, "day_open": day_n, "day_cap": day_cap}
    pct = 100 * n / cap if cap else 0
    if pct >= 95 or (day_cap and day_n >= day_cap):
        return "adjust", f"book at {n}/{cap}, DAY {day_n}/{day_cap}: raise the cap or rotate more", m
    return "ok", f"{n}/{cap} positions used ({pct:.0f}%), DAY {day_n}/{day_cap}", m


def _second_opinion(db, cfg, today):
    from engine.second_opinion import outcome_stats
    from engine.swing_experiment import round_trips
    st = outcome_stats(db.get_second_opinions(), round_trips(_paper_trades(db)))
    m = {k: {"n": v["n"], "avg_pct": round(v["avg_pct"], 2), "win_pct": round(v["win_pct"])} for k, v in st.items()}
    total = sum(v["n"] for v in st.values())
    if total < 20 or len(st) < 2 or min(v["n"] for v in st.values()) < 8:
        return "waiting", f"{total}/20 matched trades (need 8+ in each verdict group)", m
    best = max(st, key=lambda k: st[k]["avg_pct"]); worst = min(st, key=lambda k: st[k]["avg_pct"])
    return "ready", f"best verdict {best} ({st[best]['avg_pct']:+.2f}%), worst {worst} ({st[worst]['avg_pct']:+.2f}%): decide if it vetoes", m


def _risk_sizing(db, cfg, today):
    from engine.swing_experiment import round_trips
    rts = [r for r in round_trips(_paper_trades(db)) if str(r["entry"])[:10] >= SINCE]
    m = {"round_trips": len(rts)}
    if len(rts) < 30:
        return "waiting", f"{len(rts)}/30 trades sized since {SINCE}", m
    g = sum(r["pnl"] for r in rts if r["pnl"] > 0); l = -sum(r["pnl"] for r in rts if r["pnl"] < 0)
    pf = g / l if l else None
    m.update(profit_factor=round(pf, 2) if pf else None, total_pnl=round(sum(r["pnl"] for r in rts), 2))
    return ("ok" if pf and pf >= 1.0 else "adjust"), f"{len(rts)} trades, $ profit factor {('%.2f' % pf) if pf else 'n/a'}", m


def _entry_quality(db, cfg, today):
    """Closed paper positions entered since the bar change: profit factor by score band and by regime."""
    with db._conn() as c:
        cur = c.execute("SELECT entry_signal_score, entry_regime, entry_price, exit_price FROM positions "
                        "WHERE status = 'closed' AND simulated = 1 AND exit_price > 0 AND entry_price > 0 AND entry_time >= ?",
                        (SINCE,))
        rows = cur.fetchall()
    m = {"closed_since_change": len(rows)}
    if len(rows) < 40:
        return "waiting", f"{len(rows)}/40 trades closed since {SINCE}", m

    def pf(xs):
        g = sum(x for x in xs if x > 0); l = -sum(x for x in xs if x < 0)
        return (g / l) if l else None
    rets = [((x[3] / x[2]) - 1) * 100 for x in rows]
    low = [r for r, x in zip(rets, rows) if (x[0] or 0) <= 70]
    chop = [r for r, x in zip(rets, rows) if x[1] == "CHOPPY"]
    m.update(pf_all=pf(rets), n_low=len(low), pf_low=pf(low), n_choppy=len(chop), pf_choppy=pf(chop))
    msgs, adjust = [], False
    if len(low) >= 10 and (pf(low) or 0) < 0.8:
        msgs.append(f"scores <=70 still lose (PF {pf(low):.2f}, n={len(low)}): raise the bar again"); adjust = True
    if len(chop) >= 10 and (pf(chop) or 0) < 0.8:
        msgs.append(f"CHOPPY entries still lose (PF {pf(chop):.2f}, n={len(chop)}): add a CHOPPY surcharge"); adjust = True
    if not msgs:
        msgs.append(f"overall PF {pf(rets):.2f} over {len(rets)} trades: the bar is holding up" if pf(rets) else "no losing trades yet")
    return ("adjust" if adjust else "ok"), "; ".join(msgs), m


def _sleeves_vs_spy(db, cfg, today):
    from storage.sleeve_ledger import SleeveLedger
    out, m, waiting = [], {}, False
    for name, prefix in (("sector", "sm"), ("stock", "stk"), ("low-vol", "stl")):
        log = SleeveLedger(db, prefix).get_sm_equity_log()
        if len(log) < 60:
            waiting = True
            m[name] = f"{len(log)}/60 marks"
            continue
        a = (log[-1]["total_value"] / log[0]["total_value"] - 1) * 100
        s = (log[-1]["spy_close"] / log[0]["spy_close"] - 1) * 100
        m[name] = {"return": round(a, 2), "spy": round(s, 2)}
        out.append((name, a - s))
    if waiting and not out:
        return "waiting", "; ".join(f"{k} {v}" for k, v in m.items()), m
    lag = [n for n, d in out if d < -5]
    txt = ", ".join(f"{n} {d:+.1f} pts" for n, d in out)
    return ("adjust" if lag else "ok"), f"vs SPY: {txt}", m


CHECKS = {"swing_forward_test": _swing_forward_test, "risk_cap_blocks": _risk_cap_blocks, "rotation": _rotation,
          "position_cap": _position_cap, "second_opinion": _second_opinion, "risk_sizing": _risk_sizing, "entry_quality": _entry_quality,
          "sleeves_vs_spy": _sleeves_vs_spy}


def run_all(db, cfg: dict, today: date | None = None) -> list[dict]:
    """Run every check, log the result, return rows with `changed` set when the status moved."""
    today = today or date.today()
    prev = {r["item_id"]: r["status"] for r in db.latest_reevals()}
    rows = []
    for it in ITEMS:
        try:
            status, headline, metrics = CHECKS[it["id"]](db, cfg, today)
        except Exception as e:
            logger.warning(f"re-evaluation {it['id']} failed: {e}")
            status, headline, metrics = "waiting", f"check could not run ({e})", {}
        db.log_reeval(it["id"], status, headline, json.dumps(metrics, default=str))
        rows.append({**it, "status": status, "headline": headline, "metrics": metrics,
                     "changed": prev.get(it["id"]) != status and status in ("ready", "adjust")})
    return rows


def snapshot(db) -> list[dict]:
    """Latest stored result per item (what the UI shows); items never checked show as waiting."""
    latest = {r["item_id"]: r for r in db.latest_reevals()}
    out = []
    for it in ITEMS:
        r = latest.get(it["id"])
        out.append({**it, "status": r["status"] if r else "waiting",
                    "headline": r["headline"] if r else "not checked yet",
                    "checked_at": r["checked_at"] if r else None})
    return out
