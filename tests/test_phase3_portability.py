"""Phase 3 (§41-§47): the portability layer, tested without a second machine.

WHAT THESE TESTS CAN AND CANNOT PROVE. They cannot prove that a Linux box
computes the same score as this Mac - only the Phase 3 exit criterion (build
two tagged images, run the same backtest, compare) can do that, and it needs
Docker. What they CAN do is prove the things that would silently break the
attempt:

  * the OS branches are exhaustive and none of them raises
  * the process-tree kill dispatches to a real implementation on every OS
  * a notification is never dropped, whatever the transport situation
  * a missing secret raises rather than returning ''
  * the clock-skew guard actually blocks a cycle
  * nothing outside the host agent shells out to a macOS-only binary

That last one is a lint, and it is the one most likely to catch a regression:
the natural way to break §47 is for someone to add a convenient `osascript`
call to a module that runs in the container.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# =============================================================================
#  §43.1  storage/platform_support.py
# =============================================================================
class TestPlatformSupport:
    def test_exactly_one_os_flag_is_true(self):
        from storage import platform_support as ps

        # IS_WSL is deliberately not exclusive - WSL is Linux - so it is
        # excluded from the count rather than being an exception to explain.
        assert sum([ps.IS_MAC, ps.IS_WINDOWS, ps.IS_LINUX]) == 1

    def test_os_name_is_a_known_string(self):
        """No `or sys.platform` escape hatch: os_name() falls back to
        sys.platform, so a disjunct on it can never be false and the test
        would assert nothing."""
        from storage import platform_support as ps

        assert ps.os_name() in ("macos", "linux", "windows", "wsl")

    def test_describe_leaks_nothing_secret(self):
        """describe() is logged at startup and returned from /api/health."""
        from storage import platform_support as ps

        blob = json.dumps(ps.describe()).lower()
        for forbidden in ("password", "token", "secret", "api_key"):
            assert forbidden not in blob

    def test_clipboard_degrades_never_raises(self):
        """A headless box has no clipboard. That is a legitimate state, and a
        scan cycle must not die because a prompt could not be copied."""
        from storage import platform_support as ps

        with mock.patch("shutil.which", return_value=None):
            ok, msg = ps.copy_to_clipboard("hello")
        assert ok is False
        assert isinstance(msg, str) and msg

    def test_open_file_reports_the_path_on_failure(self):
        """The useful answer on a headless box is not 'it failed' but 'here is
        where the file is'."""
        from storage import platform_support as ps

        with mock.patch.object(ps, "is_containerised", return_value=True):
            ok, msg = ps.open_file(REPO / "config.yaml")
        assert ok is False
        assert "config.yaml" in msg

    def test_detached_popen_kwargs_matches_the_platform(self):
        from storage import platform_support as ps

        kw = ps.detached_popen_kwargs()
        if ps.IS_WINDOWS:
            assert "creationflags" in kw
        else:
            assert kw == {"start_new_session": True}

    def test_detached_kwargs_are_accepted_by_popen(self):
        """The kwargs are only useful if Popen actually takes them."""
        from storage import platform_support as ps

        p = subprocess.Popen([sys.executable, "-c", "pass"],
                             **ps.detached_popen_kwargs())
        assert p.wait(timeout=10) == 0


