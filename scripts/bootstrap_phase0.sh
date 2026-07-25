#!/usr/bin/env bash
# =============================================================================
#  scripts/bootstrap_phase0.sh
#
#  The parts of Phase 0 that must run on YOUR machine rather than in a
#  sandbox: installing hooks, mirroring secrets into the Keychain, and
#  confirming the dependency pins describe the environment you actually run.
#
#  Idempotent - safe to re-run. Nothing here is destructive.
# =============================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo '── 1. git hooks ────────────────────────────────────────────────────────'
mkdir -p .git/hooks
ln -sf ../../scripts/hooks/pre-push .git/hooks/pre-push
chmod +x scripts/hooks/pre-push
echo '  pre-push hook linked (refuses a tag with no release note)'

if command -v pre-commit >/dev/null; then
  pre-commit install
  echo '  pre-commit installed'
else
  echo '  pre-commit NOT installed - run: pip install pre-commit && pre-commit install'
fi
command -v gitleaks >/dev/null \
  && echo '  gitleaks present' \
  || echo '  gitleaks NOT installed - run: brew install gitleaks'

echo
echo '── 2. dependency pins ──────────────────────────────────────────────────'
echo '  The pins in requirements.txt were set from the versions this project'
echo '  was documented as running. Confirm them against your real environment:'
python3 scripts/pin_requirements.py || true
echo '    (apply with: python3 scripts/pin_requirements.py --write --lock)'

echo
echo '── 3. secrets ──────────────────────────────────────────────────────────'
./scripts/tp secrets check || true
echo
echo '  .env is the source of truth and is gitignored. To ALSO mirror it into'
echo '  the macOS Keychain (encrypted at rest, survives a repo leak):'
echo '      ./scripts/tp secrets import .env'
echo '  Add --shred once you are satisfied, to overwrite and unlink the file.'

echo
echo '── 4. audit ────────────────────────────────────────────────────────────'
./scripts/tp doctor || true

echo
echo 'Next:'
echo '  1. Rotate the credentials in .env (§3) - the Robinhood password, the'
echo '     TOTP enrolment, the UI token, and every API key. The NVIDIA key was'
echo '     exposed in a chat window and must be revoked, not reused.'
echo '  2. git push -u origin main --follow-tags'
echo '  3. ./scripts/tp install v1.0.0 && ./scripts/tp run v1.0.0 --backtest'
