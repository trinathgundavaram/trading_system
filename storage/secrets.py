"""Secret resolution for the whole platform. One place, one order.

Phase 0 step 0.2 (§3, §34.3, §39.1). Before this module, credentials were read
with bare ``os.getenv`` calls scattered across live_trader, market_data,
robinhood_mcp and friends, each with its own ad-hoc ``.env`` loader, and two
secrets (the Robinhood account number and the UI auth token) sat as literals in
the versioned ``config.yaml``.

Resolution order, highest priority first:

  1. The process environment. Set by ``scripts/tp run``, by launchd, or by the
     shell. This is what makes containers and CI work with no file on disk.
  2. ``.env`` at the repo root. Loaded once, lazily, and NEVER overriding a
     variable that is already set - so an explicit export always wins. ``.env``
     is gitignored (see .gitignore) and must never be committed.
  3. The OS keyring, under the service name ``tp_<NAME>``. Populated by
     ``./scripts/tp secrets import``. Encrypted at rest and survives a repo
     leak.

PHASE 3 (§44) - THE KEYRING IS NO LONGER macOS-ONLY. This step used to shell
out to ``security find-generic-password``, a binary that exists only on
macOS. Everywhere else it silently returned '' and the whole system fell back
to the environment - which in practice means a plaintext file, which is the
exact thing §3 and §39 exist to eliminate. It now goes through the ``keyring``
package, which wraps the native store on each platform:

  macOS      Keychain
  Windows    Credential Locker (Windows Credential Manager)
  Linux      Secret Service (GNOME Keyring / KWallet) via D-Bus
  headless   ``keyrings.cryptfile`` - an encrypted file, passphrase-protected

The order matters and is deliberate. Environment BEFORE keyring means a
container or a CI run can inject a secret with no OS store present at all -
reverse the two and containers become impossible. Keyring before nothing
means an interactive machine picks up the encrypted store automatically.

``TP_STRICT_SECRETS=1`` removes step 2, the plaintext ``.env``, leaving only
the environment and the keyring. §44 argues that a plaintext fallback means
the safe path is whichever one the machine happens to make convenient. It is
opt-in rather than default only because the running installation has a
populated ``.env`` today and silently ignoring it would look like a
credential outage rather than a policy change.

A missing REQUIRED secret raises. It does not return ''. An empty auth token
compares equal to a blank header and would silently unauthenticate every write
endpoint on the UI, which is strictly worse than a crash at startup.

Nothing here writes a secret to disk except ``export_env()``, which exists so
a container can be handed its environment and which writes mode 0600 to a
gitignored path. Nothing passes a secret as a command-line argument, where
``ps`` would show it to every local process.
"""
from __future__ import annotations

import functools
import logging
import os
import platform
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("TP_ENV_FILE", REPO_DIR / ".env"))

# The complete set of names this system treats as secret. `scripts/tp secrets`
# and `scripts/tp doctor` read the same list, so there is exactly one place to
# add a new credential.
SECRET_KEYS = (
    # --- brokerage ---
    "ROBINHOOD_USERNAME",
    "ROBINHOOD_PASSWORD",
    "ROBINHOOD_TOTP_SECRET",
    "RH_ACCOUNT_NUMBER",
    # --- web UI ---
    "UI_AUTH_TOKEN",
    # --- database ---
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    # --- market data / MCP ---
    "ALPHAVANTAGE_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "TIINGO_API_KEY",
    "TWELVEDATA_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_API_SECRET",
    "TRAYD_MCP_URL",
    "TRAYD_AUTH_TOKEN",
    "NVIDIA_API_KEY",
)

# Values that are secret-ish and must reach a container, but whose absence is
# not a fault. Kept out of SECRET_KEYS so `tp doctor` and `missing()` do not
# report a healthy install as incomplete; included in `export_env()` so the
# container still gets them. §43.3's webhook URL embeds a private ntfy topic,
# which is a bearer credential in all but name.
OPTIONAL_KEYS = (
    "NOTIFY_WEBHOOK_URL",
    "EDGAR_USER_AGENT",
)

_ENV_LOADED = False
_ENV_LOCK = threading.Lock()


def load_dotenv(path: Path | str | None = None) -> int:
    """Load ``.env`` into ``os.environ`` without clobbering what is already set.

    Idempotent and thread-safe: several modules import this at module scope and
    the second call is a no-op. Returns the number of variables newly set.
    """
    global _ENV_LOADED
    with _ENV_LOCK:
        if path is None and _ENV_LOADED:
            return 0
        target = Path(path) if path is not None else ENV_PATH
        count = 0
        try:
            with open(target) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    # strip `export ` prefix, then surrounding quotes
                    if k.startswith("export "):
                        k = k[len("export "):].strip()
                    v = v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    if k and v and k not in os.environ:
                        os.environ[k] = v
                        count += 1
        except FileNotFoundError:
            pass
        except Exception as e:  # a malformed .env must not take the process down
            logger.warning(f"storage.secrets: .env load failed ({target}): {e}")
        if path is None:
            _ENV_LOADED = True
        return count


