"""The bar a paper strategy must clear before real money is even discussed. Fixed rules, evaluated from live data,
so the decision is a checklist and not a mood. Nothing here enables trading: live gates stay where they are.

Automatic checks come from the overview data; two are manual acknowledgements you flip in config.yaml
(`live_readiness.*`) once they are genuinely true."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

MIN_MARKS = 63            # ~3 months of daily marks
BASE = Path(__file__).resolve().parent.parent


def _backup_age_days() -> float | None:
    d = BASE / "output" / "backups"
    files = sorted(d.glob("*.dump")) if d.is_dir() else []
    if not files:
        return None
    import time
    return (time.time() - files[-1].stat().st_mtime) / 86400


def evaluate(overview: dict, cfg: dict) -> dict:
    lr = (cfg or {}).get("live_readiness", {}) or {}
    stats = overview.get("stats") or {}
    drift = (overview.get("drift") or {}).get("status")
    sched = (overview.get("scheduler") or {}).get("status")
    ex = overview.get("experiment") or {}
    curves = overview.get("curves") or {}
    days = stats.get("days") or 0
    sleeve_tot, bl = None, curves.get("baseline") or []
    sl = curves.get("sleeve") or []
    if len(sl) > 1 and len(bl) > 1:
        sleeve_tot, bl_tot = sl[-1] / sl[0] - 1, bl[-1] / bl[0] - 1
    age = _backup_age_days()
    checks = [
        {"name": f"{MIN_MARKS}+ daily marks tracked", "ok": days >= MIN_MARKS, "detail": f"{days} so far", "manual": False},
        {"name": "Drift monitor is 'On track'", "ok": drift == "ok", "detail": str(drift or "n/a"), "manual": False},
        {"name": "Beats SPY + 200-day rule at the same leverage", "ok": bool(sleeve_tot is not None and sleeve_tot > bl_tot),
         "detail": "n/a" if sleeve_tot is None else f"{sleeve_tot:+.1%} vs {bl_tot:+.1%}", "manual": False},
        {"name": "Scheduler healthy", "ok": sched in ("ok", "running"), "detail": str(sched or "n/a"), "manual": False},
        {"name": "Nightly backup is fresh (< 2 days)", "ok": age is not None and age < 2,
         "detail": "no backup" if age is None else f"{age:.1f} days old", "manual": False},
        {"name": "Swing engine decided (passed or switched off)", "ok": ex.get("verdict") == "pass" or bool(lr.get("swing_decision_made")),
         "detail": str(ex.get("verdict", "n/a")), "manual": False},
        {"name": "Drawdown plan written down", "ok": bool(lr.get("drawdown_plan_written")), "detail": "set live_readiness.drawdown_plan_written", "manual": True},
        {"name": "Runs on an always-on machine", "ok": bool(lr.get("always_on_host")), "detail": "set live_readiness.always_on_host", "manual": True},
    ]
    n_ok = sum(c["ok"] for c in checks)
    return {"checks": checks, "passed": n_ok, "total": len(checks), "ready": n_ok == len(checks)}