# =============================================================================
#  §43.2  the process-tree kill
# =============================================================================
class TestProcessTreeKill:
    def test_dispatch_covers_this_platform(self):
        from engine import cycle_supervisor as cs

        target = ("_kill_process_tree_psutil" if cs.IS_WINDOWS
                  else "_kill_process_group_posix")
        with mock.patch.object(cs, target) as m:
            cs._kill_process_tree(999999, reason="test")
        assert m.called

    def test_legacy_alias_still_resolves(self):
        """`scripts/` and operator muscle memory refer to the old name."""
        from engine import cycle_supervisor as cs

        assert cs._kill_process_group is cs._kill_process_tree

    def test_killing_a_dead_pid_does_not_raise(self):
        """The 15-min auto-kill and a user-triggered /api/cycle/cancel can
        legitimately race to kill the same pid. Neither may raise."""
        from engine import cycle_supervisor as cs

        cs._kill_process_tree(999999, reason="already gone")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX group semantics")
    def test_posix_path_kills_a_real_child(self):
        from engine import cycle_supervisor as cs
        from storage.platform_support import detached_popen_kwargs

        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             **detached_popen_kwargs())
        try:
            cs._kill_process_tree(p.pid, reason="test")
            assert p.wait(timeout=15) != 0
        finally:
            if p.poll() is None:
                p.kill()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX group semantics")
    def test_posix_path_survives_eperm_on_an_unreaped_child(self):
        """The macOS failure this test exists for: the child dies on SIGTERM
        but stays a zombie until its parent reaps it, and Darwin/BSD answer a
        signal aimed at an exited process with EPERM, not ESRCH. Linux answers
        0, so without this test the bug is invisible on CI and shows up only
        on the machine that ships releases.

        Uncaught it escaped run_supervised()'s TimeoutExpired handler before
        mark_cycle_killed() ran - a killed cycle logged as a clean finish -
        and made /api/cycle/cancel return 500. Simulated rather than staged
        with a real zombie, because whether a given kernel returns EPERM or
        ESRCH is the very thing that is not portable."""
        from engine import cycle_supervisor as cs

        calls = []

        def _killpg(pgid, sig):
            calls.append(sig)
            if sig == 0:
                raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(cs.os, "getpgid", lambda pid: pid), \
                mock.patch.object(cs.os, "killpg", _killpg):
            cs._kill_process_tree(4242, reason="eperm")

        assert calls[0] == cs.signal.SIGTERM
        assert cs.signal.SIGKILL not in calls, \
            "EPERM means no member of the group could be signalled - escalating is pointless"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX group semantics")
    def test_posix_path_survives_eperm_from_getpgid(self):
        """Same reasoning one call earlier: a pid that is not ours at all."""
        from engine import cycle_supervisor as cs

        def _boom(pid):
            raise PermissionError(1, "Operation not permitted")

        with mock.patch.object(cs.os, "getpgid", _boom):
            cs._kill_process_tree(4242, reason="not ours")

    def test_psutil_path_is_importable(self):
        """psutil is what gives Windows any hang protection at all. If it is
        missing, that is a real finding, not a skip."""
        import psutil  # noqa: F401

        from engine import cycle_supervisor as cs

        cs._kill_process_tree_psutil(999999, reason="no such process")


