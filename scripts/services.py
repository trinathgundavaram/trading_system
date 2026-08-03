#!/usr/bin/env python3
"""Install / start / stop / status for the background services, on any OS.
§45 (Phase 3). Replaces service.sh, which was 100% launchd.

WHY THIS EXISTS. service.sh is `launchctl bootstrap`, `~/Library/LaunchAgents`
and plist XML from top to bottom. On any other operating system the scheduler
runs in a foreground terminal and dies when the window closes - which is
exactly the failure ("auto stops and doesn't come back") that service.sh was
written to fix on macOS, reintroduced everywhere else.

  macOS     launchd            LaunchAgent plists in ~/Library/LaunchAgents
  Linux     systemd            user units in ~/.config/systemd/user (no root)
  Windows   Task Scheduler     schtasks /Create with an ONLOGON trigger

Same three services everywhere: scheduler, ui, maverick.

WHAT IS PRESERVED FROM service.sh, deliberately. Every one of these was added
in response to a real incident and none of them is decoration:

  * The versioned label/unit suffix (§38.5). Two installed versions must not
    fight over one service name; `tp promote` passes the suffix.
  * TP_OUTPUT_DIR / POSTGRES_DB / TP_VERSION in the service environment, so a
    promoted version does not write into its own git worktree or share the
    primary's database.
  * The 4096/8192 open-file limits (2026-07-17 fd exhaustion: launchd's
    default 256 was hit by concurrent ticker fan-out and cascaded into
    unrelated-looking SQLite and MCP failures).
  * The full installing-shell PATH, because the scheduler spawns npx/uvx MCP
    subprocesses and a service's default PATH does not include them.
  * Idle-sleep prevention. caffeinate on macOS, systemd-inhibit on Linux,
    DisallowStartIfOnBatteries=false plus a wakelock in the agent on Windows.
    A sleeping machine is a stopped scheduler on every OS.

HONEST CAVEAT ABOUT WINDOWS. Task Scheduler is the weakest of the three: no
real supervision, primitive logging, and ONLOGON means nothing runs when
nobody is logged in. If Windows is your main platform, run the containers
(§42) under Docker Desktop or WSL2 rather than fighting this - you get systemd
semantics and a far better outcome.

Usage:
    python3 scripts/services.py install [--services scheduler,ui]
    python3 scripts/services.py start|stop|restart|status|uninstall
    python3 scripts/services.py logs scheduler
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))

SERVICES = ("scheduler", "ui", "maverick")


def _out_dir() -> Path:
    """Runtime data root. Mirrors storage/paths.py: unset means <repo>/output,
    and `tp promote` points it at ~/tp/data/<tag>/output so a promoted version
    does not write into its own git worktree."""
    return Path(os.getenv("TP_OUTPUT_DIR", REPO_DIR / "output"))


def _log_path(name: str) -> Path:
    d = _out_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"service_{name}.log"


def _resolved_secrets() -> dict:
    """Every SECRET_KEY/OPTIONAL_KEY that currently resolves, by value - added
    2026-07-26 (external report: "APIs/MCPs shown as not configured whereas
    there was data fetched from them earlier").

    Before this, a launchd/systemd/Task-Scheduler-managed service's
    EnvironmentVariables held ONLY the six fixed keys below - no market-data
    or broker credentials at all. Every provider client in this codebase
    (market_data.py's REST providers, robinhood_mcp.py, live_trader.py) reads
    its key with a bare os.getenv(), so the service process's actual ability
    to fetch data depended entirely on falling through to a local .env file -
    which works for the PRIMARY checkout (its .env sits right next to
    services.py) but silently fails for anything `tp install`/`tp promote`
    manages: `git worktree add` never checks out a gitignored path, so
    ~/tp/versions/<tag>/.env never exists, and nothing ever created one.

    `tp run <tag>` (the FOREGROUND path) already avoided this by resolving
    every secret through storage.secrets - which additionally checks the OS
    Keychain, a machine-wide store independent of which worktree is asking -
    and injecting the resolved values straight into the child's environment.
    This does the identical thing for the SERVICE path, so `tp promote`
    stops being the one code path that quietly drops every credential.

    Best-effort: a machine with storage/secrets.py unimportable (a stripped
    checkout, a broken venv) gets the old six-key environment back, exactly
    as before this function existed, rather than failing the whole install."""
    try:
        from storage import secrets
        out = {}
        for k in secrets.SECRET_KEYS + secrets.OPTIONAL_KEYS:
            v = secrets.get(k, required=False)
            if v:
                out[k] = v
        return out
    except Exception as e:
        print(f"  WARNING: could not resolve secrets for the service environment ({e}) - "
              f"this service may be unable to reach any key-gated provider until "
              f"reinstalled with secrets available.")
        return {}


def _service_env() -> dict:
    """The environment every managed service needs.

    PATH is the installing shell's, in full: a service gets a minimal PATH by
    default on all three platforms, and the scheduler shells out to npx and
    uvx for MCP servers."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(Path.home()),
        "TP_OUTPUT_DIR": str(_out_dir()),
        "POSTGRES_DB": os.getenv("POSTGRES_DB", "trading_platform"),
        "TP_VERSION": os.getenv("TP_VERSION", "unversioned"),
        # Fail closed, same rule as docker-compose.yml: a background service
        # that was not explicitly promoted must not arm live execution.
        "TP_FORCE_PAPER": os.getenv("TP_FORCE_PAPER", "1"),
        # See _resolved_secrets()'s docstring. Placed last so nothing above
        # can ever be shadowed by a same-named secret (there is no overlap
        # today, but the ordering is deliberate insurance).
        **_resolved_secrets(),
    }


# The port main.py binds for the web UI. Hardcoded there (uvicorn.run(...,
# port=8080)); named here so the stale-holder cleanup below and that bind
# cannot drift apart silently.
UI_PORT = int(os.getenv("TP_UI_PORT", "8080"))


def _port_holder_pids(port: int) -> list:
    """PIDs listening on ``port``, or [] if that cannot be determined.

    Deliberately best-effort and never raises: this feeds a cleanup step that
    must not be the reason a start fails. An empty list means EITHER nothing is
    listening or we could not tell, and the caller treats those the same way -
    it proceeds, and the bind either works or reports its own error.
    """
    if os.name == "nt":
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                if parts[4].isdigit() and parts[4] != "0":
                    pids.append(int(parts[4]))
        return sorted(set(pids))
    for argv in (["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                 # Containers and slim Linux images routinely have no lsof.
                 ["fuser", f"{port}/tcp"]):
        if not shutil.which(argv[0]):
            continue
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        pids = [int(t) for t in r.stdout.replace(":", " ").split() if t.isdigit()]
        if pids:
            return sorted(set(pids))
    return []


def _describe_pid(pid: int) -> str:
    """Best-effort command line for ``pid``, for the log line and the ownership
    check below. Empty string when it cannot be read."""
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                capture_output=True, text=True, timeout=10)
        else:
            r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                               capture_output=True, text=True, timeout=10)
        return " ".join(r.stdout.split())
    except Exception:
        return ""


