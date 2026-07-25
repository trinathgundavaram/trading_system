"""Every OS-specific call in the codebase, in one place (§43.1, Phase 3).

WHY THIS EXISTS. Before this module the OS-specific calls were inline and
scattered: ``pbcopy`` in server.py, macOS ``open`` in main.py, ``osascript``
in engine/notifications.py, ``caffeinate`` in service.sh - each with its own
ad-hoc ``sys.platform`` guard or bare ``try/except``, and each answering the
question "does this work on Linux?" differently. §41's inventory found eleven
such places. Centralising them means adding an operating system is editing
one file, and "does this work on Linux" has one answer instead of six.

THE CONTRACT: every function here degrades, it does not raise. A clipboard
that is missing on a headless box is a legitimate state, not an error, and a
failure to copy a prompt must never take down a scan cycle. Callers get
``(ok, message)`` and decide how loudly to care.

WHAT IS DELIBERATELY *NOT* HERE. Notifications live in engine/notifications.py
because they have a transport chain rather than a single call, and the
wakelock lives in scripts/tp_agent.py because it holds process state across
the market session. Both import the IS_* flags from here, so the detection
logic is still defined exactly once.

Under the §47 architecture the containerised engine should never call into
this module at all - it is here for the native route (§43) and for the host
agent. ``is_containerised()`` lets a caller assert that.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# --- detection -------------------------------------------------------------
# Defined once, imported everywhere. `os.name == 'nt'` rather than
# `sys.platform == 'win32'` because the latter is also 'win32' on 64-bit
# Python, which reads as a bug to anyone maintaining this later.
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = os.name == "nt"


def _detect_wsl() -> bool:
    """WSL reports as Linux but has the Windows clipboard and file handlers
    available under .exe names, so it needs its own branch in both."""
    if not IS_LINUX:
        return False
    try:
        return "microsoft" in platform.uname().release.lower()
    except Exception:
        return False


IS_WSL = _detect_wsl()


def os_name() -> str:
    """One short, stable string for logs, banners and the /api/health payload."""
    if IS_WSL:
        return "wsl"
    if IS_MAC:
        return "macos"
    if IS_WINDOWS:
        return "windows"
    if IS_LINUX:
        return "linux"
    return sys.platform


def is_containerised() -> bool:
    """True when running inside Docker/Podman.

    Used to assert the §47 split: the engine image must never shell out to a
    host binary. /.dockerenv is written by Docker itself; the cgroup check
    catches Podman and older runtimes. TP_IN_CONTAINER is the explicit
    override the Dockerfile sets, and is checked first so the answer does not
    depend on runtime archaeology."""
    if os.getenv("TP_IN_CONTAINER", "").strip() in ("1", "true", "yes"):
        return True
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except Exception:
        return False


def describe() -> dict:
    """Everything a support question needs, in one dict. Deliberately free of
    anything secret - safe to log at startup and to return from /api/health."""
    return {
        "os": os_name(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "containerised": is_containerised(),
        "clipboard": clipboard_backend() or "none",
    }


# --- clipboard -------------------------------------------------------------
def clipboard_backend() -> str | None:
    """Which clipboard tool this machine actually has, or None.

    Split out from copy_to_clipboard() so a health endpoint can report the
    capability without putting anything on the user's clipboard to find out."""
    for cmd in _clipboard_commands():
        if shutil.which(cmd[0]):
            return cmd[0]
    return None


