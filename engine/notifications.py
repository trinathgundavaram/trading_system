"""Desktop notifications via macOS's `osascript` (same mechanism server.py
already uses for `pbcopy` - no new dependency, no API key). Fires for the two
event types worth interrupting you for: a high-conviction BUY candidate, and
an urgent Loop B exit (position_management.py's priority-1 "THESIS BROKEN").
Everything else stays inside trade_prompt.md / the UI - this is deliberately
narrow so it doesn't turn into notification spam on every HOLD signal.

Gated by config.yaml's `notifications.enabled` (default True) and
`notifications.desktop_enabled` (default True) - set either false to
silence this without touching code. Silently no-ops on non-macOS (checks
`sys.platform`) rather than raising, since `osascript` doesn't exist there.
"""
import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def _enabled(cfg: dict) -> bool:
    n = cfg.get("notifications", {}) if cfg else {}
    return n.get("enabled", True) and n.get("desktop_enabled", True)


def notify(cfg: dict, title: str, message: str, subtitle: str = None):
    """Best-effort - a notification failure should never break a scan cycle,
    so every failure mode here is caught and logged, not raised."""
    if not _enabled(cfg):
        return
    if sys.platform != "darwin":
        logger.debug(f"Desktop notification skipped (not macOS): {title} - {message}")
        return

    # AppleScript string literals: escape backslashes first, then quotes.
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    if subtitle:
        script += f' subtitle "{esc(subtitle)}"'

    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=5,
                        capture_output=True)
    except Exception as e:
        logger.warning(f"Desktop notification failed ({e}): {title} - {message}")


def notify_buy_signal(cfg: dict, ticker: str, pct_score: float, regime: str = ""):
    threshold = cfg.get("notifications", {}).get("high_conviction_buy_pct", 80)
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
    )
