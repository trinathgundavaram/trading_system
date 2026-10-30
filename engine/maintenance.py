"""Daily housekeeping for an unattended engine: keep the drift reference in step with the config, prune stale
files, and tell you ONLY when something is wrong. Everything here is best-effort and never touches trading."""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

CACHE_MAX_AGE_DAYS = 14
PROMPT_MAX_AGE_DAYS = 3
LOG_ERROR_LIMIT = 50            # ERROR/CRITICAL lines in the last 24 h before it is worth a message
OUTPUT_MAX_GB = 2.0


def reference_is_stale(cfg: dict) -> bool:
    from engine.sleeve_drift import load_reference
    sc = (cfg or {}).get("sector_momentum_sleeve", {}) or {}
    try:
        meta, _ = load_reference()
    except Exception:
        return True
    return (abs(float(meta.get("leverage", 0)) - float(sc.get("leverage", 1.0))) > 1e-9
            or abs(float(meta.get("band", 0)) - float(sc.get("band_pct", 0.0))) > 1e-9)


def ensure_reference(cfg: dict) -> str | None:
    """Rebuild the backtest reference when leverage/band changed. Returns a message if it rebuilt (or failed)."""
    if not reference_is_stale(cfg):
        return None
    try:
        r = subprocess.run([sys.executable, str(BASE / "scripts" / "build_sleeve_reference.py")],
                           capture_output=True, text=True, timeout=900, cwd=str(BASE))
        if r.returncode == 0:
            return "drift reference rebuilt for the new leverage/band"
        return f"drift reference rebuild FAILED: {(r.stderr or r.stdout)[-200:]}"
    except Exception as e:
        return f"drift reference rebuild FAILED: {e}"


def _prune_dir(path: Path, max_age_days: int) -> int:
    if not path.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    for f in path.rglob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def prune_files() -> int:
    out = BASE / "output"
    return _prune_dir(out / "cache", CACHE_MAX_AGE_DAYS) + _prune_dir(out / "pending_prompts", PROMPT_MAX_AGE_DAYS)


ALERT_MAX_AGE_DAYS = 2


