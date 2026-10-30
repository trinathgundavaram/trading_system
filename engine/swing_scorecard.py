"""Report card for the swing engine, built from real paper round trips (not simulated pattern outcomes).

Answers "is it working, and where does it win or lose?": overall result, then the same numbers split by trade mode,
hold time, exit source and the free second-opinion verdict. All pure functions over trade rows."""
from __future__ import annotations

from datetime import datetime

from engine.swing_experiment import round_trips

HOLD_BUCKETS = [("under 1 day", 0, 1), ("1-3 days", 1, 3), ("3-6 days", 3, 6), ("6+ days", 6, 10_000)]
EXIT_NAMES = {"price_watch": "Live stop / price watch", "loop_b_urgent": "Exit engine (urgent)", "sell_rules": "Sell rules",
              "rotation": "Rotation swap", "eod_flatten": "End-of-day flatten", "day_hold": "Day-hold exit"}


def _stats(pcts: list, usd: list) -> dict:
    n = len(pcts)
    if not n:
        return {"n": 0, "avg_pct": None, "win_pct": None, "pf": None, "usd": 0.0}
    gains = sum(p for p in pcts if p > 0)
    losses = -sum(p for p in pcts if p < 0)
    return {"n": n, "avg_pct": sum(pcts) / n, "win_pct": 100 * sum(1 for p in pcts if p > 0) / n,
            "pf": (gains / losses) if losses > 0 else None, "usd": sum(usd)}


def _hold_days(rt: dict) -> float:
    return (datetime.fromisoformat(str(rt["exit"])) - datetime.fromisoformat(str(rt["entry"]))).total_seconds() / 86400


def _exit_kind(reason: str) -> str:
    head = (reason or "").split(":")[0].split(" ")[0].strip().lower()
    for key, name in EXIT_NAMES.items():
        if head.startswith(key):
            return name
    return "Other"


def _group(rts: list, keyfn, order: list | None = None) -> list:
    groups: dict = {}
    for r in rts:
        k = keyfn(r)
        if k is not None:
            groups.setdefault(k, []).append(r)
    keys = order if order else sorted(groups, key=lambda k: -len(groups[k]))
    return [{"name": k, **_stats([r["pnl_pct"] for r in groups[k]], [r["pnl"] for r in groups[k]])} for k in keys if k in groups]


def build(trades: list, opinions: list | None = None, experiment: dict | None = None) -> dict:
    rts = round_trips(trades)
    overall = _stats([r["pnl_pct"] for r in rts], [r["pnl"] for r in rts])
    recent = rts[-30:]
    recent_stats = _stats([r["pnl_pct"] for r in recent], [r["pnl"] for r in recent])

    def hold_bucket(r):
        d = _hold_days(r)
        return next((n for n, a, b in HOLD_BUCKETS if a <= d < b), None)

    out = {
        "overall": overall, "recent": recent_stats,
        "by_mode": _group(rts, lambda r: (r.get("mode") or "?").upper()),
        "by_hold": _group(rts, hold_bucket, [n for n, _, _ in HOLD_BUCKETS]),
        "by_exit": _group(rts, lambda r: _exit_kind(r.get("reason", ""))),
    }
    if opinions:
        from engine.second_opinion import outcome_stats
        out["by_opinion"] = [{"name": k, "n": v["n"], "avg_pct": v["avg_pct"], "win_pct": v["win_pct"], "pf": None, "usd": 0.0}
                             for k, v in outcome_stats(opinions, rts).items()]
    ex = experiment or {}
    pf = overall["pf"]
    if ex.get("verdict") == "pass":
        verdict = ("Passing", "good", "Forward test passed: sizing steps up automatically while it keeps passing.")
    elif ex.get("verdict") == "fail":
        verdict = ("Failing", "bad", "Forward test failed the bar - consider switching the swing engine off.")
    elif overall["n"] >= 30 and pf is not None and pf < 0.9:
        verdict = ("Not profitable", "bad", "Losing on average across all closed trades; the forward test is still collecting.")
    elif overall["n"] >= 30 and pf is not None and pf < 1.3:
        verdict = ("Unproven", "warn", "About break-even so far (profit factor %.2f, needs 1.3). The forward test decides." % pf)
    else:
        verdict = ("Unproven", "warn", "Too few independent entry days to judge; the forward test is still collecting.")
    out["verdict"] = {"label": verdict[0], "tone": verdict[1], "text": verdict[2]}
    return out