def _free_ui_port(port: int = UI_PORT, wait_s: float = 5.0) -> None:
    """Kill a STALE previous UI process still holding ``port``, then wait for it.

    Why this exists (2026-07-26). `run.sh --ui` has done this since the
    2026-07-14 incident, where an orphaned `main.py --ui` kept holding 8080
    invisibly: the new process failed to bind, the browser kept talking to the
    OLD one, and - in run.sh's own words - it "looked exactly like a frontend
    bug ... when it was really 'you have two of these running.'"

    §45 moved service management from service.sh to this file and did NOT bring
    that cleanup along, so `./service.sh restart` reintroduced the identical
    failure through a different door. It recurred on 2026-07-26: a restart
    after tagging v2.3.0 left the pre-v2.3.0 process on 8080, launchd
    relaunch-looped the new one 342 times, and the fix being tested appeared
    not to work because the code under test was never the code serving.

    Stricter than run.sh's version in one respect. run.sh kills whatever holds
    the port, reasoning that on a single-user dev machine it is always our own
    leftover. That is usually true and occasionally expensive, so this checks
    the command line first and REFUSES to kill a process that is not one of
    ours - the cost of being wrong (killing an unrelated service) is much
    higher than the cost of printing an explanation and letting the bind fail
    with a message that now makes sense.
    """
    pids = [p for p in _port_holder_pids(port) if p != os.getpid()]
    if not pids:
        return
    for pid in pids:
        desc = _describe_pid(pid)
        ours = ("main.py" in desc and "--ui" in desc) or "uvicorn" in desc
        if not ours:
            print(f"  WARNING: port {port} is held by PID {pid}, which does not look")
            print(f"           like this platform's UI process:")
            print(f"             {desc or '(command line unavailable)'}")
            print(f"           NOT killing it. The UI will fail to bind until you")
            print(f"           free the port or set TP_UI_PORT to something else.")
            continue
        print(f"  port {port} still held by a previous UI process (PID {pid}) - "
              f"terminating it")
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue          # already gone between the scan and the signal
        except PermissionError:
            print(f"  WARNING: not permitted to terminate PID {pid} - free port "
                  f"{port} manually")
            continue
        except Exception as e:
            print(f"  WARNING: could not terminate PID {pid}: {e}")
            continue

    # A SIGTERM is a request. Poll rather than sleeping a fixed amount, then
    # escalate once - a process that ignores SIGTERM will still be holding the
    # socket when uvicorn tries to bind, which is the whole failure being fixed.
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not [p for p in _port_holder_pids(port) if p != os.getpid()]:
            return
        time.sleep(0.25)
    for pid in [p for p in _port_holder_pids(port) if p != os.getpid()]:
        desc = _describe_pid(pid)
        if ("main.py" in desc and "--ui" in desc) or "uvicorn" in desc:
            print(f"  PID {pid} ignored SIGTERM after {wait_s:.0f}s - sending SIGKILL")
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def _commands() -> dict:
    """service name -> argv. One definition, three managers."""
    py = sys.executable or "python3"
    cmds = {
        "scheduler": [py, str(REPO_DIR / "scheduler.py")],
        "ui": [py, str(REPO_DIR / "main.py"), "--ui"],
    }
    mav = os.getenv("MAVERICK_CMD", "")
    if not mav and (Path.home() / "maverick-mcp").is_dir():
        mav = f"cd {Path.home() / 'maverick-mcp'} && make dev"
    if mav:
        cmds["maverick"] = ["/bin/bash", "-c", mav] if os.name != "nt" else ["cmd", "/c", mav]
    return cmds


