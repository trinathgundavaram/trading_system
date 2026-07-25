#!/usr/bin/env python3
"""Is Phase 2 actually in force, on this machine, right now?

READ-ONLY. The companion to verify_phase1.py, and it exists for the same
reason: "implemented" and "in force" are different claims, and the 2026-07-25
incident happened in the gap between them. A guard that is present in the
source but disabled in config, or a migration that is written but never
applied, reads as done in a code review and does nothing at runtime.

Checks §7 through §18 against the code AND the live database AND config.
Every check prints PASS / FAIL / WARN and a reason. Exits non-zero on any FAIL,
so it can gate a release.

    python3 scripts/verify_phase2.py
    python3 scripts/verify_phase2.py --no-db      # skip database checks

Deliberately NOT a replacement for the test suite. Tests prove the code is
correct; this proves the correct code is the code that is running here.
"""
from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    tag = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
    print(f"  [{tag}] {name}" + (f"\n         {detail}" if detail else ""))


def section(title):
    print(f"\n{'─' * 74}\n{title}\n{'─' * 74}")


def src(rel):
    try:
        return (REPO / rel).read_text()
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-db", action="store_true",
                    help="skip checks that need a database connection")
    args = ap.parse_args()

    try:
        from config_loader import load_config_dict
        cfg = load_config_dict()
    except Exception as e:
        check("config loads", False, str(e))
        return 1

    risk = cfg.get("risk", {}) or {}
    prcfg = cfg.get("portfolio_risk", {}) or {}

    # ── §7/§8/§9/§10 - the risk controls have real inputs ───────────────────
    section("§7-§10 - the daily budget, the loss limit and the breaker")
    check("max_daily_loss_pct is set",
          float(risk.get("max_daily_loss_pct", 0) or 0) > 0,
          f"{risk.get('max_daily_loss_pct')!r} - without it the limit is the "
          f"absolute $ figure alone, which on a $1,000 account is half the book")
    try:
        from rules.risk_rules import RiskEngine, daily_loss_limit
        from storage.database import Database
        db = None if args.no_db else Database()
        if db is not None:
            lim = daily_loss_limit(db, cfg, simulated=True)
            check("the resolved paper loss limit is plausible",
                  0 < lim <= float(risk.get("max_daily_loss_usd", 500)),
                  f"${lim:.2f} (tighter of {risk.get('max_daily_loss_pct')}% "
                  f"and ${risk.get('max_daily_loss_usd')})")
    except Exception as e:
        check("§8 limit resolves", False, str(e))

    pt = src("engine/paper_trader.py")
    check("§10 - the risk gate is inside execute_buy, not only per cycle",
          "RiskEngine(db, cfg, simulated=True).check()" in pt)
    check("§10 - execute_sell has NO risk gate",
          "DELIBERATELY HAS NO RISK-ENGINE CHECK" in pt,
          "being unable to close a losing position because the daily trade "
          "count is spent turns the limit into a risk")

    # ── §11 - drawdown ──────────────────────────────────────────────────────
    section("§11 - drawdown is computed, persisted and binding")
    check("intraday drawdown cap is set",
          float(risk.get("max_intraday_drawdown_pct", 0) or 0) > 0,
          repr(risk.get("max_intraday_drawdown_pct")))
    check("running drawdown cap is set",
          float(risk.get("max_running_drawdown_pct", 0) or 0) > 0,
          repr(risk.get("max_running_drawdown_pct")))
    try:
        from rules.risk_rules import RiskEngine as _RE
        check("the drawdown gate is wired into RiskEngine.check",
              "drawdown_breach" in inspect.getsource(_RE.check))
        from rules.risk_rules import trip_kill_switch_if_needed as _trip
        check("a running-drawdown breach escalates to the kill switch",
              "_running_drawdown_breach" in inspect.getsource(_trip))
    except Exception as e:
        check("§11 wiring", False, str(e))
    check("the equity writer recomputes drawdown",
          "update_drawdown" in src("storage/database.py").split(
              "def record_paper_equity")[-1][:2000],
          "a separate job is a second thing that must be running for a risk "
          "control to be current")

    # ── §14 - the position-opening race ─────────────────────────────────────
    section("§14 - opening a position is one transaction")
    dbsrc = src("storage/database.py")
    check("try_open_position exists", "def try_open_position" in dbsrc)
    check("try_debit_paper_cash exists", "def try_debit_paper_cash" in dbsrc)
    check("the cap is held with an advisory lock",
          "pg_advisory_xact_lock" in dbsrc,
          "FOR UPDATE cannot hold at 0-of-N, which is where every day starts")
    check("paper_trader opens through try_open_position",
          "db.try_open_position(" in pt)
    check("paper_trader no longer calls open_position for a new entry",
          "db.open_position(ticker, price, shares, amount" not in pt)

    # ── §15 - learning data ─────────────────────────────────────────────────
    section("§15 - the learning tables are quarantined")
    check("reads filter on data_quality",
          dbsrc.count("data_quality, 'ok') = 'ok'") >= 3,
          f"{dbsrc.count(chr(34) if False else 'data_quality')} mentions")
    check("close_position returns hold_hours",
          '"hold_hours": hold_hours' in dbsrc,
          "one definition, so the ledger and the learning table cannot "
          "disagree the way they did for ADPT")
    for mod in ("engine/paper_trader.py", "engine/live_trader.py", "confirm_fill.py"):
        s = src(mod)
        check(f"{mod} uses close_position's hold_hours",
              'closed.get("hold_hours"' in s or 'closed["hold_hours"]' in s)
    check("the write-time classifier exists",
          "_classify_quality" in src("engine/mae_mfe_engine.py"),
          "a cleanup that only runs once has to be run again")
    check("scripts/reconcile.py exists", (REPO / "scripts/reconcile.py").exists())

    # ── §16 - cross-book writes ─────────────────────────────────────────────
    section("§16 (E-9) - no by-ticker write crosses the books")
    check("update_position_by_ticker requires a book",
          "update_position_by_ticker requires simulated" in dbsrc)
    check("update_trail_high requires a book",
          "update_trail_high requires simulated" in dbsrc)
    unscoped = []
    for mod in ("engine/paper_trader.py", "engine/live_trader.py",
                "confirm_fill.py", "scheduler.py"):
        s = src(mod)
        for line_no, line in enumerate(s.splitlines(), 1):
            call = ("db.update_position_by_ticker(" in line
                    or "db.update_trail_high(" in line)
            if call and "simulated=" not in line and not line.strip().startswith("#"):
                # Multi-line calls carry the kwarg on a later line; only flag a
                # call that both opens and closes on this one.
                if line.count("(") == line.count(")"):
                    unscoped.append(f"{mod}:{line_no}")
    check("every call site passes simulated=", not unscoped,
          "\n         ".join(unscoped))

    # ── §18 - portfolio risk ────────────────────────────────────────────────
    section("§18 - portfolio risk is measured and binding")
    check("hard_block_on_severe_breach is on",
          prcfg.get("hard_block_on_severe_breach") is True,
          repr(prcfg.get("hard_block_on_severe_breach")))
    check("severe_breach_multiple is set",
          float(prcfg.get("severe_breach_multiple", 0) or 0) > 1.0,
          repr(prcfg.get("severe_breach_multiple")))
    check("UNCLASSIFIED has its own cap",
          float(prcfg.get("max_unclassified_exposure_pct", 0) or 0) > 0,
          repr(prcfg.get("max_unclassified_exposure_pct")))
    prsrc = src("engine/portfolio_risk.py")
    check("themes fall back to sector/industry",
          "SECTOR:" in prsrc and "INDUSTRY:" in prsrc,
          "the hand-maintained map left ~95% of traded names themeless")
    sched = src("scheduler.py")
    check("scheduler HONOURS portfolio_risk.allowed",
          "not portfolio_risk_result.allowed" in sched,
          "it was computed and ignored - a measured limit that does not bind "
          "is documentation")
    check("rejections are recorded",
          "_log_rejected(" in sched,
          "rejected_signals held 0 rows against portfolio_risk_log's 244")

    # ── database state ──────────────────────────────────────────────────────
    if not args.no_db:
        section("DATABASE - are the migrations actually applied here?")
        try:
            from storage.database import Database
            db = Database()
            with db._conn() as conn:
                cols = {r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'daily_stats'").fetchall()}
                for c in ("paper_max_drawdown", "paper_running_drawdown"):
                    check(f"daily_stats.{c} exists (005)", c in cols)

                idx = {r[0] for r in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'positions'"
                ).fetchall()}
                check("uq_open_position_per_ticker_book exists (006)",
                      "uq_open_position_per_ticker_book" in idx,
                      "without it the duplicate race is still open - check the "
                      "CRITICAL log line at startup for pre-existing duplicates")

                for tbl in ("mae_mfe_data", "pattern_database"):
                    c = {r[0] for r in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = %s", (tbl,)).fetchall()}
                    check(f"{tbl}.data_quality exists (007)", "data_quality" in c)

                rc = {r[0] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'rejected_signals'").fetchall()}
                check("rejected_signals.would_have_size exists (008)",
                      "would_have_size" in rc)

                dupes = conn.execute(
                    "SELECT COUNT(*) FROM (SELECT ticker FROM positions "
                    "WHERE status='open' GROUP BY ticker, COALESCE(simulated,0) "
                    "HAVING COUNT(*) > 1) d").fetchone()[0]
                check("no duplicate open positions", dupes == 0, f"{dupes} found")

                quarantined = conn.execute(
                    "SELECT COUNT(*) FROM mae_mfe_data "
                    "WHERE COALESCE(data_quality,'ok') <> 'ok'").fetchone()[0]
                clean = conn.execute(
                    "SELECT COUNT(*) FROM mae_mfe_data "
                    "WHERE COALESCE(data_quality,'ok') = 'ok'").fetchone()[0]
                # The column existing proves only that the SCHEMA half of 007
                # ran - storage/database.py self-heals that on every startup.
                # The sweep is the data half and lives solely in the .sql file,
                # so it has to be applied by hand and can be missed.
                #
                # Distinguishing the two states matters. "0 quarantined, 30
                # clean" against a table known to contain a ticker called AAA
                # means the sweep has NOT run, and reporting that as the
                # expected end state would have been the reassuring answer to
                # a question nobody had asked properly.
                stale = conn.execute(
                    """SELECT COUNT(*) FROM mae_mfe_data
                        WHERE COALESCE(data_quality,'ok') = 'ok'
                          AND (hold_hours < 0.01
                               OR (COALESCE(mae_pct,0) = 0 AND COALESCE(mfe_pct,0) = 0
                                   AND COALESCE(outcome_pct,0) <> 0)
                               OR recorded_at < '2026-07-20T00:00:00')"""
                ).fetchone()[0]
                if stale:
                    check("migration 007's sweep has been applied", False,
                          f"{stale} row(s) still marked 'ok' that the sweep "
                          f"would quarantine ({quarantined} quarantined, "
                          f"{clean} clean). The COLUMN exists because "
                          f"storage/database.py self-heals the schema; the "
                          f"DATA half only runs when you apply the file:\n"
                          f"           psql \"$POSTGRES_DB\" -f "
                          f"migrations/007_learning_data_quarantine.sql")
                else:
                    check("migration 007's sweep has been applied", None,
                          f"{quarantined} quarantined, {clean} clean. Near-zero "
                          f"clean rows is the expected end state - it means "
                          f"there is no untainted learning data yet, which is "
                          f"true and is not a number to make go up.")
        except Exception as e:
            check("database checks ran", None,
                  f"{str(e)[:200]}\n         (run without --no-db against the "
                  f"real database to verify the migrations)")

    # ── summary ─────────────────────────────────────────────────────────────
    fails = [n for n, ok, _ in _results if ok is False]
    warns = [n for n, ok, _ in _results if ok is None]
    print(f"\n{'=' * 74}")
    print(f"{len(_results)} checks: {len(_results) - len(fails) - len(warns)} pass, "
          f"{len(fails)} FAIL, {len(warns)} warn")
    for n in fails:
        print(f"  FAIL  {n}")
    for n in warns:
        print(f"  WARN  {n}")
    print("=" * 74)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
