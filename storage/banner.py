"""Single source of truth for 'what will this process actually do' (§6).

Five statements in this codebase described a system state that ended on
16 July 2026. They were not stale comments in a dusty corner - they were the
sentences an operator read to decide whether it was safe to leave the machine
running unattended. Correcting those five sentences would have taken ten
minutes and would have been wrong again the next time the configuration
changed.

So this module does not assert anything. It RESOLVES the values and prints
them: the master switch, the two mode flags, the risk level, the kill switch
and the validation receipt, every one read from the live config and from
engine/live_trader.py's own gate functions. A banner derived from the same
functions the execution path calls cannot disagree with behaviour the way
prose did.

Printed at startup by main.py (both modes) and scheduler.py, and returned by
server.py's /api/status so the dashboard shows the same four lines the
terminal does.

Deliberately dependency-light: importing this must never be what pulls the
analysis stack, the Postgres driver or robin_stocks into a process.
engine.live_trader is imported lazily inside the function for that reason.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("trading")


def _section(cfg, key) -> dict:
    """A config section as a dict, whatever the config actually contains.

    Deliberately paranoid. This module is called at startup, before anything
    has validated config.yaml, and on the /api/status path - so a hand-edited
    config with a wrong TYPE (not merely a missing key) must degrade to an
    honest banner, not to a traceback that hides the posture entirely."""
    try:
        section = (cfg or {}).get(key)
    except AttributeError:
        return {}
    return section if isinstance(section, dict) else {}


def execution_posture(cfg: dict) -> dict:
    """Resolved answer to 'can this process place a real order right now'.

    Never raises. A banner that crashes the process it is describing would be
    a spectacularly bad trade, so every lookup is defensive and any failure
    degrades to an explicit 'unknown' rather than to silence.
    """
    t = _section(cfg, "trading")
    try:
        from engine import live_trader
        master = live_trader.is_live_execution_enabled(cfg)
        armed = live_trader.is_live_mode(cfg)
        _, why = live_trader._validation_current()
        force_paper = live_trader.is_force_paper()
    except Exception as e:
        logger.error(f"banner: could not resolve execution posture: {e}")
        master = armed = force_paper = False
        why = f"unknown - posture check failed: {e}"

    if armed:
        mode, colour = "LIVE - REAL ORDERS WILL BE PLACED", "bold red"
    elif master:
        mode, colour = "ARMED but not trading (a gate is closed)", "yellow"
    else:
        mode, colour = "PAPER - no real orders possible", "green"

    return {
        "mode": mode,
        "colour": colour,
        "master_switch": bool(master),
        "watch_execute": str(t.get("watch_execute", "WATCH")).upper(),
        "auto_trade": bool(t.get("auto_trade", False)),
        "risk_level": (cfg or {}).get("risk_level") if isinstance(cfg, dict) else None,
        "kill_switch": bool(_section(cfg, "risk").get("kill_switch_triggered")),
        "force_paper": bool(force_paper),
        "validation": why,
    }


def posture_lines(cfg: dict) -> list[str]:
    """The banner as plain strings, no markup - for log files and the API."""
    p = execution_posture(cfg)
    lines = [
        f"EXECUTION: {p['mode']}",
        f"  master_switch={p['master_switch']}  watch_execute={p['watch_execute']}  "
        f"auto_trade={p['auto_trade']}",
        f"  risk_level={p['risk_level']}  kill_switch={p['kill_switch']}",
        f"  validation: {p['validation']}",
    ]
    if p["force_paper"]:
        lines.insert(1, "  TP_FORCE_PAPER=1 - this process is vetoed from live "
                        "execution regardless of config (§38)")
    return lines


def print_banner(console, cfg: dict):
    """Rich-console banner. `console` may be any object with .print()."""
    p = execution_posture(cfg)
    console.print(f"[{p['colour']}]EXECUTION: {p['mode']}[/{p['colour']}]")
    if p["force_paper"]:
        console.print("[green]  TP_FORCE_PAPER=1 - live execution vetoed for this "
                       "process (§38)[/green]")
    console.print(f"  master_switch={p['master_switch']}  watch_execute={p['watch_execute']}  "
                  f"auto_trade={p['auto_trade']}")
    console.print(f"  risk_level={p['risk_level']}  kill_switch={p['kill_switch']}")
    console.print(f"  validation: {p['validation']}")


def log_banner(cfg: dict, log=None):
    """Same banner into the log file, for processes with no console
    (scheduler.py under launchd). WARNING level when real orders are possible,
    so it survives any sane log filter."""
    log = log or logger
    p = execution_posture(cfg)
    level = log.warning if (p["master_switch"] or p["mode"].startswith("LIVE")) else log.info
    for line in posture_lines(cfg):
        level(line)
