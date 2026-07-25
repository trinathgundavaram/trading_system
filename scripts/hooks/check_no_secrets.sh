#!/usr/bin/env bash
# Block a literal secret from being committed (§12, §34.4).
#
# Complements gitleaks rather than duplicating it. gitleaks matches known
# provider key FORMATS; this matches the SHAPE `NAME = "long-opaque-string"` in
# any staged file, which is how a key gets pasted into a .py or .md "just to
# test something". That is exactly how the NVIDIA key in this project's .env
# was exposed.
#
# A real script rather than an inline `entry:` in .pre-commit-config.yaml: as a
# folded YAML scalar the pipeline collapsed onto one line, and if anyone later
# reformatted it, `xargs -r grep -lEi` and its pattern landed on separate lines
# and grep read the pattern as a filename. That fails with exit 0 - a security
# hook that silently passes, which is worse than no hook at all.
set -uo pipefail

PATTERN='(api[_-]?key|password|passwd|totp|secret|auth[_-]?token|bearer)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_/+.-]{16,}'

# Excluded, deliberately:
#   *.template        documents variable NAMES and carries no values
#   tests/            fixtures use obvious fakes; a real key there is caught by
#                     gitleaks anyway
#   docs/releases/    release notes quote config keys when explaining a change
files=$(git diff --cached --name-only --diff-filter=ACM \
        | grep -Ev '\.template$|^tests/|^docs/releases/|^scripts/hooks/' || true)

[ -z "$files" ] && exit 0

# --no-messages so a deleted-but-staged path does not produce a spurious error.
hits=$(printf '%s\n' "$files" | xargs -r grep --no-messages -lEi "$PATTERN" || true)

if [ -n "$hits" ]; then
    echo 'BLOCKED - possible literal secret in:'
    printf '  %s\n' $hits
    echo
    echo 'Move the value out of the tree: put it in .env (gitignored) or the'
    echo 'Keychain, and reference it via storage/secrets.py or ${VAR} in'
    echo 'config.yaml. If this is a false positive, --no-verify is NOT the'
    echo 'answer - narrow the pattern in scripts/hooks/check_no_secrets.sh so'
    echo 'the next person is still protected.'
    exit 1
fi
exit 0