def resolve_stale_alerts(db, max_age_days: int = ALERT_MAX_AGE_DAYS) -> int:
    """Auto-dismiss MEDIUM-or-lower monitoring alerts older than `max_age_days`. They are per-ticker data hiccups
    and real-book sync notes that used to pile up until someone clicked Dismiss. HIGH/CRITICAL stay until a human
    resolves them."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    n = 0
    for a in db.get_open_alerts():
        if str(a.get("severity", "")).upper() in ("HIGH", "CRITICAL"):
            continue
        if str(a.get("triggered_at", "")) < cutoff:
            db.resolve_alert(a["id"], "auto-dismissed: older than %d days" % max_age_days)
            n += 1
    return n


def _last_business_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def health_problems(db, cfg: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    problems: list[str] = []
    try:
        from engine.strategy_overview import scheduler_health
        sh = scheduler_health(db, cfg)
        if sh.get("status") == "stale":
            problems.append("swing scheduler has no recent cycle")
    except Exception as e:
        problems.append(f"could not read scheduler health ({e})")
    # Sleeve marks: the latest should be the previous business day or later (today's runs after the close).
    try:
        from storage.sleeve_ledger import SleeveLedger
        want = str(_last_business_day(today))
        for name, prefix, key in (("sector momentum", "sm", "sector_momentum_sleeve"),
                                  ("stock momentum", "stk", "stock_momentum_sleeve"),
                                  ("stock low-vol", "stl", "stock_lowvol_sleeve")):
            if not (cfg.get(key, {}) or {}).get("paper_enabled", False):
                continue
            log = SleeveLedger(db, prefix).get_sm_equity_log()
            last = max((r["as_of_date"] for r in log), default=None)
            if last is None or last < want:
                problems.append(f"{name} sleeve has no daily mark since {last or 'ever'}")
    except Exception as e:
        problems.append(f"could not check sleeve marks ({e})")
    # Errors in the scheduler log, last 24 h.
    try:
        log = BASE / "output" / "logs" / "scheduler.log"
        if log.exists():
            since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
            n = 0
            with open(log, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line[:16] >= since and (" ERROR " in line or " CRITICAL " in line):
                        n += 1
            if n > LOG_ERROR_LIMIT:
                problems.append(f"{n} errors in the scheduler log in the last 24 h")
    except Exception:
        pass
    try:
        size = sum(f.stat().st_size for f in (BASE / "output").rglob("*") if f.is_file()) / 1e9
        if size > OUTPUT_MAX_GB:
            problems.append(f"output/ is {size:.1f} GB")
    except Exception:
        pass
    return problems


def swing_decision_due(db, cfg: dict, today: date | None = None) -> str | None:
    """On/after swing_experiment.decision_date, once, return the verdict message (the caller sends it)."""
    from engine.strategy_overview import swing_experiment_status
    today = today or date.today()
    ex = swing_experiment_status(db, cfg, today)
    if not ex or not ex.get("decision_date") or today < date.fromisoformat(ex["decision_date"]):
        return None
    if db.get_sm_state("swing_decision_sent") == ex["decision_date"]:
        return None
    pf = ex.get("profit_factor")
    verdict = ex["verdict"]
    advice = {"pass": "it passed - sizing steps up automatically while it keeps passing",
              "fail": "it FAILED the bar - consider switching the swing engine off (Control tab)",
              "collecting": "still collecting: too few entry days to judge - extend the date or switch it off"}[verdict]
    db.set_sm_state("swing_decision_sent", ex["decision_date"])
    return (f"Swing forward test decision day: {ex['entry_days']}/{ex['needs_days']} entry days, "
            f"profit factor {('%.2f' % pf) if pf else 'n/a'} (needs {ex['needs_pf']}); {advice}.")


def weekly_digest(db, cfg: dict, today: date | None = None) -> str | None:
    """One short paragraph on Sundays: how each strategy did this week versus SPY, drift, readiness. None on other days."""
    today = today or date.today()
    if today.weekday() != 6:
        return None
    if db.get_sm_state("digest_sent") == str(today):
        return None
    from engine import strategy_overview as so
    d = so.build(db, cfg)
    cur = d.get("curves") or {}

    def wk(key):
        v = cur.get(key) or []
        if len(v) < 2:
            return None
        base = v[max(0, len(v) - 6)]                 # ~5 trading days back
        return v[-1] / base - 1
    parts = []
    for key, label in (("sleeve", "Sector"), ("stock", "Stock mom"), ("stock_lowvol", "Low-vol"), ("spy", "SPY"), ("baseline", "SPY+rule")):
        w = wk(key)
        if w is not None:
            parts.append(f"{label} {w:+.1%}")
    r = d.get("readiness") or {}
    drift = (d.get("drift") or {}).get("status", "n/a")
    msg = f"Week: {' | '.join(parts) or 'collecting data'}. Drift: {drift}. Live readiness {r.get('passed', '?')}/{r.get('total', '?')}."
    db.set_sm_state("digest_sent", str(today))
    return msg


def backup_database() -> str | None:
    """Nightly pg_dump; returns an error string on failure (it is then reported with the other problems)."""
    try:
        sys.path.insert(0, str(BASE))
        from scripts.backup_db import run_backup
        run_backup()
        return None
    except Exception as e:
        return f"database backup FAILED: {e}"


def run(db, cfg: dict) -> dict:
    """The scheduled job body. Returns what it did (for the log and tests)."""
    result = {"reference": ensure_reference(cfg), "pruned": prune_files(), "alerts_cleared": resolve_stale_alerts(db),
              "problems": health_problems(db, cfg)}
    result["backup"] = backup_database()
    try:
        digest = weekly_digest(db, cfg)
        if digest:
            from engine.notifications import notify
            notify(cfg, title="Weekly digest", message=digest, subtitle="Trading Platform", severity="summary")
            result["digest"] = digest
    except Exception as e:
        logger.warning(f"weekly digest failed: {e}")
    try:
        msg = swing_decision_due(db, cfg)
        if msg:
            from engine.notifications import notify
            notify(cfg, title="Swing engine: decision day", message=msg, subtitle="Forward test", severity="critical")
            result["swing_decision"] = msg
    except Exception as e:
        logger.warning(f"swing decision check failed: {e}")
    try:
        from engine import reevaluation
        rows = reevaluation.run_all(db, cfg)
        result["reeval"] = {r["id"]: r["status"] for r in rows}
        flagged = [r for r in rows if r["changed"]]
        if flagged:
            from engine.notifications import notify
            notify(cfg, title="Items ready to re-evaluate",
                   message="; ".join(f"{r['title']}: {r['headline']}" for r in flagged)[:400],
                   subtitle="Re-evaluation tracker", severity="summary")
    except Exception as e:
        logger.warning(f"re-evaluation tracker failed: {e}")
    try:
        from engine import baseline
        result["baseline_days"] = baseline.refresh(db, cfg)
    except Exception as e:
        result["baseline_days"] = f"skipped: {e}"
    msgs = [m for m in (result["reference"], result["backup"]) if m and "FAILED" in m] + result["problems"]
    if msgs:
        try:
            from engine.notifications import notify
            notify(cfg, title="Trading Platform needs a look", message="; ".join(msgs)[:400],
                   subtitle="Daily health check", severity="critical")
        except Exception as e:
            logger.warning(f"maintenance notification failed: {e}")
    logger.info(f"maintenance: {result}")
    return result