# =============================================================================
#  The interface
# =============================================================================
class ServiceManager:
    """One method per verb. Every implementation below is expected to be
    idempotent: installing twice must not produce two services, and stopping
    something that is not running is not an error."""

    def __init__(self, suffix: str = ""):
        # §38.5: '.v2.1.0' when installed by `tp promote`, '' for a plain
        # development install.
        self.suffix = suffix

    def install(self, name: str, cmd: list, env: dict, workdir: Path, log_path: Path) -> None:
        raise NotImplementedError

    def is_installed(self, name: str) -> bool:
        """Has `install` been run for this service on this machine?

        Added 2026-07-26 so main() can ask the question without reaching for a
        manager-specific attribute. The first cut of that check called
        `mgr.plist_path(name)` directly, which exists only on LaunchdManager
        and would have raised AttributeError on Linux and Windows - trading a
        macOS bug for a portability one.

        Defaults to True: a manager that cannot answer cheaply should not
        block an operation that used to proceed. The check exists to prevent a
        destructive step (killing the port holder) when we already KNOW the
        replacement cannot start, not to add a new precondition.
        """
        return True

    def uninstall(self, name: str) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def status(self, name: str) -> str:
        raise NotImplementedError

    def other_registrations(self, name: str) -> list:
        """IDs of every OTHER installed registration of ``name`` still on this
        machine - same service, different suffix (including no suffix at
        all).

        Added 2026-07-26. §38.5 says two installed versions must not fight
        over one service name, and `tp promote` upholds that by uninstalling
        the OLD version's suffixed registration on every promote. What it
        does NOT cover is a bare `python3 scripts/services.py install ui`
        run with no `--suffix` - which happens the moment anyone bypasses
        `tp` and calls this file directly, including by habit from before
        promoted versions existed. That writes the UNSUFFIXED registration
        and never touches a suffixed one left over from a previous promote,
        so both end up installed and KeepAlive/Restart=on-failure/ONLOGON
        resurrects each one the instant the other kills it for the same
        port - the same failure `_free_ui_port` documents, just one layer up
        and for any of the three services, not only `ui`.

        Default: no way to answer on this manager, so report none rather
        than guess.

        Deliberately does NOT auto-remove what it finds (see the call site in
        main()): the whole failure mode this exists to catch is one
        registration silently winning over another, and picking "whichever
        one you happened to run install/start on" as the automatic winner
        would just move that same silent-overwrite risk one level up - a
        stray bare `install ui` would delete the pinned `v3.2.0` registration
        (losing its TP_VERSION/TP_OUTPUT_DIR pinning) rather than the other
        way around. Report and refuse instead, same rule _free_ui_port
        already applies to killing a PID it cannot positively identify as
        ours.
        """
        return []