# =============================================================================
#  §43.3  notification transports
# =============================================================================
class TestNotificationTransports:
    def test_log_transport_always_succeeds(self):
        """This is the whole point of the chain: the pre-Phase-3 code dropped
        a notification with a warning when osascript failed, which on a
        headless box or in a container was always."""
        from engine import notifications as n

        assert n._transport_log("t", "m", None, {}, "info") is True

    def test_a_notification_is_never_lost(self):
        """Patched through TRANSPORTS, not through the module attributes.

        notify() dispatches on the dict, whose values were bound at module
        definition - so mock.patch.object(n, '_transport_desktop') does NOT
        reach it, and a test written that way runs the real desktop transport
        and fires an actual popup while asserting nothing."""
        from engine import notifications as n

        cfg = {"notifications": {"enabled": True,
                                 "transports": ["desktop", "webhook", "log"]}}
        with mock.patch.dict(n.TRANSPORTS, {
            "desktop": lambda *a, **k: False,
            "webhook": lambda *a, **k: False,
        }):
            assert n.notify(cfg, "title", "message") is True

    def test_order_is_respected_and_stops_at_the_first_success(self):
        from engine import notifications as n

        calls = []

        def make(name, result):
            def fn(*a, **k):
                calls.append(name)
                return result
            return fn

        cfg = {"notifications": {"enabled": True, "transports": ["webhook", "desktop", "log"]}}
        with mock.patch.dict(n.TRANSPORTS, {
            "webhook": make("webhook", False),
            "desktop": make("desktop", True),
            "log": make("log", True),
        }):
            n.notify(cfg, "t", "m")
        assert calls == ["webhook", "desktop"]

    def test_a_raising_transport_does_not_take_the_caller_down(self):
        """A notification failure must never break a scan cycle."""
        from engine import notifications as n

        def boom(*a, **k):
            raise RuntimeError("transport exploded")

        cfg = {"notifications": {"enabled": True, "transports": ["desktop", "log"]}}
        with mock.patch.dict(n.TRANSPORTS, {"desktop": boom}):
            assert n.notify(cfg, "t", "m") is True

    def test_disabled_means_nothing_is_attempted(self):
        from engine import notifications as n

        called = []
        cfg = {"notifications": {"enabled": False}}
        with mock.patch.dict(n.TRANSPORTS,
                             {"log": lambda *a, **k: called.append(1) or True}):
            assert n.notify(cfg, "t", "m") is False
        assert not called

    def test_unknown_transport_is_skipped_not_fatal(self):
        from engine import notifications as n

        cfg = {"notifications": {"enabled": True, "transports": ["carrier_pigeon", "log"]}}
        assert n.notify(cfg, "t", "m") is True

    def test_desktop_transport_is_skipped_in_a_container(self):
        """§47: the engine image must not try to draw a popup."""
        from engine import notifications as n

        with mock.patch("storage.platform_support.is_containerised", return_value=True):
            assert n._transport_desktop("t", "m", None, {}, "info") is False

    def test_webhook_without_a_url_declines_quietly(self):
        from engine import notifications as n

        assert n._transport_webhook("t", "m", None, {}, "info") is False

    def test_send_critical_delivers_at_critical_severity(self):
        from engine import notifications as n

        seen = []
        with mock.patch.dict(n.TRANSPORTS,
                             {"desktop": lambda *a, **k: False,
                              "webhook": lambda *a, **k: False,
                              "log": lambda t, m, s, c, sev="info": seen.append(sev) or True}):
            n.send_critical("TRADING HALTED", "kill switch tripped")
        assert seen == ["critical"]

    def test_send_critical_always_writes_the_durable_log(self, caplog):
        """§9, and the part that is easy to lose: the chain SHORT-CIRCUITS at
        the first success, so a desktop popup that lands means the `log`
        transport is never reached. A popup is dismissed and gone; 'TRADING
        HALTED' has to still be in the log tomorrow when someone asks why the
        system stopped. So send_critical logs unconditionally."""
        import logging

        from engine import notifications as n

        with mock.patch.dict(n.TRANSPORTS, {"desktop": lambda *a, **k: True}), \
             caplog.at_level(logging.CRITICAL, logger="engine.notifications"):
            n.send_critical("TRADING HALTED", "kill switch tripped")
        assert any("TRADING HALTED" in r.message for r in caplog.records)

    def test_send_critical_builds_its_own_enabled_config(self):
        """Someone who muted buy-signal chatter has not consented to missing
        'TRADING HALTED'. send_critical takes no cfg argument at all - it
        constructs one with enabled=True - so there is no setting anywhere
        that can suppress it. Asserted on the cfg it actually passes down."""
        from engine import notifications as n

        with mock.patch.object(n, "notify", return_value=True) as notify:
            n.send_critical("TRADING HALTED", "kill switch tripped")
        cfg = notify.call_args[0][0]
        assert cfg["notifications"]["enabled"] is True
        assert notify.call_args.kwargs["severity"] == "critical"


