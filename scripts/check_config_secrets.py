#!/usr/bin/env python3
"""Refuse to commit a literal secret in config.yaml (Phase 0 step 0.3, §34.4).

config.yaml is versioned - it is genuinely part of the system and should be in
git. What must never be in it is a value. Every secret-bearing key has to be a
``${VAR}`` reference that config_loader expands from the environment at load
time.

This is a belt to gitleaks' braces: gitleaks catches things that LOOK like
credentials (high entropy, known key prefixes). A five-character auth token
like the one this project shipped with, or a nine-digit account number, looks
like nothing at all to an entropy scanner - but is exactly as dangerous.

Exit 1 blocks the commit. Run manually with:  python3 scripts/check_config_secrets.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.yaml"

# Any key whose value must be a ${VAR} reference, never a literal.
SECRET_KEY_PATTERN = re.compile(
    r"^\s*[-\s]*(?P<key>[a-z0-9_]*("
    r"account_number|auth_token|api_key|apikey|secret|password|passwd|"
    r"token|client_id|client_secret|private_key|access_key|credential"
    r")[a-z0-9_]*)\s*:\s*(?P<val>.*?)\s*(?:#.*)?$",
    re.IGNORECASE,
)

# Values that are not secrets even though the key name matches.
ALLOWED_VALUES = re.compile(
    r"^(|~|null|Null|NULL|true|false|True|False|yes|no|\[\]|\{\}|\|\-?|>\-?|\$\{[A-Z0-9_]+(:-[^}]*)?\})$"
)


def main() -> int:
    if not CONFIG.exists():
        print(f"check_config_secrets: {CONFIG} not found", file=sys.stderr)
        return 0

    bad = []
    for lineno, raw in enumerate(CONFIG.read_text().splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        m = SECRET_KEY_PATTERN.match(raw)
        if not m:
            continue
        val = m.group("val").strip().strip('"').strip("'")
        if ALLOWED_VALUES.match(val):
            continue
        bad.append((lineno, m.group("key"), val))

    if bad:
        print("BLOCKED - literal secret(s) in config.yaml. Use ${VAR} instead:")
        for lineno, key, val in bad:
            shown = val[:2] + "*" * max(0, len(val) - 2)
            print(f"  config.yaml:{lineno}  {key}: {shown}")
        print()
        print("  Move the value to .env (gitignored) and reference it, e.g.:")
        print("      auth_token: ${UI_AUTH_TOKEN}")
        print("  config_loader.py expands it at load time and raises if unset.")
        return 1

    print("config.yaml: no literal secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
