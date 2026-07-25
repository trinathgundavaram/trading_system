"""Notification transports, tried in configured order (§43.3, Phase 3).

WHAT THIS REPLACED. The previous implementation was a single ``osascript``
call. That is macOS-only - and, worse under the §47 architecture, it cannot
work from inside a container *even on macOS*, because a container has no
route to the host's notification centre. So a system that raises a kill-switch
alert while you are out had exactly one delivery mechanism, and it was the one
guaranteed to be unavailable in the deployment shape the platform is moving to.

THE SHAPE NOW. A notification is an event with a severity, and delivery is a
chain of transports tried in order until one succeeds:

  desktop   osascript / notify-send / win10toast. Free, instant, and only
            works when the process is on the same machine as the human.
  webhook   ntfy.sh, Slack, Discord, Pushover - anything accepting a POST.
            Works from a container, from a VPS, and reaches your phone when
            you are not at the machine. This is the one that matters.
  log       Always succeeds. It is last in the default order for a reason:
            the old code, when osascript failed, logged a warning and dropped
            the notification. A notification is now never silently lost.

Order is configurable in config.yaml (``notifications.transports``) so a
headless install can put webhook first and skip the pointless desktop attempt.

UNDER §47. Once the host agent (scripts/tp_agent.py) is running, the engine
should not call this at all for user-facing events - it writes to ``ui_events``
and the agent, which is native and on the host, does the notifying. This module
remains the path for the native (non-container) route and for anything that
must be delivered even if the agent is dead.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_TRANSPORTS = ("desktop", "webhook", "log")


def _cfg_section(cfg: dict) -> dict:
    return (cfg.get("notifications", {}) if cfg else {}) or {}


def _enabled(cfg: dict) -> bool:
    return _cfg_section(cfg).get("enabled", True)


_ENV_REF = re.compile(r"^\$\{([A-Z0-9_]+)(?::-([^}]*))?\}$")


def _webhook_url(cfg: dict) -> str:
    """The webhook endpoint, with ``${VAR}`` resolved.

    This is not defensive coding, it is a real trap. config.yaml stores the
    URL as ``${NOTIFY_WEBHOOK_URL:-}`` so the endpoint is not committed, and
    config_loader.py expands that - but scheduler.py's load_config() and
    server.py's _load_config() both read the file with a bare yaml.safe_load.
    Those callers therefore hand us the literal string ``${NOTIFY_WEBHOOK_URL:-}``,
    which is TRUTHY, so without this the webhook transport would attempt a POST
    to a nonsense URL and log a failure on every single notification - and the
    transport that reaches you when you are away from the machine would never
    have worked once."""
    raw = (_cfg_section(cfg).get("webhook_url") or "").strip()
    m = _ENV_REF.match(raw)
    if m:
        import os

        return (os.getenv(m.group(1)) or m.group(2) or "").strip()
    return raw


def _esc(s: str) -> str:
    """AppleScript string literals: escape backslashes first, then quotes.
    Order matters - doing quotes first would then double-escape the
    backslashes this inserts."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


# --- transports ------------------------------------------------------------
def _transport_desktop(title, message, subtitle, cfg, severity="info") -> bool:
    """The native popup, on whichever OS this is. Returns False rather than
    raising when the mechanism is absent - a headless box and a container are
    both legitimate places to be, and a scan cycle must not die because a
    notification could not be drawn."""
    if not _cfg_section(cfg).get("desktop_enabled", True):
        return False
    from storage.platform_support import IS_LINUX, IS_MAC, IS_WINDOWS, is_containerised

    if is_containerised():
        # Not a failure worth a warning: §47 expects this and routes the
        # event to the host agent instead.
        logger.debug("desktop notification skipped - running in a container")
        return False
    try:
        if IS_MAC:
            script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
            if subtitle:
                script += f' subtitle "{_esc(subtitle)}"'
            if severity == "critical":
                script += ' sound name "Basso"'
            subprocess.run(["osascript", "-e", script], check=True, timeout=5,
                           capture_output=True)
        elif IS_LINUX and shutil.which("notify-send"):
            urgency = "critical" if severity == "critical" else "normal"
            subprocess.run(["notify-send", "-u", urgency, title,
                            f"{subtitle + chr(10) if subtitle else ''}{message}"],
                           check=True, timeout=5)
        elif IS_WINDOWS:
            # Optional extra; absent on a clean install, hence the local
            # import and the False return rather than an ImportError at
            # module load.
            from win10toast import ToastNotifier

            ToastNotifier().show_toast(title, message, threaded=True)
        else:
            return False
        return True
    except Exception as e:
        logger.debug(f"desktop notification unavailable: {e}")
        return False