# =============================================================================
#  §44  cross-platform secrets
# =============================================================================
class TestSecrets:
    def test_environment_wins_over_the_keyring(self):
        """Environment BEFORE keyring is what makes containers and CI
        possible. Reversing the two would make them impossible."""
        from storage import secrets

        with mock.patch.dict("os.environ", {"UI_AUTH_TOKEN": "from-env"}), \
             mock.patch.object(secrets, "_keychain", return_value="from-keyring"):
            assert secrets.get("UI_AUTH_TOKEN") == "from-env"

    def test_a_missing_required_secret_raises(self):
        """It must NEVER return ''. An empty UI_AUTH_TOKEN compares equal to a
        blank header and silently unauthenticates every write endpoint."""
        from storage import secrets

        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(secrets, "load_dotenv", return_value=0), \
             mock.patch.object(secrets, "_keychain", return_value=""):
            with pytest.raises(RuntimeError):
                secrets.get("DEFINITELY_NOT_SET_ANYWHERE")

    def test_optional_secret_returns_empty_not_raise(self):
        from storage import secrets

        with mock.patch.object(secrets, "_keychain", return_value=""):
            assert secrets.get("DEFINITELY_NOT_SET_ANYWHERE", required=False) == ""

    def test_strict_mode_skips_the_plaintext_env_file(self):
        from storage import secrets

        with mock.patch.dict("os.environ", {"TP_STRICT_SECRETS": "1"}, clear=True), \
             mock.patch.object(secrets, "load_dotenv") as loader, \
             mock.patch.object(secrets, "_keychain", return_value=""):
            secrets.get("ANYTHING", required=False)
        assert not loader.called

    def test_a_pre_44_keyring_item_is_still_found(self):
        """§44 moved the `tp_` prefix from the Keychain SERVICE field to the
        ACCOUNT field, which makes the old and new items two different items -
        so installing `keyring` made every previously stored secret vanish.
        Silently: get() falls through to the environment, so the caller
        receives '' rather than an error, and an empty UI_AUTH_TOKEN 503s every
        write endpoint on a dashboard that looks healthy."""
        from storage import secrets

        class _LegacyOnly:
            def get_password(self, service, account):
                # The pre-§44 layout: service='tp_NAME', account=$USER.
                return "legacy-value" if service == "tp_UI_AUTH_TOKEN" else None

            def set_password(self, service, account, value):
                self.written = (service, account, value)

        kr = _LegacyOnly()
        secrets._keychain.cache_clear()
        with mock.patch.object(secrets, "_backend", return_value=kr):
            assert secrets._keychain("UI_AUTH_TOKEN") == "legacy-value"
        secrets._keychain.cache_clear()
        assert kr.written == (secrets.SERVICE, "tp_UI_AUTH_TOKEN", "legacy-value"), \
            "a legacy secret must be copied forward, so the migration happens once"

    def test_the_current_location_wins_over_the_legacy_one(self):
        """Order matters: after a rotation the new item is the real one, and
        reading the stale legacy copy would resurrect a retired credential."""
        from storage import secrets

        class _Both:
            def get_password(self, service, account):
                return "current" if service == secrets.SERVICE else "legacy"

            def set_password(self, *a):
                raise AssertionError("nothing to migrate")

        secrets._keychain.cache_clear()
        with mock.patch.object(secrets, "_backend", return_value=_Both()):
            assert secrets._keychain("UI_AUTH_TOKEN") == "current"
        secrets._keychain.cache_clear()

    def test_the_security_binary_is_still_tried_when_keyring_finds_nothing(self):
        """Before this, an installed keyring short-circuited the `security`
        path entirely - which is precisely the machine that has a pre-§44 item
        to find."""
        from storage import secrets

        class _Empty:
            def get_password(self, *a):
                return None

            def set_password(self, *a):
                pass

        secrets._keychain.cache_clear()
        with mock.patch.object(secrets, "_backend", return_value=_Empty()), \
             mock.patch.object(secrets, "_keychain_security_binary",
                               return_value="from-security") as sec:
            assert secrets._keychain("UI_AUTH_TOKEN") == "from-security"
        secrets._keychain.cache_clear()
        assert sec.called

    def test_set_without_a_backend_raises_rather_than_writing_a_file(self):
        """Somewhere that cannot store a secret encrypted must say so out
        loud. Writing plaintext 'as a convenience' is the failure §3 exists to
        prevent."""
        from storage import secrets

        secrets._backend.cache_clear()
        with mock.patch.object(secrets, "_backend", return_value=None):
            with pytest.raises(RuntimeError):
                secrets.set_("UI_AUTH_TOKEN", "value")

    def test_export_env_writes_owner_only(self, tmp_path):
        from storage import secrets

        out = tmp_path / ".env.runtime"
        with mock.patch.object(secrets, "get",
                               side_effect=lambda k, **kw: "v" if k == "UI_AUTH_TOKEN" else ""):
            secrets.export_env(out)
        assert out.read_text().strip() == "UI_AUTH_TOKEN=v"
        if sys.platform != "win32":
            assert oct(out.stat().st_mode)[-3:] == "600"

    def test_redact_never_returns_the_whole_value(self):
        from storage import secrets

        assert secrets.redact("supersecretvalue") != "supersecretvalue"

    def test_keyring_backend_name_never_raises(self):
        from storage import secrets

        assert isinstance(secrets.keyring_backend_name(), str)


