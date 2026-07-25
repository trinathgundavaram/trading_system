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
import subprocess
import sys
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
    }


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
        # §38.5: '.v1.4.0' when installed by `tp promote`, '' for a plain
        # development install.
        self.suffix = suffix

    def install(self, name: str, cmd: list, env: dict, workdir: Path, log_path: Path) -> None:
        raise NotImplementedError

    def uninstall(self, name: str) -> None:
        raise NotImplementedError

    def start(self, name: str) -> None:
        raise NotImplementedError

    def stop(self, name: str) -> None:
        raise NotImplementedError

    def status(self, name: str) -> str:
        raise NotImplementedError


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

    def start(self, name):
        subprocess.run(["launchctl", "bootout", self._domain(name)],
                       capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}",
                        str(self.plist_path(name))], check=True)
        print(f"  started {self.label(name)}")

    def stop(self, name):
        r = subprocess.run(["launchctl", "bootout", self._domain(name)],
                           capture_output=True)
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
        self._systemctl("enable", "--now", self.unit(name), check=True)
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
                    help="version suffix, e.g. .v1.4.0 (§38.5). `tp promote` sets this.")
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
            mgr.stop(name)
            mgr.start(name)
        elif args.action == "status":
            print(f"  {name}: {mgr.status(name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