def _transport_webhook(title, message, subtitle, cfg, severity="info") -> bool:
    """POST to whatever the user configured.

    Deliberately a raw urllib POST with a generic JSON body rather than a
    per-service client: ntfy, Slack, Discord and Pushover all accept a POST,
    and adding a dependency for each would make the notification path heavier
    than the thing it notifies about. Failures are warned, not raised."""
    url = _webhook_url(cfg)
    if not url:
        return False
    try:
        body = json.dumps({
            "title": title,
            "text": f"{subtitle or ''}\n{message}".strip(),
            "severity": severity,
        })
        req = urllib.request.Request(
            url, data=body.encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
        return True
    except Exception as e:
        logger.warning(f"webhook notification failed: {e}")
        return False


def _transport_log(title, message, subtitle, cfg, severity="info") -> bool:
    """The backstop. Always succeeds, which is the whole point: with this in
    the chain a notification is recorded somewhere durable even when every
    interactive channel is unavailable."""
    line = f"{title}: {message}" + (f" ({subtitle})" if subtitle else "")
    if severity == "critical":
        logger.critical(line)
    else:
        logger.info(f"NOTIFY {line}")
    return True


TRANSPORTS = {
    "desktop": _transport_desktop,
    "webhook": _transport_webhook,
    "log": _transport_log,
}


# --- public API ------------------------------------------------------------
def notify(cfg: dict, title: str, message: str, subtitle: str = None,
           severity: str = "info") -> bool:
    """Deliver one notification down the configured transport chain.

    Returns True if any transport accepted it. Best-effort throughout - a
    notification failure must never break a scan cycle, so every failure mode
    below is caught and logged rather than raised."""
    if not _enabled(cfg):
        return False
    order = _cfg_section(cfg).get("transports") or list(DEFAULT_TRANSPORTS)
    for name in order:
        fn = TRANSPORTS.get(name)
        if fn is None:
            logger.warning(f"unknown notification transport {name!r} - "
                           f"known: {', '.join(TRANSPORTS)}")
            continue
        try:
            if fn(title, message, subtitle, cfg, severity):
                return True
        except Exception as e:  # a transport must never take the caller down
            logger.warning(f"notification transport {name!r} raised: {e}")
    logger.warning(f"NOTIFY (no transport succeeded): {title} - {message}")
    return False


def notify_buy_signal(cfg: dict, ticker: str, pct_score: float, regime: str = ""):
    threshold = _cfg_section(cfg).get("high_conviction_buy_pct", 80)
    if pct_score < threshold:
        return
    notify(
        cfg,
        title=f"BUY candidate: {ticker}",
        message=f"Score {pct_score:.0f}%" + (f" | {regime} regime" if regime else ""),
        subtitle="Trading Platform",
    )


def notify_urgent_exit(cfg: dict, ticker: str, reason: str):
    notify(
        cfg,
        title=f"URGENT EXIT: {ticker}",
        message=reason,
        subtitle="Trading Platform - Loop B",
        severity="critical",
    )


def send_critical(title: str, message: str):
    """A notification that IGNORES notifications.enabled (§9).

    Every other notifier here is courtesy: a buy signal you can catch on the
    next cycle. This one exists for events where the system has stopped itself
    - the automatic kill switch - and those must reach you even if you muted
    the noisy ones. Someone who turned notifications off to stop buy-signal
    chatter has not consented to missing "TRADING HALTED".

    Takes no cfg, deliberately: it must be callable from rules/risk_rules.py
    without threading config through, and there is no configuration under
    which suppressing this would be the right behaviour. It therefore also
    cannot read notifications.webhook_url from config - it loads the webhook
    from the environment instead, so the critical path has no dependency on
    config parsing having succeeded.
    """
    import os

    cfg = {"notifications": {
        "enabled": True,
        "transports": ["desktop", "webhook", "log"],
        "webhook_url": os.getenv("NOTIFY_WEBHOOK_URL", ""),
    }}
    notify(cfg, title, message, subtitle="Trading Platform", severity="critical")
    # UNCONDITIONALLY, and not only when delivery failed. The chain
    # short-circuits at the first success, so a desktop popup that lands means
    # the `log` transport is never reached - and this is the one notification
    # where the durable record matters more than the interrupt. A popup is
    # dismissed and gone; "TRADING HALTED" has to still be in the log tomorrow
    # when someone asks why the system stopped.
    logger.critical(f"{title}: {message}")
