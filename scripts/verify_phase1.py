#!/usr/bin/env python3
"""Is Phase 1 actually in force, on this machine, right now?

READ-ONLY. Checks the four Phase 1 sections plus §2 and S-1, against the code
AND the live database AND the running process - because "implemented" and "in
force" are different claims, and the 2026-07-25 incident happened in the gap
between them.

Every check prints PASS / FAIL / WARN and a reason. Exits non-zero if any FAIL.

    python3 scripts/verify_phase1.py
    python3 scripts/verify_phase1.py --ui-port 8080     # also probe the UI
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
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
    ap.add_argument("--ui-port", type=int, default=None,
                    help="probe a running UI on this port (auth + bind checks)")
    args = ap.parse_args()

    # ── version identity ────────────────────────────────────────────────────
    section("VERSION - is the code here the code that is tagged and running?")
    try:
        from storage.version import app_version, is_release_build
        ver = app_version()
        dirty = ver.endswith("-dirty")
        exact = is_release_build()
        check("working tree is clean", not dirty,
              ver if not dirty else f"{ver} - uncommitted changes are running")
        check("HEAD is exactly a release tag", True if exact else None,
              f"app_version() = {ver}" + ("" if exact else
              "\n         Not a defect while in paper mode, but §32 refuses to arm"
              "\n         live execution from a non-release build."))
    except Exception as e:
        check("version resolves", False, str(e))

    # ── §2 live gates ───────────────────────────────────────────────────────
    section("§2 - live execution is disarmed and cannot self-arm")
    try:
        from config_loader import load_config_dict
        cfg = load_config_dict()
        t = cfg.get("trading", {})
        check("auto_trade is false", t.get("auto_trade") is False, repr(t.get("auto_trade")))
        check("watch_execute is WATCH",
              str(t.get("watch_execute")).upper() == "WATCH", repr(t.get("watch_execute")))
        check("live_execution_enabled is false",
              t.get("live_execution_enabled") is False, repr(t.get("live_execution_enabled")))

        from engine import live_trader as lt
        ok_v, why = lt._validation_current()
        check("is_live_mode() is False", lt.is_live_mode(cfg) is False)
        check("validation receipt gate blocks arming", ok_v is False, why)
        # Read the actual function body rather than a fixed slice of the file -
        # the first version of this check windowed 600 chars past the `def` and
        # landed inside the docstring, reporting a false FAIL.
        import inspect
        body = inspect.getsource(lt.is_live_mode)
        check("receipt guard is wired into is_live_mode",
              "_validation_current()" in body)
    except Exception as e:
        check("§2 checks ran", False, str(e))

    # ── §4 UI auth ──────────────────────────────────────────────────────────
    section("§4 - UI auth surface")
    server_src = src("server.py")
    check("no inline token comparisons remain",
          "!= _auth_token()" not in server_src)
    check("constant-time comparison in use", "hmac.compare_digest" in server_src)
    check("lockout implemented", "_throttle" in server_src and "429" in server_src)
    check("token resolves via storage/secrets.py, not config.yaml",
          "secrets.get(\"UI_AUTH_TOKEN\")" in server_src)
    check("UI binds loopback by default",
          'os.getenv("TP_UI_HOST", "127.0.0.1")' in src("main.py"))
    if os.getenv("TP_UI_HOST") not in (None, "", "127.0.0.1", "localhost"):
        check("TP_UI_HOST override is not set", None,
              f"TP_UI_HOST={os.getenv('TP_UI_HOST')!r} - UI is exposed beyond this machine")

    try:
        import platform as _plat
        from storage import secrets
        tok = secrets.get("UI_AUTH_TOKEN", required=False)
        # storage/secrets.py resolves environment -> .env -> Keychain. The
        # Keychain leg only exists on macOS, so a run from a container or a
        # non-Darwin host sees "unset" even when the token is correctly stored.
        # Report that as a WARN, not a FAIL - a false alarm here trains you to
        # ignore the one finding on this page that actually matters.
        if not tok and _plat.system() != "Darwin":
            check("UI_AUTH_TOKEN is set", None,
                  "not resolvable on this host (no macOS Keychain) - re-run on "
                  "the Mac to check it properly")
        else:
            check("UI_AUTH_TOKEN is set", bool(tok))
        if tok or _plat.system() == "Darwin":
            check("UI_AUTH_TOKEN is a real token (>= 24 chars)",
                  len(tok) >= 24 if tok else False,
                  f"length {len(tok)}" if tok else "unset")
            check("UI_AUTH_TOKEN is not the retired 5-char value",
                  tok != "3nath" if tok else False)
    except Exception as e:
        check("token check ran", None, str(e))

    try:
        import server as _srv
        rows = []
        for r in _srv.app.routes:
            m = (getattr(r, "methods", set()) or set()) & {"POST", "PUT", "PATCH", "DELETE"}
            if not m:
                continue
            names = [d.call.__name__ for d in r.dependant.dependencies
                     if getattr(d, "call", None)]
            rows.append((r.path, "require_token" in names))
        must = {"/api/paper/sell", "/api/real/sell", "/api/portfolio/clear_seed",
                "/api/positions/clear_synced", "/api/live_execution", "/api/config",
                "/api/kill_switch"}
        guarded = {p for p, a in rows if a}
        check("every money/config write route is guarded", must <= guarded,
              f"unguarded: {sorted(must - guarded)}" if must - guarded else
              f"{len(guarded)}/{len(rows)} write routes guarded")
        # v1.2.0 closed the last eight. This is now a FAIL, not a warning: a
        # new unguarded write route is a regression, and the whole point of
        # the require_token dependency is that it cannot be forgotten.
        open_routes = sorted(p for p, a in rows if not a)
        check("no write route is unauthenticated", not open_routes,
              ", ".join(open_routes) if open_routes else f"all {len(rows)} guarded")
    except Exception as e:
        check("route audit ran", None, str(e))

    # ── §5 quarantine ───────────────────────────────────────────────────────
    section("§5 - SYNC/SEED quarantine, three layers")
    dbsrc = src("storage/database.py")
    check("layer 1: get_managed_positions exists", "def get_managed_positions" in dbsrc)
    check("layer 1: is_managed exists", "def is_managed" in dbsrc)
    check("layer 2: sell rules refuse unmanaged",
          "UNMANAGED_TRADE_MODES" in src("rules/sell_rules.py"))
    check("layer 3: execute_sell_live refuses automated unmanaged",
          "unmanaged_sell_blocked" in src("engine/live_trader.py"))
    check("constants have not drifted",
          _constants_agree(), "sell_rules / live_trader / database")

    for path, fn in (("scheduler.py", "price-watch loop"),
                     ("engine/position_management.py", "run_loop_b"),
                     ("engine/rotation.py", "find_rotation_victim")):
        check(f"call site uses managed query: {path} ({fn})",
              "get_managed_positions" in src(path))

    # ── §6 banner ───────────────────────────────────────────────────────────
    section("§6 - runtime posture banner")
    check("storage/banner.py exists", (REPO / "storage" / "banner.py").exists())
    check("printed by main.py", "banner.print_banner" in src("main.py"))
    check("logged by scheduler.py", "banner.log_banner" in src("scheduler.py"))
    check("exposed by /api/status", "execution_posture" in server_src)
    try:
        from storage import banner
        from config_loader import load_config_dict
        p = banner.execution_posture(load_config_dict())
        check("banner reports PAPER", p["mode"].startswith("PAPER"), p["mode"])
        check("banner agrees with live_trader gates",
              p["master_switch"] is False and p["mode"].startswith("PAPER"))
    except Exception as e:
        check("banner renders", False, str(e))

    # ── §17 learning freeze ─────────────────────────────────────────────────
    section("§17 - learning loop frozen, provenance stamped")
    try:
        import yaml
        lc = yaml.safe_load((REPO / "config.yaml").read_text())["learning"]
        check("bayesian_enabled is false", lc.get("bayesian_enabled") is False)
        check("min_trades_before_bayesian is 150",
              lc.get("min_trades_before_bayesian") == 150, repr(lc.get("min_trades_before_bayesian")))
        check("min_pattern_recorded_at set",
              str(lc.get("min_pattern_recorded_at", "")).startswith("2026-07-25"),
              repr(lc.get("min_pattern_recorded_at")))
        check("require_shadow_validation on", lc.get("require_shadow_validation") is True)
        from learning.bayesian_updater import learning_frozen
        frozen, why = learning_frozen({"learning": lc})
        check("learning_frozen() agrees", frozen is True, why)
    except Exception as e:
        check("§17 config checks ran", False, str(e))
    check("provenance stamped on record",
          "config_fingerprint" in src("learning/pattern_database.py")
          and "config_fingerprint" in dbsrc)

    # ── S-1 ─────────────────────────────────────────────────────────────────
    section("S-1 - stop stage does not revert (v1.1.0)")
    try:
        from engine.stop_state_machine import StopState, _calculate_raw, calculate
        pos = dict(entry_price=14.8050, shares=10.0, trade_mode="SWING",
                   risk_per_share=0.09, entry_signal_score=75,
                   stop_state="BREAKEVEN", current_stop_price=14.8095)
        cfg2 = {"risk_level": "TURBO",
                "risk": {"TURBO": {"stop_loss_swing_pct": 8, "stop_loss_day_pct": 4}}}
        td = {"price": 14.8060, "atr": 0.06}
        raw = _calculate_raw(pos, td, 20.0, cfg2).state
        rat = calculate(pos, td, 20.0, cfg2).state
        check("raw stage still regresses (control)", raw is StopState.INITIAL_RISK, raw.value)
        check("ratchet holds the reached stage", rat is StopState.BREAKEVEN, rat.value)
    except Exception as e:
        check("S-1 checks ran", False, str(e))

    # ── database state ──────────────────────────────────────────────────────
    section("DATABASE - is the 2026-07-25 test residue gone?")
    try:
        from storage.database import Database, is_unmanaged_mode
        db = Database()
        pos = db.get_all_positions()
        synthetic = [p for p in pos if p["ticker"] in ("AAA", "BBB", "CCC", "NEW", "FIX")]
        in_window = [p for p in pos if str(p.get("entry_time") or "") >= "2026-07-25T00:00:00"]
        check("no synthetic test tickers in positions", not synthetic,
              f"{len(synthetic)} rows: {sorted({p['ticker'] for p in synthetic})}"
              if synthetic else "")
        check("no positions created in the test window", not in_window,
              f"{len(in_window)} rows - run scripts/repair_test_damage.py --apply"
              if in_window else "")

        with db._conn() as c:
            rot = c.execute("SELECT COUNT(*) FROM rotation_log WHERE victim_ticker IN ('Y','Z')").fetchone()[0]
        check("no fixture rotations in rotation_log", rot == 0, f"{rot} rows" if rot else "")

        # Phase 1 schema
        with db._conn() as c:
            cols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'pattern_database'").fetchall()}
        check("migration 003 applied (pattern provenance)",
              {"engine_version", "config_fingerprint"} <= cols,
              f"missing: {sorted({'engine_version','config_fingerprint'} - cols)}"
              if not {"engine_version", "config_fingerprint"} <= cols else "")
        with db._conn() as c:
            pcols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'positions'").fetchall()}
        check("migration 002 applied (quarantine columns)",
              {"quarantined_stop_price", "quarantined_at"} <= pcols)

        unmanaged_armed = [p for p in pos
                           if is_unmanaged_mode(p.get("trade_mode"))
                           and (p.get("current_stop_price") or 0) > 0]
        check("no quarantined row still carries a live stop", not unmanaged_armed,
              f"{len(unmanaged_armed)} rows - run migrations/002" if unmanaged_armed else "")
    except Exception as e:
        check("database checks ran", None, f"could not reach the database: {e}")

    # ── running process ─────────────────────────────────────────────────────
    section("PROCESS - what is actually running")
    try:
        ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True,
                            text=True, timeout=5).stdout
        procs = [l for l in ps.splitlines()
                 if re.search(r"(scheduler\.py|main\.py)", l) and "grep" not in l]
        if not procs:
            check("scheduler/UI running", None, "nothing running - start with ./service.sh start")
        for l in procs:
            print(f"         {l.strip()[:110]}")
        check("processes inspected", True, f"{len(procs)} found")
    except Exception as e:
        check("process check ran", None, str(e))

    if args.ui_port:
        section(f"UI - live probe on port {args.ui_port}")
        try:
            out = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
                 f"http://127.0.0.1:{args.ui_port}/api/kill_switch"],
                capture_output=True, text=True, timeout=10).stdout.strip()
            check("unauthenticated kill_switch is refused", out in ("403", "429"), f"HTTP {out}")
        except Exception as e:
            check("UI probe ran", None, str(e))
        try:
            lsof = subprocess.run(["lsof", "-nP", f"-iTCP:{args.ui_port}"],
                                  capture_output=True, text=True, timeout=10).stdout
            listen = [l for l in lsof.splitlines() if "LISTEN" in l]
            bad = [l for l in listen if "*:" in l or "0.0.0.0" in l]
            check("UI is bound to loopback only", not bad,
                  "\n         ".join(l.split()[-2] for l in listen) if listen else "not listening")
        except Exception as e:
            check("bind check ran", None, str(e))

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


def _constants_agree() -> bool:
    try:
        from engine.live_trader import UNMANAGED_TRADE_MODES as LT
        from rules.sell_rules import UNMANAGED_TRADE_MODES as SR
        from storage.database import MANAGED_EXCLUDED_MODES as DB
        return set(LT) == set(SR) == set(DB)
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