# =============================================================================
#  macOS - launchd
# =============================================================================
class LaunchdManager(ServiceManager):
    LABEL_PREFIX = "com.tradingplatform"

    @property
    def agents_dir(self) -> Path:
        d = Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def label(self, name: str) -> str:
        return f"{self.LABEL_PREFIX}{self.suffix}.{name}"

    def plist_path(self, name: str) -> Path:
        return self.agents_dir / f"{self.label(name)}.plist"

    def is_installed(self, name: str) -> bool:
        return self.plist_path(name).exists()

    def _domain(self, name: str) -> str:
        return f"gui/{os.getuid()}/{self.label(name)}"

    @staticmethod
    def _xml(s: str) -> str:
        """plist is XML. A Maverick command like `cd ... && make dev` breaks
        the file without escaping - this was found by service.sh's own
        install-time validation, not in theory."""
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def install(self, name, cmd, env, workdir, log_path):
        # caffeinate -i keeps the Mac out of idle sleep through scan windows.
        # The display may still sleep and the screen may lock; only idle sleep
        # of the machine is inhibited.
        if name == "scheduler":
            caffeinate = shutil.which("caffeinate")
            if caffeinate:
                cmd = [caffeinate, "-i", *cmd]
        args = "".join(f"<string>{self._xml(a)}</string>" for a in cmd)
        envs = "".join(f"<key>{self._xml(k)}</key><string>{self._xml(v)}</string>"
                       for k, v in env.items())
        self.plist_path(name).write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{self.label(name)}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>WorkingDirectory</key><string>{self._xml(workdir)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{self._xml(log_path)}</string>
  <key>StandardErrorPath</key><string>{self._xml(log_path)}</string>
  <!-- 2026-07-17 fd-exhaustion fix. launchd's default per-process open-file
       limit of 256 is too low: scheduler.py fans out to
       cycle_max_parallel_tickers concurrent tickers and each analyze() can
       spawn up to 9 concurrent MCP calls, several of them a fresh uvx/npx
       subprocess. Hitting [Errno 24] cascaded into unrelated-looking
       failures in the same process - "unable to open database file", the
       Maverick SSE stream erroring, yfinance failing - all at once, because
       every one of them needs a free descriptor. -->
  <key>SoftResourceLimits</key><dict>
    <key>NumberOfFiles</key><integer>4096</integer>
  </dict>
  <key>HardResourceLimits</key><dict>
    <key>NumberOfFiles</key><integer>8192</integer>
  </dict>
  <key>EnvironmentVariables</key><dict>{envs}</dict>
</dict>
</plist>
""")
        print(f"  wrote {self.plist_path(name)}")

    # launchctl bootout is ASYNCHRONOUS (2026-07-26). It returns as soon as the
    # request is accepted, not when the job is gone - so `restart`, which is
    # stop-then-start, raced its own bootout and bootstrap landed while the
    # label was still registered in the domain. launchd reports that as
    #
    #     Bootstrap failed: 5: Input/output error
    #     Try re-running the command as root for richer errors.
    #
    # which names neither the cause nor the fix, and sends you looking for a
    # permissions problem that is not there. Root would not have helped.
    BOOTOUT_TIMEOUT_S = 10.0

    def _is_loaded(self, name) -> bool:
        """False, rather than an exception, when launchctl cannot be run at
        all. 'Is it loaded?' has a sensible answer on a machine with no
        launchd, and it is no."""
        try:
            return subprocess.run(["launchctl", "print", self._domain(name)],
                                  capture_output=True).returncode == 0
        except (FileNotFoundError, OSError):
            return False

    def _wait_until_gone(self, name) -> bool:
        """Poll until the label leaves the domain. Returns False on timeout,
        so the caller can say so rather than failing obscurely one line later."""
        deadline = time.time() + self.BOOTOUT_TIMEOUT_S
        while time.time() < deadline:
            if not self._is_loaded(name):
                return True
            time.sleep(0.25)
        return False

    def start(self, name):
        """Bootstrap the agent, and DIAGNOSE the failure rather than raising.

        2026-07-26. `./service.sh restart` produced this, verbatim:

            restart scheduler
              not running: com.tradingplatform.scheduler
            Bootstrap failed: 5: Input/output error
            Try re-running the command as root for richer errors.
            Traceback (most recent call last):
              ...
            subprocess.CalledProcessError: Command '['launchctl',
            'bootstrap', 'gui/501', '.../com.tradingplatform.scheduler.plist']'
            returned non-zero exit status 5

        Three separate faults conspired there, and all three are fixed here.

        1. `start()` bootstraps a plist it never checks exists. Only
           `install()` writes one. So `start`/`restart` on a machine that has
           never run `install` - or whose plist was removed, or was written by
           a DIFFERENT python than the one now running (`_commands()` uses
           `sys.executable`, so an install under a venv and a restart under
           anaconda disagree) - fails in launchd rather than here, where the
           reason is known.

        2. launchd's exit 5 is `EIO`, which it uses for several unrelated
           conditions. The two that actually happen: the label is still
           registered (bootout is asynchronous - see BOOTOUT_TIMEOUT_S above -
           and `launchctl print` can report "not loaded" while the job is mid
           -teardown), or the label has been explicitly disabled, in which
           case bootstrap refuses forever and no amount of retrying helps.
           Telling them apart takes one extra command; guessing does not work.

        3. `check=True` converted all of that into a Python traceback ending in
           CalledProcessError. A stack trace through subprocess.run is the
           least informative possible rendering of "your service is already
           registered" - and launchd's own advice, "try re-running as root",
           is actively wrong here: this is a gui/$UID domain, and running it as
           root targets a different domain entirely.
        """
        plist = self.plist_path(name)
        if not plist.exists():
            print(f"  ERROR: no plist at {plist}")
            print(f"         `start` bootstraps an existing plist; only `install` writes one.")
            print(f"         Fix: ./service.sh install {name}")
            return

        if self._is_loaded(name):
            subprocess.run(["launchctl", "bootout", self._domain(name)],
                           capture_output=True)
            self._wait_until_gone(name)

        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  started {self.label(name)}")
            return

        # Exit 5 with the job not visibly loaded is nearly always a teardown
        # that had not finished. Retry once, after boot-ing out unconditionally
        # this time - which is safe whether or not anything is there.
        # 2026-07-27: Enhanced retry logic for "Input/output error" (exit 5).
        if r.returncode == 5:
            subprocess.run(["launchctl", "bootout", self._domain(name)],
                           capture_output=True)
            self._wait_until_gone(name, timeout=3.0)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  started {self.label(name)} (after clearing a stale registration)")
                return

            # If still failing, try with sudo (different user context may help)
            if r.returncode == 5:
                try:
                    r = subprocess.run(
                        ["sudo", "launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                        capture_output=True, text=True, timeout=15)
                    if r.returncode == 0:
                        print(f"  started {self.label(name)} (with elevated privileges)")
                        return
                except Exception as e:
                    print(f"  (sudo attempt failed: {e})")

        self._explain_bootstrap_failure(name, plist, r)

    def _explain_bootstrap_failure(self, name, plist, r):
        """Turn a launchctl exit code into something actionable."""
        label = self.label(name)
        err = (r.stderr or r.stdout or "").strip()
        print(f"  ERROR: could not start {label} (launchctl exit {r.returncode})")
        if err:
            print(f"         launchctl said: {err}")

        # Explicitly disabled labels survive reboots and ignore bootstrap.
        disabled = subprocess.run(["launchctl", "print-disabled", f"gui/{os.getuid()}"],
                                  capture_output=True, text=True).stdout
        if f'"{label}" => disabled' in disabled or f'"{label}" => true' in disabled:
            print(f"         {label} is DISABLED in this domain, which is why bootstrap")
            print(f"         refuses. Re-enable it:")
            print(f"           launchctl enable gui/{os.getuid()}/{label}")
            print(f"           ./service.sh start {name}")
            return

        # A plist naming an interpreter that no longer exists is the other
        # common cause, and the one a version switch creates: _commands() bakes
        # sys.executable into the plist at install time.
        try:
            import plistlib
            with open(plist, "rb") as fh:
                prog = (plistlib.load(fh).get("ProgramArguments") or [None])[0]
            if prog and not Path(prog).exists():
                print(f"         The plist runs {prog}, which does not exist.")
                print(f"         It was written by a different interpreter than the one")
                print(f"         running now ({sys.executable}). Rewrite it:")
                print(f"           ./service.sh install {name}")
                return
        except Exception as e:
            print(f"         (could not parse {plist}: {type(e).__name__}: {e})")
            print(f"         A malformed plist also produces exit 5. Rewrite it:")
            print(f"           ./service.sh install {name}")
            return

        print(f"         Most likely a stale registration. In order:")
        print(f"           launchctl bootout gui/{os.getuid()}/{label}")
        print(f"           ./service.sh install {name}")
        print(f"         Note: launchd's own 'try as root' advice does not apply -")
        print(f"         this is the gui/{os.getuid()} domain, and root is a different one.")

    def stop(self, name):
        r = subprocess.run(["launchctl", "bootout", self._domain(name)],
                           capture_output=True)
        if r.returncode == 0:
            # Wait here too, so `stop` means stopped. Otherwise anything that
            # follows - a pg_dump, a migration, a reset - runs while the
            # scheduler is still mid-cycle and holding connections.
            self._wait_until_gone(name)
        print(f"  {'stopped' if r.returncode == 0 else 'not running:'} {self.label(name)}")

    def uninstall(self, name):
        self.stop(name)
        self.plist_path(name).unlink(missing_ok=True)

    def status(self, name):
        r = subprocess.run(["launchctl", "print", self._domain(name)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return "not loaded"
        for line in r.stdout.splitlines():
            if "state =" in line or "pid =" in line:
                return line.strip()
        return "loaded"

    def other_registrations(self, name):
        # Glob rather than ask launchd: a stray plist with nothing currently
        # loaded is still the bug waiting to happen the next time anything
        # bootstraps it, so it needs to be found and reported too, not just
        # the ones launchd currently shows as active.
        mine = self.plist_path(name)
        return [p.stem for p in self.agents_dir.glob(f"{self.LABEL_PREFIX}*.{name}.plist")
                if p != mine]


# =============================================================================
#  Linux - systemd user units
# =============================================================================
class SystemdManager(ServiceManager):
    """User units, not system units: no root anywhere in this file.

    THE ONE THAT CATCHES PEOPLE OUT is lingering. A user unit stops when you
    log out, so the scheduler dies when you close your SSH session - the Linux
    analogue of the launchd session-scoping problem service.sh already
    documents. install() checks for it and tells you the command."""

    @property
    def unit_dir(self) -> Path:
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def unit(self, name: str) -> str:
        return f"tp-{name}{self.suffix}.service"

    def is_installed(self, name: str) -> bool:
        return (self.unit_dir / self.unit(name)).exists()

    def _systemctl(self, *args, check=False):
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, check=check)

    def install(self, name, cmd, env, workdir, log_path):
        exec_start = " ".join(_shquote(a) for a in cmd)
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in env.items() if v)
        # systemd-inhibit is the caffeinate equivalent: keep the machine out of
        # idle sleep while the scheduler runs. Harmless on a headless server,
        # meaningful on a laptop.
        inhibit = ("ExecStartPre=-/usr/bin/systemd-inhibit --what=idle "
                   "--why='trading platform' true\n") if name == "scheduler" else ""
        # The working directory, NOT the data directory. scripts/tp_agent.py
        # writes .env.runtime beside the repo; pointing at _out_dir().parent
        # only coincides with that when TP_OUTPUT_DIR is unset, so after a
        # `tp promote` the unit would reference a path nothing ever writes -
        # and the '-' prefix means it would fail SILENTLY, presenting as
        # missing credentials with no error to explain them.
        runtime_env = Path(workdir) / ".env.runtime"
        self.unit_dir.joinpath(self.unit(name)).write_text(f"""[Unit]
