#!/usr/bin/env python3
"""'I have a new laptop, get me running.' §46.2 (Phase 3).

WHAT THIS PREVENTS, precisely. Installing on a machine that lacks pandas_ta or
lacks tzdata, getting no error at all, and then quietly computing different
scores or mis-detecting market hours by four hours. Both failures are silent;
both make every number this machine produces incomparable with every other
machine's; and neither shows up until you try to reconcile two runs and cannot.

So this checks prerequisites, reports what is missing WITH the install command
for THIS operating system, and returns non-zero rather than proceeding on a
partial setup.

    python3 scripts/bootstrap.py            # report
    python3 scripts/bootstrap.py --strict   # fail on warnings too
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Per-OS install commands. The value of this check is not "postgres missing" -
# anyone can see that - it is not having to go and look up how to install it
# on the operating system you happen to be on today.
HINTS = {
    "darwin": {
        "postgres": "brew install postgresql@16",
        "git": "xcode-select --install",
        "docker": "brew install --cask docker",
        "clipboard": "(built in - pbcopy)",
    },
    "linux": {
        "postgres": "sudo apt install postgresql-client libpq-dev",
        "git": "sudo apt install git",
        "docker": "curl -fsSL https://get.docker.com | sh",
        "clipboard": "sudo apt install wl-clipboard   # or xclip",
    },
    "win32": {
        "postgres": "winget install PostgreSQL.PostgreSQL",
        "git": "winget install Git.Git",
        "docker": "winget install Docker.DockerDesktop",
        "clipboard": "(built in - clip)",
    },
}

problems: list[str] = []
warnings: list[str] = []
ok: list[str] = []


def os_key() -> str:
    return "linux" if sys.platform.startswith("linux") else sys.platform


def hints() -> dict:
    return HINTS.get(os_key(), {})


def check_python() -> None:
    v = sys.version_info
    if v < (3, 11):
        problems.append(f"Python {v.major}.{v.minor} - 3.11+ required")
    elif v < (3, 12):
        # pandas-ta 0.4.71b0, which requirements.txt pins, has no wheel below
        # 3.12 - so 3.11 installs and then silently uses the fallback engine.
        warnings.append(f"Python {v.major}.{v.minor} - pandas-ta has no wheel below "
                        f"3.12, so this machine will fall back to the hand-rolled "
                        f"TA engine and its scores will not match (§13)")
    else:
        ok.append(f"Python {v.major}.{v.minor}.{v.micro}")


def check_tools() -> None:
    for tool, key, required in (("git", "git", True),
                                ("psql", "postgres", True),
                                ("pg_dump", "postgres", True),
                                ("docker", "docker", False)):
        if shutil.which(tool):
            ok.append(f"{tool} found")
        else:
            msg = f"{tool} not found -> {hints().get(key, 'install it')}"
            (problems if required else warnings).append(msg)


def check_timezone() -> None:
    """The quietest failure in the whole list. Without a zone database,
    ZoneInfo either raises at import or - depending on the platform - resolves
    to something that is not New York, and every market-open check is then
    wrong by four or five hours with no error and no log line."""
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/New_York")
        ok.append("timezone database present")
    except Exception:
        problems.append("timezone database missing -> pip install tzdata "
                        "(REQUIRED on Windows; market hours are silently wrong "
                        "without it)")


def check_ta_backend() -> None:
    """§13/§41. Two machines that disagree about which TA engine is active do
    not produce comparable scores, and nothing at runtime says so."""
    try:
        import pandas_ta

        ok.append(f"pandas_ta {getattr(pandas_ta, '__version__', 'unknown')}")
    except ImportError:
        problems.append("pandas_ta missing - scores will use the fallback engine and "
                        "will NOT match other machines (§13) -> "
                        "pip install -r requirements.txt")


def check_keyring() -> None:
    try:
        import keyring
        from keyring.backends.fail import Keyring as Fail

        if isinstance(keyring.get_keyring(), Fail):
            problems.append("no keyring backend -> pip install keyrings.cryptfile "
                            "(headless Linux), or run inside a desktop session")
        else:
            ok.append(f"keyring backend {type(keyring.get_keyring()).__name__}")
    except ImportError:
        problems.append("keyring not installed -> pip install keyring")


def check_psutil() -> None:
    """§43.2: without psutil there is no hang protection on Windows at all."""
    try:
        import psutil  # noqa: F401

        ok.append("psutil present (cycle hang protection works)")
    except ImportError:
        msg = "psutil missing -> pip install psutil"
        if sys.platform == "win32":
            problems.append(msg + "  (REQUIRED on Windows: without it a hung cycle "
                                  "cannot be killed - §43.2)")
        else:
            warnings.append(msg)


def check_secrets() -> None:
    try:
        from storage import secrets

        missing = [k for k in ("UI_AUTH_TOKEN", "RH_ACCOUNT_NUMBER")
                   if not secrets.present(k)]
        if missing:
            warnings.append(f"required secrets not set: {', '.join(missing)} -> "
                            f"./scripts/tp secrets set <KEY>")
        else:
            ok.append("required secrets resolve")
    except Exception as e:
        warnings.append(f"could not check secrets ({e})")


def check_clipboard() -> None:
    """Cosmetic, and listed as such. The web UI copies in the browser now
    (§47.5), so this only affects the CLI paths."""
    try:
        from storage import platform_support

        backend = platform_support.clipboard_backend()
        if backend:
            ok.append(f"clipboard via {backend}")
        else:
            warnings.append(f"no clipboard tool (CLI copy only; the web UI is "
                            f"unaffected) -> {hints().get('clipboard', '')}")
    except Exception as e:
        warnings.append(f"could not check clipboard ({e})")


def check_database() -> None:
    try:
        from storage.database import Database

        Database()
        ok.append("database reachable")
    except Exception as e:
        warnings.append(f"database not reachable yet ({str(e).splitlines()[0]}) - "
                        f"normal on a fresh machine; `tp install <tag>` creates it")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures")
    args = ap.parse_args(argv)

    check_python()
    check_tools()
    check_timezone()
    check_ta_backend()
    check_keyring()
    check_psutil()
    check_clipboard()
    check_secrets()
    check_database()

    print(f"Trading Platform bootstrap check - {sys.platform}")
    print()
    for line in ok:
        print(f"  ok       {line}")
    for line in warnings:
        print(f"  warn     {line}")
    for line in problems:
        print(f"  MISSING  {line}")
    print()

    if problems:
        print("SETUP INCOMPLETE. Fix the MISSING items above before running the")
        print("platform - each one changes the numbers this machine produces, and")
        print("does so silently.")
        return 1
    if warnings and args.strict:
        print("SETUP INCOMPLETE (--strict: warnings are failures).")
        return 1
    print("All prerequisites present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