def _clipboard_commands() -> list[list[str]]:
    if IS_MAC:
        return [["pbcopy"]]
    if IS_WINDOWS:
        return [["clip"]]
    if IS_WSL:
        # The Windows binary is on PATH inside WSL and reaches the real,
        # shared clipboard; wl-copy inside WSLg would not.
        return [["clip.exe"]]
    if IS_LINUX:
        # Wayland first, then X11. Neither is present on a headless box,
        # which is a legitimate state and not an error.
        return [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    return []


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Best-effort copy. Returns (ok, human-readable message).

    NOTE (§47.5): the web UI no longer calls this - the browser's
    navigator.clipboard works on every OS, over an SSH tunnel and from a
    phone, none of which a server-side shell-out can do. This remains for
    main.py's CLI paths, where the process and the human genuinely are on the
    same machine."""
    if is_containerised():
        return False, "no clipboard inside a container - use the web UI's copy button"
    cmds = _clipboard_commands()
    if not cmds:
        return False, f"no clipboard mechanism known for {os_name()}"
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
            return True, f"copied via {cmd[0]}"
        except Exception as e:
            logger.debug(f"clipboard {cmd[0]} failed: {e}")
    hint = ""
    if IS_LINUX and not IS_WSL:
        hint = " (try: apt install wl-clipboard, or xclip)"
    return False, f"no working clipboard tool found{hint}"


# --- opening a file in the desktop handler ---------------------------------
def open_file(path: str | Path) -> tuple[bool, str]:
    """Open a file with the OS's default handler.

    Always returns the resolved path in the failure message: on a headless
    box or in a container the useful answer is not "it failed" but "here is
    where the file is, open it yourself"."""
    p = str(Path(path).resolve())
    if is_containerised():
        return False, f"cannot open a desktop handler from a container; path is {p}"
    try:
        if IS_MAC:
            subprocess.run(["open", p], check=True, timeout=5)
        elif IS_WINDOWS:
            os.startfile(p)  # noqa: attr-defined - Windows-only, guarded above
        elif IS_WSL:
            subprocess.run(["wslview", p], check=True, timeout=5)
        else:
            subprocess.run(["xdg-open", p], check=True, timeout=5)
        return True, "opened"
    except Exception as e:
        return False, f"could not open ({e}); path is {p}"


# --- process spawning ------------------------------------------------------
def sleep_prevention_status() -> tuple[bool | None, str]:
    """Is anything stopping this machine idle-sleeping? (ok, explanation).

    ``None`` means "cannot tell here" - which is the correct answer inside a
    container, where a power assertion has no meaning and the host agent owns
    the wakelock.

    WHY THIS IS IN THE PLATFORM LAYER rather than in scheduler.py, where the
    check lives. It is the fourth OS-specific call in the codebase, and §43.1's
    whole premise is that there is one file to edit when an operating system is
    added. It also keeps the engine free of host binaries, which §47's split
    depends on and tests/test_phase3_portability.py enforces.

    The macOS branch asks pmset rather than walking the process ancestry for
    `caffeinate`, as the pre-Phase-3 check did: pmset reports the assertion
    whoever holds it - a caffeinate wrapper (the old service.sh shape) or the
    host agent (the §47 shape) - and ancestry-walking could only ever see the
    first."""
    if is_containerised():
        return None, ("containerised - idle-sleep prevention belongs to the host "
                      "agent (scripts/tp_agent.py, §47.4)")
    try:
        if IS_MAC:
            out = subprocess.run(["pmset", "-g", "assertions"],
                                 capture_output=True, text=True, timeout=5).stdout
            held = any(f"{k} 1" in out for k in
                       ("PreventUserIdleSystemSleep", "PreventSystemSleep"))
            return held, ("a no-idle-sleep assertion is held" if held else
                          "NOTHING is preventing idle sleep - this machine can sleep "
                          "through scheduled cycles (2026-07-21). Start the host agent, "
                          "or re-run ./service.sh install for the caffeinate wrapper")
        if IS_LINUX:
            out = subprocess.run(["systemd-inhibit", "--list"],
                                 capture_output=True, text=True, timeout=5).stdout
            held = "idle" in out
            return held, ("a systemd idle inhibitor is held" if held else
                          "no idle inhibitor - a laptop can sleep through scheduled "
                          "cycles; a headless server is unaffected")
        if IS_WINDOWS:
            # SetThreadExecutionState is per-thread and cannot be queried from
            # another process, so this is genuinely unknowable from here.
            return None, ("Windows: the host agent holds the execution-state flag; "
                          "it cannot be queried from another process")
    except FileNotFoundError:
        return None, "no power-management tool available to query"
    except Exception as e:
        return None, f"could not determine sleep-prevention status: {e}"
    return None, f"no known mechanism on {os_name()}"


def detached_popen_kwargs() -> dict:
    """Popen kwargs that put a child in its own process/job group.

    POSIX gets start_new_session=True (its own session and process group, so
    engine/cycle_supervisor.py can take the whole tree down at once). Windows
    has no equivalent notion, but CREATE_NEW_PROCESS_GROUP at least stops the
    child inheriting the parent console's Ctrl-C and lets it be signalled
    independently - which is what psutil's tree walk in cycle_supervisor
    relies on."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}