SERVICE = "trading_platform"


def _strict() -> bool:
    """True when the plaintext ``.env`` tier is disabled (see module docstring)."""
    return os.getenv("TP_STRICT_SECRETS", "").strip() in ("1", "true", "yes")


@functools.lru_cache(maxsize=1)
def _backend():
    """The keyring module, or None if this machine has no usable store.

    Probed once, at first use, and cached. The probe read is not decoration:
    on headless Linux ``keyring`` resolves happily to a backend that only
    raises when you actually touch it, so without reading a dummy key here
    the failure would surface at the first live Robinhood login rather than
    at startup."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        kr = keyring.get_keyring()
        if isinstance(kr, FailKeyring):
            raise RuntimeError("no usable keyring backend on this machine")
        keyring.get_password(SERVICE, "__probe__")
        logger.debug(f"storage.secrets: keyring backend {type(kr).__name__}")
        return keyring
    except ImportError:
        logger.debug("storage.secrets: keyring not installed - environment and .env only")
        return None
    except Exception as e:
        logger.warning(
            f"storage.secrets: OS keyring unavailable ({e}); falling back to the "
            f"environment. On headless Linux: pip install keyrings.cryptfile and see "
            f"docs - §44.")
        return None


def keyring_backend_name() -> str:
    """For ``tp doctor`` and /api/health. Never touches a secret value."""
    kr = _backend()
    if kr is None:
        return "none"
    try:
        return type(kr.get_keyring()).__name__
    except Exception:
        return "unknown"


def _keychain_security_binary(name: str) -> str:
    """Read the PRE-§44 item with the macOS ``security`` binary. '' if absent."""
    if platform.system() != "Darwin":
        return ""
    try:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-a", os.environ.get("USER", ""), "-s", f"tp_{name}", "-w"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


@functools.lru_cache(maxsize=64)
def _keychain(name: str) -> str:
    """Read ``tp_<name>`` from the OS keyring. '' if absent or unavailable.

    THREE LOCATIONS ARE READ, AND THE ORDER IS A MIGRATION (2026-07-25). §44
    shipped claiming the ``tp_`` prefix meant "the Keychain items are the same
    items, the library reading them is what changed". That was wrong, and it
    is the kind of wrong that produces a silent credential outage on upgrade:

        pre-§44   security -s tp_UI_AUTH_TOKEN -a $USER
                  -> Keychain service='tp_UI_AUTH_TOKEN', account=$USER
        §44       keyring.get_password("trading_platform", "tp_UI_AUTH_TOKEN")
                  -> Keychain service='trading_platform', account='tp_UI_AUTH_TOKEN'

    The prefix moved from the SERVICE field to the ACCOUNT field and the
    service became a constant, so these are two different Keychain items.
    Every secret stored by the old code became invisible to the new code the
    moment ``keyring`` was installed - and because ``get()`` falls through to
    the environment and ``.env`` first, the symptom is not an error but an
    empty string arriving somewhere that treats empty as "not configured".
    On a machine whose UI token lived only in the Keychain, that silently
    503s every write endpoint on the dashboard.

    So: read the new location, then the legacy location through keyring, then
    the ``security`` binary (which is also the path when keyring is not
    installed at all). Anything found in a legacy location is written forward
    to the new one, so the migration happens once, on first read, without
    anybody having to know it was needed."""
    kr = _backend()
    if kr is not None:
        try:
            val = kr.get_password(SERVICE, f"tp_{name}")
            if val:
                return val
        except Exception as e:
            logger.warning(f"storage.secrets: keyring read failed for {name}: {e}")
        try:
            legacy = kr.get_password(f"tp_{name}", os.environ.get("USER", "")) or ""
        except Exception:
            legacy = ""
        if legacy:
            return _migrate_forward(name, legacy, "the pre-§44 keyring location")

    found = _keychain_security_binary(name)
    if found:
        return _migrate_forward(name, found, "the pre-§44 `security` item")
    return ""


def _migrate_forward(name: str, value: str, where: str) -> str:
    """Copy a legacy secret into the §44 location. Never fatal, never logs the
    value. The old item is deliberately NOT deleted: a rollback to v2.0.0 has
    to keep working, and an upgrade that destroys the only copy of a
    credential is a worse failure than reading two places forever."""
    kr = _backend()
    if kr is not None:
        try:
            kr.set_password(SERVICE, f"tp_{name}", value)
            logger.warning(f"storage.secrets: {name} was found in {where} and has been "
                           f"copied to the current one. The old item is left in place "
                           f"for rollback; `./scripts/tp secrets check` now sees both.")
        except Exception as e:
            logger.warning(f"storage.secrets: {name} read from {where}, but writing it "
                           f"to the current location failed ({e}) - it will be read from "
                           f"the old one again next time.")
    return value


def set_(name: str, value: str) -> None:
    """Write one secret to the OS keyring.

    Raises rather than falling back to a file. Somewhere that cannot store a
    secret encrypted should say so out loud; writing it to disk in plaintext
    as a convenience is precisely the failure §3 and §44 exist to prevent."""
    if name not in SECRET_KEYS:
        logger.warning(f"storage.secrets: {name!r} is not in SECRET_KEYS - "
                       f"add it there so `tp doctor` knows to check it")
    kr = _backend()
    if kr is None:
        raise RuntimeError(
            f"No keyring backend on this machine, so {name!r} cannot be stored "
            f"encrypted.\n"
            f"  Install one:   pip install keyring keyrings.cryptfile\n"
            f"  Or inject it in the environment instead (containers, CI, systemd).")
    kr.set_password(SERVICE, f"tp_{name}", value)
    _keychain.cache_clear()


def delete_(name: str) -> bool:
    """Remove one secret from the keyring. False if it was not there."""
    kr = _backend()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, f"tp_{name}")
        _keychain.cache_clear()
        return True
    except Exception:
        return False


def export_env(path: str | Path = ".env.runtime", keys=None) -> Path:
    """Materialise the resolved secrets to a 0600 file for a container.

    Docker cannot read the macOS Keychain, the Windows Credential Locker or
    a D-Bus Secret Service - a container has no OS store, so secrets have to
    be injected. This is the bridge: the HOST reads its keyring, writes a
    short-lived file, ``docker compose`` consumes it via ``env_file``, and
    scripts/tp_agent.py deletes it as soon as the containers are up. The
    values live on in the container's environment, not on disk.

    Mode 0600 and gitignored is proportionate for a single-user laptop. On a
    VPS or any shared host, use a real secret manager (1Password Connect,
    Doppler, Vault, AWS Secrets Manager) and inject at container start
    instead - see §44."""
    import stat

    p = Path(path)
    lines = []
    for k in (keys or (SECRET_KEYS + OPTIONAL_KEYS)):
        v = get(k, required=False)
        if v:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n")
    try:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 - owner only
    except Exception as e:  # Windows has no POSIX mode bits
        logger.debug(f"storage.secrets: chmod 600 not applied to {p}: {e}")
    logger.info(f"storage.secrets: wrote {len(lines)} secrets to {p} (mode 600, gitignored)")
    return p


def get(name: str, required: bool = True, default: str | None = None) -> str:
    """Resolve one secret. See module docstring for the order.

    ``required=True`` (the default) raises RuntimeError when nothing is found.
    Pass ``default`` to supply a non-secret fallback (e.g. a hostname).
    """
    val = os.environ.get(name, "")
    if not val and not _strict():
        load_dotenv()
        val = os.environ.get(name, "")
    if not val:
        val = _keychain(name)
    if not val and default is not None:
        return default
    if not val and required:
        sources = "the environment or the OS keyring" if _strict() else \
            f"the environment, {ENV_PATH.name}, or the OS keyring"
        raise RuntimeError(
            f"Secret {name!r} not found in {sources}.\n"
            f"  Store it encrypted:  ./scripts/tp secrets set {name}\n"
            f"  Or inject it:        export {name}=...   (containers, CI, systemd)\n"
            f"  Keyring backend here: {keyring_backend_name()}")
    return val


def present(name: str) -> bool:
    """True if the secret resolves to a non-empty value. Never raises, never
    returns the value - safe to use in log lines and health endpoints."""
    try:
        return bool(get(name, required=False))
    except Exception:
        return False


def missing(keys=SECRET_KEYS) -> list[str]:
    """Which of ``keys`` do not resolve. Used by ``tp doctor``."""
    return [k for k in keys if not present(k)]


def redact(value: str, keep: int = 4) -> str:
    """For logs. Never print a secret; print enough to identify which one."""
    if not value:
        return "<unset>"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


# Load .env at import so `os.getenv` calls elsewhere in the codebase keep
# working unchanged during the migration off the scattered loaders.
load_dotenv()