# =============================================================================
#  §45  launchd restart is not stop-then-start
# =============================================================================
class TestLaunchdRestartRace:
    """`launchctl bootout` returns when the request is ACCEPTED, not when the
    job is gone. restart raced its own bootout and launchd answered

        Bootstrap failed: 5: Input/output error
        Try re-running the command as root for richer errors.

    which names neither the cause nor the fix and sends you after a
    permissions problem that does not exist."""

    def _mgr(self):
        import importlib.machinery
        import importlib.util

        loader = importlib.machinery.SourceFileLoader(
            "services", str(REPO / "scripts" / "services.py"))
        spec = importlib.util.spec_from_loader("services", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod, mod.LaunchdManager()

    def test_start_waits_for_the_label_to_leave_the_domain(self):
        mod, mgr = self._mgr()
        calls = []
        # Loaded for the first two polls, then gone.
        states = iter([True, True, False, False, False])

        with mock.patch.object(mgr, "_is_loaded", side_effect=lambda n: next(states)), \
             mock.patch.object(mod.subprocess, "run",
                               side_effect=lambda cmd, **kw: calls.append(cmd[1])
                               or mock.Mock(returncode=0)):
            mgr.start("scheduler")

        assert calls == ["bootout", "bootstrap"], calls

    def test_bootstrap_is_not_preceded_by_a_pointless_bootout(self):
        """Nothing loaded means nothing to tear down. Booting out something
        that is not there produced the 'not running' noise that trained people
        to ignore this command's output."""
        mod, mgr = self._mgr()
        calls = []
        with mock.patch.object(mgr, "_is_loaded", return_value=False), \
             mock.patch.object(mod.subprocess, "run",
                               side_effect=lambda cmd, **kw: calls.append(cmd[1])
                               or mock.Mock(returncode=0)):
            mgr.start("scheduler")
        assert calls == ["bootstrap"], calls

    def test_is_loaded_is_false_when_launchctl_is_absent(self):
        """'Is it loaded?' has a sensible answer on a machine with no launchd,
        and it is no - not a traceback."""
        mod, mgr = self._mgr()
        with mock.patch.object(mod.subprocess, "run", side_effect=FileNotFoundError):
            assert mgr._is_loaded("scheduler") is False

    def test_the_wait_gives_up_rather_than_hanging(self):
        mod, mgr = self._mgr()
        mgr.BOOTOUT_TIMEOUT_S = 0.3
        with mock.patch.object(mgr, "_is_loaded", return_value=True):
            assert mgr._wait_until_gone("scheduler") is False


# =============================================================================
#  §13  the dependency guard reads metadata, not `pip freeze`
# =============================================================================
class TestDependencyCheck:
    """§13 is only worth having if its output is believable. On an Anaconda
    env it reported sixteen present, working packages as NOT INSTALLED -
    including pytest, while pytest was running it - because a conda-built or
    locally-installed distribution appears in `pip freeze` as
    `pandas @ file:///croot/...`, not `pandas==2.3.3`, and the parser kept only
    lines containing '=='. Two of the sixteen were flagged SCORE-AFFECTING, so
    the loudest possible warning was also the least trustworthy one."""

    def _mod(self, name, path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, REPO / path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_installed_set_includes_a_package_pip_freeze_renders_as_a_url(self):
        """pytest itself is the fixture: it is unquestionably importable, so
        any correct implementation reports it, whatever form it was installed
        in."""
        check_deps = self._mod("check_deps", "scripts/check_deps.py")

        have = check_deps.installed()
        assert check_deps.norm("pytest") in have
        assert have[check_deps.norm("pytest")]

    def test_no_pinned_package_is_reported_missing_while_importable(self):
        """The specific false alarm: a name pinned in requirements.txt that
        imports fine here must not appear as NOT INSTALLED."""
        import importlib.util

        check_deps = self._mod("check_deps", "scripts/check_deps.py")
        want, have = check_deps.wanted(), check_deps.installed()

        lying = []
        for pinned in want:
            if pinned in have:
                continue
            mod = pinned.replace("-", "_")
            try:
                found = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                # find_spec raises rather than returning None for a name whose
                # parent package is absent, and for an already-imported module
                # with no __spec__. Neither is evidence either way.
                continue
            if found:
                lying.append(pinned)
        assert not lying, f"reported NOT INSTALLED but importable: {lying}"

    def test_the_lock_body_is_portable(self):
        """requirements.lock.txt used to be raw freeze output, so a
        `@ file:///croot/...` line - a path on one machine - could land in the
        file whose whole purpose (§42) is rebuilding this environment
        elsewhere."""
        pin = self._mod("pin_requirements", "scripts/pin_requirements.py")

        _, body = pin.freeze()
        assert body.strip(), "a lock body with no entries is not a lock"
        for line in body.splitlines():
            assert "==" in line and " @ " not in line and "file://" not in line, line


# =============================================================================
#  §42.4  the clock-skew guard
# =============================================================================
@pytest.fixture(scope="module")
def scheduler_module():
    """Import scheduler.py without requiring the reference TA backend.

    engine/ticker_analyzer.py refuses to import when pandas_ta is missing -
    correctly, since a score computed on the fallback engine is not comparable
    with anything (§13). But pandas_ta has no wheel below Python 3.12, so on a
    CI runner or a developer machine still on 3.11 the ENTIRE test suite would
    be unrunnable, including these tests, which never compute a score.
    TP_REQUIRE_REFERENCE_TA=0 is the documented escape hatch for exactly this
    and is scoped to this fixture - nothing here reads an indicator."""
    import os

    prior = os.environ.get("TP_REQUIRE_REFERENCE_TA")
    os.environ["TP_REQUIRE_REFERENCE_TA"] = "0"
    try:
        import scheduler

        yield scheduler
    finally:
        if prior is None:
            os.environ.pop("TP_REQUIRE_REFERENCE_TA", None)
        else:
            os.environ["TP_REQUIRE_REFERENCE_TA"] = prior


class TestClockSkew:
    def _fresh(self, scheduler):
        scheduler._CLOCK_SKEW_CACHE.update({"at": 0.0, "value": None})
        return scheduler

    def test_unreachable_reference_returns_none_not_zero(self, scheduler_module):
        """None means 'cannot tell' and the caller proceeds. Returning 0.0
        would be a claim the clock is fine, which is worse than admitting
        ignorance."""
        s = self._fresh(scheduler_module)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")):
            assert s._clock_skew_seconds(timeout=0.1) is None

    def test_the_answer_is_cached(self, scheduler_module):
        """This runs at the top of every cycle; it must not be a request per
        cycle."""
        s = self._fresh(scheduler_module)
        calls = []

        class FakeResp:
            headers = {"Date": "Sat, 25 Jul 2026 12:00:00 GMT"}

            def __enter__(self):
                calls.append(1)
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            s._clock_skew_seconds()
            s._clock_skew_seconds()
        assert len(calls) == 1

    def test_a_large_skew_blocks_the_cycle(self, scheduler_module):
        """The Docker-Desktop-after-sleep case. Every market-hours and stop
        decision below this point is a function of the local clock."""
        scheduler = scheduler_module

        with mock.patch.object(scheduler, "_clock_skew_seconds", return_value=9999), \
             mock.patch.object(scheduler, "load_config",
                               return_value={"trading": {"max_clock_skew_seconds": 120},
                                             "risk": {"kill_switch_triggered": False}}), \
             mock.patch.object(scheduler, "is_market_open", return_value=True) as market, \
             mock.patch.object(scheduler, "db") as db:
            scheduler._run_cycle_impl(force=False)

        # The guard sits ABOVE the market-hours check, so nothing downstream
        # of a bad clock is even consulted.
        assert not market.called
        blocked = [c for c in db.log_cycle.call_args_list
                   if "clock_skew" in str(c)]
        assert blocked, "a skewed clock must be RECORDED, not silently skipped"


# =============================================================================
#  §47  the architectural split
# =============================================================================
# Matched as a QUOTED string, i.e. how they appear in a subprocess argv - not
# as bare words. Every one of these files discusses these binaries in prose,
# and a lint that fires on its own explanatory comment is a lint people delete.
MAC_ONLY_BINARIES = ("osascript", "pbcopy", "caffeinate", "launchctl",
                     "pmset", "security")
_BINARY_RE = re.compile(
    r"""["'](%s)["']""" % "|".join(MAC_ONLY_BINARIES))

# The only places allowed to name a macOS binary: the host agent (that is its
# entire job), the platform layer (which is the abstraction), the service
# manager (launchd IS one of its three backends), the version manager, the
# deploy plists, the kept .bak originals, and this test.
ALLOWED = {
    "scripts/tp_agent.py", "scripts/services.py", "scripts/tp",
    "scripts/bootstrap.py", "storage/platform_support.py",
    "storage/secrets.py", "engine/notifications.py",
    "service.sh", "tests/test_phase3_portability.py",
}


def _engine_sources():
    """Every Python file that could end up running inside the container.

    Scans scripts/ and mcp_clients/ too. Leaving them out is how the lint
    quietly stops working: the four scripts/ entries in ALLOWED would be
    inert, and a convenient `osascript` added to an MCP client would sail
    through."""
    skip_dirs = {"tests", "node_modules", "output", "docs", "deploy",
                 "__pycache__", ".git", "migrations", "prompts"}
    for p in REPO.rglob("*.py"):
        rel = p.relative_to(REPO).as_posix()
        if rel in ALLOWED or ".bak" in rel:
            continue
        if set(Path(rel).parts) & skip_dirs:
            continue
        yield rel, p
    # scripts/tp has no .py suffix and is the version manager - rglob misses it.
    for extra in ("scripts/tp",):
        if extra not in ALLOWED and (REPO / extra).exists():
            yield extra, REPO / extra


class TestNoHostCallsInTheEngine:
    """§47's whole claim is that 99% of the system does not know what OS it is
    on. The natural way to break that is for someone to add a convenient
    osascript call to a module that runs inside the container - where it does
    not merely fail, it fails silently. This is the lint that catches it."""

    def test_no_macos_binary_outside_the_host_agent(self):
        offenders = []
        for rel, path in _engine_sources():
            for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if _BINARY_RE.search(line):
                    offenders.append(f"{rel}:{i}: {stripped[:100]}")
        assert not offenders, (
            "macOS-only calls found outside the host agent (§47.1). The engine "
            "runs in a container where these do not exist - route the event "
            "through db.log_ui_event() and let scripts/tp_agent.py deliver it:\n  "
            + "\n  ".join(offenders))


class TestContainerArtefacts:
    def test_dockerfile_asserts_the_things_that_fail_silently(self):
        text = (REPO / "Dockerfile").read_text()
        # tzdata: without it every market-hours check is wrong by 4-5 hours.
        assert "tzdata" in text
        # The build must FAIL rather than silently ship the fallback engine.
        assert "import pandas_ta" in text
        # And that the zone database is genuinely readable, not just installed.
        assert "ZoneInfo('America/New_York')" in text
        # Credentials live in this container's environment; root does not.
        assert "USER trader" in text
        # Volume mountpoints must be owned by uid 1000 BEFORE the USER switch,
        # or Docker creates them root-owned and the container cannot write.
        assert text.index("chown -R trader:trader") < text.index("USER trader")

    def test_dockerfile_digest_pin_is_still_outstanding(self):
        """An honest test rather than a passing one.

        The reproducibility argument in §42 rests on pinning the base image by
        DIGEST; the tree currently pins by TAG, which is a moving target. This
        test documents that gap and will fail - deliberately, as a reminder -
        the moment someone writes the digest in, at which point delete it."""
        text = (REPO / "Dockerfile").read_text()
        digest_pinned = "python:3.12-slim@sha256:" in text
        assert not digest_pinned, (
            "the base image is now digest-pinned - delete this test, and update "
            "docs/releases/v2.1.0.md, which lists the tag pin as outstanding")

    def test_compose_binds_the_ui_to_loopback_only(self):
        """'8080:8080' would publish the UI to the whole network AND bypass
        most host firewalls. §4 exists to prevent exactly that."""
        text = (REPO / "docker-compose.yml").read_text()
        assert "127.0.0.1:${TP_UI_PORT:-8080}:8080" in text

    def test_compose_fails_closed_on_live_execution(self):
        text = (REPO / "docker-compose.yml").read_text()
        assert "TP_FORCE_PAPER: ${TP_FORCE_PAPER:-1}" in text

    def test_compose_caps_logs(self):
        """E-10 was 77 MB of unrotated log."""
        text = (REPO / "docker-compose.yml").read_text()
        assert "max-size" in text

    def test_dockerignore_excludes_secrets(self):
        """A file copied into a layer stays in that layer even if a later
        layer deletes it - anyone who can pull the image can read it.

        Matched on whole LINES: every one of these patterns also appears in
        the file's own prose header, so a substring check would pass even if
        every actual rule were deleted."""
        rules = {ln.strip() for ln in (REPO / ".dockerignore").read_text().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")}
        for pattern in (".env", ".env.*", "*.pickle", "output/", ".tokens/"):
            assert pattern in rules, f"{pattern!r} is not an active .dockerignore rule"


class TestUiEventOutbox:
    def test_log_ui_event_survives_a_backend_without_pg_notify(self):
        """The NOTIFY is best-effort: a SQLite-backed test database or a
        payload over Postgres's 8000-byte limit must not fail the write that
        actually matters."""
        from storage.database import Database

        db = Database.__new__(Database)
        executed = []

        class FakeConn:
            def execute(self, sql, params=None):
                executed.append(sql)
                if "pg_notify" in sql:
                    raise RuntimeError("no such function")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(Database, "_conn", return_value=FakeConn()):
            db.log_ui_event("notify", {"title": "t", "body": "b"})

        assert any("INSERT INTO ui_events" in s for s in executed)
        assert any("pg_notify" in s for s in executed)


class TestServerPromptEndpoints:
    def test_the_pbcopy_endpoint_is_gone(self):
        """§47.5: the server no longer shells out to the host clipboard.

        Matched on the route DECORATOR, not on the string: both files discuss
        the removed endpoint in a comment explaining why it went away, and
        that comment is worth keeping."""
        text = (REPO / "server.py").read_text()
        assert '@app.post("/api/prompt/copy")' not in text
        assert '"pbcopy"' not in text

    def test_the_raw_endpoint_exists(self):
        assert '@app.get("/api/prompt/raw"' in (REPO / "server.py").read_text()

    def test_the_ui_copies_in_the_browser(self):
        text = (REPO / "ui" / "index.html").read_text()
        assert "navigator.clipboard" in text
        assert 'authFetch("/api/prompt/copy"' not in text


class TestScriptsAreValidPython:
    """§46 rewrote three shell scripts in Python. A syntax error in any of
    them is a version manager that cannot install a version."""

    @pytest.mark.parametrize("rel", [
        "scripts/tp", "scripts/tp_agent.py", "scripts/services.py",
        "scripts/bootstrap.py", "scripts/healthcheck.py",
    ])
    def test_compiles(self, rel):
        src = (REPO / rel).read_text()
        compile(src, rel, "exec")

    def test_tp_root_is_platform_appropriate(self):
        """Not ~/tp everywhere: on a domain-joined Windows machine the roaming
        profile is synced over the network at every login, and this directory
        holds gigabytes."""
        import importlib.machinery
        import importlib.util

        # SourceFileLoader rather than exec(): scripts/tp has no .py suffix
        # and resolves REPO from __file__, which a bare exec does not define.
        loader = importlib.machinery.SourceFileLoader(
            "tp_cli", str(REPO / "scripts" / "tp"))
        spec = importlib.util.spec_from_loader("tp_cli", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)

        root = module.tp_root()
        assert root.is_absolute()
        # The parser must build - a broken subcommand table is a version
        # manager that cannot install a version.
        assert module.build_parser().parse_args(["install", "v1.0.0"]).tag == "v1.0.0"

    def test_bootstrap_runs_and_reports(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "bootstrap.py")],
                           capture_output=True, text=True, timeout=120)
        # Exit code depends on this machine; the contract is that it always
        # produces a readable report rather than a traceback.
        assert "bootstrap check" in r.stdout
        assert "Traceback" not in r.stderr