Description=Trading Platform {name} ({os.getenv('TP_VERSION', 'dev')})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
{env_lines}
# '-' prefix: a missing runtime env file is not a startup failure. On a
# container-less install the secrets come from the OS keyring instead (§44).
EnvironmentFile=-{runtime_env}
ExecStart={exec_start}
{inhibit}Restart=on-failure
RestartSec=10
# The fd ceiling that the 2026-07-17 incident established on macOS. The same
# concurrent MCP fan-out happens here.
LimitNOFILE=8192

# journald handles rotation, which removes the 77 MB unrotated-log problem
# (E-10) entirely on this platform - no logrotate config to forget.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=tp-{name}

[Install]
WantedBy=default.target
""")
        print(f"  wrote {self.unit_dir / self.unit(name)}")
        self._systemctl("daemon-reload")
        self._warn_if_not_lingering()

    @staticmethod
    def _warn_if_not_lingering() -> None:
        user = getpass.getuser()
        linger = Path(f"/var/lib/systemd/linger/{user}")
        if not linger.exists():
            print(f"  NOTE: lingering is off for {user}, so these services stop when you\n"
                  f"        log out - including when an SSH session ends. Enable it:\n"
                  f"          sudo loginctl enable-linger {user}")

    def start(self, name):
        # Same guard the launchd path grew on 2026-07-26: `start` enables a
        # unit it does not write (only `install` writes one), so on a machine
        # that has never installed, check=True turned "no unit file" into a
        # CalledProcessError traceback. Kept in step with LaunchdManager.start
        # so the two platforms fail the same way.
        if not self.is_installed(name):
            print(f"  ERROR: no unit file at {self.unit_dir / self.unit(name)}")
            print(f"         `start` enables an existing unit; only `install` writes one.")
            print(f"         Fix: ./service.sh install {name}")
            return
        r = self._systemctl("enable", "--now", self.unit(name))
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()
            print(f"  ERROR: could not start {self.unit(name)} (systemctl exit {r.returncode})")
            if err:
                print(f"         systemctl said: {err}")
            return
        print(f"  started {self.unit(name)}")

    def stop(self, name):
        self._systemctl("disable", "--now", self.unit(name))
        print(f"  stopped {self.unit(name)}")

    def uninstall(self, name):
        self.stop(name)
        self.unit_dir.joinpath(self.unit(name)).unlink(missing_ok=True)
        self._systemctl("daemon-reload")

    def status(self, name):
        r = self._systemctl("is-active", self.unit(name))
        return (r.stdout or r.stderr).strip() or "unknown"

    def other_registrations(self, name):
        mine = self.unit(name)
        return [p.name for p in self.unit_dir.glob(f"tp-{name}*.service") if p.name != mine]


# =============================================================================
#  Windows - Task Scheduler
# =============================================================================
class WindowsTaskManager(ServiceManager):
    """See the module docstring's caveat: this is the weakest of the three.

    Two structural limits worth restating at the point of use. ONLOGON means
    nothing runs when nobody is logged in, so a Windows box used as an
    always-on host needs autologon or - much better - WSL2 with the systemd
    manager above. And Task Scheduler has no supervision worth the name: the
    RestartCount below is the whole of it."""

    def task(self, name: str) -> str:
        return f"TradingPlatform_{name}{self.suffix.replace('.', '_')}"

    def install(self, name, cmd, env, workdir, log_path):
        # A .bat wrapper, because schtasks cannot express environment
        # variables and Python does not read them from the task definition.
        # It therefore contains the service environment and is gitignored.
        bat = Path(workdir) / f"_run_{name}.bat"
        lines = ["@echo off", f'cd /d "{workdir}"']
        lines += [f"set {k}={v}" for k, v in env.items() if v]
        lines.append(" ".join(f'"{a}"' if " " in str(a) else str(a) for a in cmd)
                     + f' >> "{log_path}" 2>&1')
        bat.write_text("\r\n".join(lines))
        print(f"  wrote {bat}")

        r = subprocess.run(["schtasks", "/Create", "/TN", self.task(name),
                            "/TR", f'"{bat}"',
                            "/SC", "ONLOGON",
                            "/RL", "LIMITED",   # never elevated
                            "/F"], capture_output=True, text=True)
        if r.returncode:
            # Group policy commonly forbids task creation on a managed
            # machine. Say so, rather than raising CalledProcessError with an
            # empty message - and say what still works.
            print(f"  FAILED to create {self.task(name)}: "
                  f"{(r.stderr or r.stdout).strip()}")
            print(f"  The .bat above still runs by hand: {bat}")
            return

        # Restart on failure, and do not refuse to run on battery. These are
        # the two things launchd's KeepAlive and caffeinate give away free on
        # macOS. check=False: PowerShell may be locked down by policy, and a
        # task that runs without the restart tuning is better than no task.
        subprocess.run(["powershell", "-NoProfile", "-Command", f"""
            $t = Get-ScheduledTask -TaskName "{self.task(name)}"
            $t.Settings.RestartCount = 3
            $t.Settings.RestartInterval = 'PT1M'
            $t.Settings.DisallowStartIfOnBatteries = $false
            $t.Settings.StopIfGoingOnBatteries = $false
            $t.Settings.ExecutionTimeLimit = 'PT0S'
            Set-ScheduledTask -InputObject $t
        """], check=False, capture_output=True)

    def start(self, name):
        subprocess.run(["schtasks", "/Run", "/TN", self.task(name)], check=True)
        print(f"  started {self.task(name)}")

    def stop(self, name):
        subprocess.run(["schtasks", "/End", "/TN", self.task(name)],
                       check=False, capture_output=True)
        print(f"  stopped {self.task(name)}")

    def uninstall(self, name):
        self.stop(name)
        subprocess.run(["schtasks", "/Delete", "/TN", self.task(name), "/F"],
                       check=False, capture_output=True)

    def status(self, name):
        r = subprocess.run(["schtasks", "/Query", "/TN", self.task(name)],
                           capture_output=True, text=True)
        if r.returncode:
            return "not installed"
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        return lines[-1].strip() if lines else "installed (no detail reported)"

    def other_registrations(self, name):
        mine = self.task(name)
        r = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True)
        if r.returncode:
            return []
        prefix = f"TradingPlatform_{name}"
        others = []
        for line in r.stdout.splitlines():
            fields = line.split('","')
            if not fields:
                continue
            taskname = fields[0].strip('"').lstrip("\\")
            if taskname.startswith(prefix) and taskname != mine:
                others.append(taskname)
        return others


def _shquote(s) -> str:
    import shlex

    return shlex.quote(str(s))


def manager(version_suffix: str = "") -> ServiceManager:
    if sys.platform == "darwin":
        return LaunchdManager(version_suffix)
    if sys.platform.startswith("linux"):
        return SystemdManager(version_suffix)
    if os.name == "nt":
        return WindowsTaskManager(version_suffix)
    raise RuntimeError(f"unsupported platform: {sys.platform}")


# =============================================================================
#  CLI
# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action",
                    choices=["install", "uninstall", "start", "stop",
                             "restart", "status", "logs"])
    ap.add_argument("service", nargs="?", help="one service, or all if omitted")
    ap.add_argument("--suffix", default=os.getenv("TP_LABEL_SUFFIX", ""),
                    help="version suffix, e.g. .v2.1.0 (§38.5). `tp promote` sets this.")
    args = ap.parse_args(argv)

    mgr = manager(args.suffix)
    cmds = _commands()
    names = [args.service] if args.service else [s for s in SERVICES if s in cmds]
    env = _service_env()

    if args.action == "logs":
        name = args.service or "scheduler"
        if isinstance(mgr, SystemdManager):
            # journald owns these logs on Linux; there is no file to tail.
            return subprocess.run(["journalctl", "--user", "-u", mgr.unit(name),
                                   "-f"]).returncode
        path = _log_path(name)
        if os.name == "nt":
            # No tail on Windows. Get-Content -Wait is the equivalent.
            return subprocess.run(["powershell", "-NoProfile", "-Command",
                                   f'Get-Content -Path "{path}" -Wait -Tail 50']).returncode
        return subprocess.run(["tail", "-n", "50", "-f", str(path)]).returncode

    for name in names:
        if name not in cmds:
            print(f"  {name}: skipped (set MAVERICK_CMD=... to manage it)")
            continue
        if args.action != "status":
            print(f"{args.action} {name}")

        # Refuse to install/start/restart while an OTHER registration of the
        # SAME service (different suffix, including no suffix) still exists.
        # See other_registrations()'s docstring for why this is a refusal
        # and not an automatic cleanup: this is what was missing on
        # 2026-07-26. `tp promote` uninstalls the OLD version's suffixed
        # registration on every promote, but a bare `services.py install ui`
        # (no --suffix) never touches a leftover one - so both end up
        # installed, both KeepAlive/Restart=on-failure/ONLOGON, and each one
        # resurrects the instant the other is killed for the same port. From
        # the browser's side that looks exactly like a frontend bug
        # (repeated "Failed to fetch" that clears itself after a moment) when
        # it is really "you have two of these installed". Checked for every
        # service, not just `ui`: scheduler and maverick can duplicate the
        # same way, they just do not fight over a shared port, so the
        # symptom is quieter (duplicate scan cycles, doubled API usage)
        # rather than a visible outage.
        if args.action in ("install", "start", "restart"):
            others = mgr.other_registrations(name)
            if others:
                print(f"  ERROR: {name} has {len(others)} OTHER installed registration(s) "
                      f"besides this one:")
                for other_id in others:
                    print(f"    {other_id}")
                print(f"  Refusing to {args.action} - whichever one runs would fight the "
                      f"other for the same process/port the moment either one restarts.")
                print(f"  Keep exactly ONE. Remove the ones you do not want, then re-run:")
                if isinstance(mgr, LaunchdManager):
                    for other_id in others:
                        print(f"    launchctl bootout gui/{os.getuid()}/{other_id} && "
                              f"rm ~/Library/LaunchAgents/{other_id}.plist")
                elif isinstance(mgr, SystemdManager):
                    for other_id in others:
                        print(f"    systemctl --user disable --now {other_id} && "
                              f"rm ~/.config/systemd/user/{other_id} && "
                              f"systemctl --user daemon-reload")
                elif isinstance(mgr, WindowsTaskManager):
                    for other_id in others:
                        print(f"    schtasks /End /TN {other_id} && "
                              f"schtasks /Delete /TN {other_id} /F")
                continue

        # Free the port BEFORE any action that ends in a running UI. `stop`
        # alone is excluded: it has no bind to protect, and killing a holder we
        # were not asked to start would be a surprise.
        #
        # Runs on install/start/restart because all three end with mgr.start(),
        # and the stale holder is just as fatal in each case. It is a no-op when
        # the port is free, which is the normal path.
        #
        # BUT ONLY IF A REPLACEMENT CAN ACTUALLY START (2026-07-26). This block
        # used to run unconditionally, and killing the port holder is
        # destructive while `start` may not be able to follow through. Observed
        # exactly once, which was enough:
        #
        #     restart ui
        #       not running: com.tradingplatform.ui
        #       port 8080 still held by a previous UI process (PID 64255) - terminating it
        #       ERROR: no plist at .../com.tradingplatform.ui.plist
        #
        # A working UI was killed to make room for one that could never be
        # bootstrapped, and the net effect of `restart` was to take the UI down.
        # `install` writes the plist itself so it is always safe; `start` and
        # `restart` are only safe once one exists. Check first, and say so.
        if name == "ui" and args.action in ("install", "start", "restart"):
            can_start = args.action == "install" or mgr.is_installed(name)
            if not can_start:
                print(f"  refusing to free port {UI_PORT}: {name} is not installed,")
                print(f"  so nothing could take the port afterwards. Leaving the current")
                print(f"  process alone. Fix: ./service.sh install {name}")
            else:
                if args.action == "restart":
                    mgr.stop(name)      # stop first, so the port is usually
                                        # already free by the time we look
                _free_ui_port()

        if args.action == "install":
            mgr.install(name, cmds[name], env, REPO_DIR, _log_path(name))
            mgr.start(name)
        elif args.action == "uninstall":
            mgr.uninstall(name)
        elif args.action == "start":
            mgr.start(name)
        elif args.action == "stop":
            mgr.stop(name)
        elif args.action == "restart":
            # `ui` already stopped above so the port check saw a settled state;
            # everything else stops here.
            if name != "ui":
                mgr.stop(name)
            mgr.start(name)
        elif args.action == "status":
            print(f"  {name}: {mgr.status(name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
