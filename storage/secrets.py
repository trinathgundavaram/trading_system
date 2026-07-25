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
  3. The macOS Keychain, under the service name ``tp_<NAME>``. Populated by
     ``./scripts/tp secrets import``. Encrypted at rest and survives a repo
     leak; on non-macOS this step is skipped silently (§44 replaces it with a
     cross-platform keyring in Phase 3 - the interface below does not change).

A missing REQUIRED secret raises. It does not return ''. An empty auth token
compares equal to a blank header and would silently unauthenticate every write
endpoint on the UI, which is strictly worse than a crash at startup.

Nothing here writes a secret to disk, and nothing passes one as a command-line
argument, where ``ps`` would show it to every local process.
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


@functools.lru_cache(maxsize=64)
def _keychain(name: str) -> str:
    """Read ``tp_<name>`` from the macOS Keychain. '' if absent or not macOS."""
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


def get(name: str, required: bool = True, default: str | None = None) -> str:
    """Resolve one secret. See module docstring for the order.

    ``required=True`` (the default) raises RuntimeError when nothing is found.
    Pass ``default`` to supply a non-secret fallback (e.g. a hostname).
    """
    val = os.environ.get(name, "")
    if not val:
        load_dotenv()
        val = os.environ.get(name, "")
    if not val:
        val = _keychain(name)
    if not val and default is not None:
        return default
    if not val and required:
        raise RuntimeError(
            f"Secret {name!r} not found in the environment, {ENV_PATH.name}, "
            f"or the macOS Keychain.\n"
            f"  Add it to {ENV_PATH} (which is gitignored), or store it with:\n"
            f"    ./scripts/tp secrets set {name}")
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
