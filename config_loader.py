"""Loads config.yaml fresh every call - this is what makes config hot-reloadable
without restarting the process (build note #4).

Phase 0 step 0.2 (§1, §34.3): config.yaml is versioned and therefore may not
contain secrets. Any ``${VAR}`` reference in a string value is expanded from the
environment (which storage.secrets has already populated from .env / Keychain)
at load time.

The expansion deliberately HARD FAILS on a missing variable rather than
substituting an empty string. An empty ``ui.auth_token`` would compare equal to
a blank Authorization header and silently unauthenticate every write endpoint
on the web UI - a crash at startup is the safer failure.

``${VAR:-fallback}`` is supported for non-secret values that have a sensible
default (ports, hostnames). Use it sparingly: a fallback on a secret defeats the
whole point of the hard failure.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import yaml

from storage import secrets

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# ${VAR} or ${VAR:-default}. Uppercase/digits/underscore only, so ordinary
# prose or a shell snippet inside a config comment cannot accidentally match.
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand_str(s: str, required: bool = True) -> str:
    def sub(m):
        name, fallback = m.group(1), m.group(2)
        val = secrets.get(name, required=False)
        if val:
            return val
        if fallback is not None:
            return fallback
        if not required:
            return ""
        raise RuntimeError(
            f"config.yaml references ${{{name}}} but it is not set.\n"
            f"  Add {name}= to .env (gitignored), or run: ./scripts/tp secrets set {name}")
    return _ENV_REF.sub(sub, s)


def expand_env_refs(value, required: bool = False):
    """Expand ``${VAR}`` / ``${VAR:-default}`` in ONE already-read config value.

    Why this is public (2026-07-26): ``load_config_dict()`` is not the only way
    config.yaml reaches running code. server.py deliberately reads the YAML raw
    (``_load_config()``) because it also WRITES the file back, and an expanded
    tree round-tripped through ``yaml.dump`` would bake the secret into a
    versioned file. The consequence was that every value server.py handed to
    engine code arrived as the literal placeholder: ``account.
    robinhood_account_number`` reached ``live_trader._account_number()`` as the
    string ``'${RH_ACCOUNT_NUMBER}'``, ``load_account_profile()`` was called
    with that as an account number and raised, and the Real Portfolio tab
    reported "Robinhood not connected" even though the login had succeeded.
    (Same class of bug §4 already fixed for ``ui.auth_token``, which is why
    that one now bypasses config.yaml entirely.)

    Consumers of individual values call this so they are correct no matter
    which loader their caller used - expanding an already-expanded string is a
    no-op, since a real value contains no ``${...}``.

    ``required`` defaults to False here, unlike ``load_config_dict()``'s hard
    failure. That default is deliberate and scoped: this is for values whose
    absence is legitimate (an unset ``RH_ACCOUNT_NUMBER`` means "use the
    primary account"), and it is reached lazily at call time rather than at
    startup, so raising would surface as a mid-cycle crash instead of a clean
    boot failure. Pass ``required=True`` for a value that must exist.
    """
    if not isinstance(value, str):
        return value
    return _expand_str(value, required=required)


def _expand(obj):
    """Recursively expand ${VAR} references in every string in the tree.

    Hard-fails on a missing variable - see the module docstring. The
    single-value ``expand_env_refs()`` above is the lenient counterpart.
    """
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    if isinstance(obj, str):
        return _expand_str(obj)
    return obj


def _to_namespace(obj):
    """Recursively convert nested dicts to SimpleNamespace for dot-access (cfg.risk.max_daily_loss_usd)
    while keeping dict semantics available via vars()/.__dict__ if needed."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_namespace(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_namespace(v) for v in obj]
    return obj


def load_config_dict() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return _expand(yaml.safe_load(f))


def load_config_dict_raw() -> dict:
    """The file as written, with ${VAR} references left INTACT.

    Only for tooling that must inspect or rewrite config.yaml itself - the
    pre-commit literal-secret check, and the config fingerprint, which must be
    stable across machines with different secret values. Never use this to run
    the platform: nothing downstream expects a literal '${UI_AUTH_TOKEN}'.
    """
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def load_config() -> SimpleNamespace:
    return _to_namespace(load_config_dict())
