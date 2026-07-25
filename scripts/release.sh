#!/usr/bin/env bash
# scripts/release.sh - cut a release (§37).
#
# One script, so the sequence cannot be got wrong. Usage:
#     ./scripts/release.sh patch|minor|major
#
# The ordering is the point. Tests before version computation, version before
# the release note, the note before the tag. A tag that exists without a note
# is a tag nobody can interpret in six months.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

LEVEL="${1:?usage: release.sh patch|minor|major}"

# ── 1. Refuse to release from a dirty or unexpected state ───────────────────
[ -z "$(git status --porcelain)" ] || { echo 'FAIL: working tree dirty'; exit 1; }

BRANCH=$(git rev-parse --abbrev-ref HEAD)
case "$BRANCH" in
  main|release/*) ;;
  *) echo "FAIL: release from main or release/*, not $BRANCH"; exit 1 ;;
esac

# ── 2. Tests must pass. Not 'mostly'. ───────────────────────────────────────
# §12 (Phase 2 step 2.1) restores the suite; until then 33 of 58 tests cannot
# run, so this is soft-failed with a loud warning rather than silently skipped.
if ! python3 -m pytest tests/ -q; then
  echo
  echo 'WARNING: the test suite is red. This is finding E-2 and is scheduled'
  echo '         for Phase 2 step 2.1 (§12). Until then a release cannot be'
  echo '         gated on it - but you are releasing without a safety net.'
  read -rp 'Continue anyway? [y/N] ' ok; [ "$ok" = y ] || exit 1
fi

python3 scripts/check_deps.py            # §13 - no dependency drift
python3 scripts/check_config_secrets.py  # §34 - no literal secret in config
command -v gitleaks >/dev/null && gitleaks detect --no-banner --redact \
  || echo 'note: gitleaks not installed - `brew install gitleaks` (§34.4)'

# ── 3. Compute the next version ─────────────────────────────────────────────
PREV=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || echo v0.0.0)
NEXT=$(python3 scripts/version.py --bump "$LEVEL" --from "$PREV")
SHORT=$(python3 scripts/version.py --shorthand "$NEXT")

# ── 4. Sanity-check the level against the diff ──────────────────────────────
SUGGESTED=$(python3 scripts/classify_change.py "$PREV")
if [ "$SUGGESTED" = 'MAJOR' ] && [ "$LEVEL" != 'major' ]; then
  echo "WARNING: the diff touches the decision function, which calls for a"
  echo "         major bump. You asked for '$LEVEL'."
  echo "         Pooling trade history across a decision-function change is a"
  echo "         measurement error, not a versioning nicety."
  git diff --name-only "$PREV" HEAD | sed 's/^/           /'
  read -rp 'Continue anyway? [y/N] ' ok; [ "$ok" = y ] || exit 1
fi

# ── 5. The release note must exist BEFORE the tag ───────────────────────────
NOTE="docs/releases/${NEXT}.md"
if [ ! -f "$NOTE" ]; then
  cp docs/releases/TEMPLATE.md "$NOTE"
  python3 scripts/version.py --fill-note "$NOTE" --tag "$NEXT" --prev "$PREV"
  echo "Created $NOTE - fill it in, then re-run."
  echo "  Suggested bump from the diff: $SUGGESTED"
  exit 1
fi

grep -q "^## \[$SHORT\]" CHANGELOG.md || {
  echo "FAIL: CHANGELOG.md has no '## [$SHORT]' entry for $NEXT"; exit 1; }

# ── 6. Stamp the version into the code so a running process knows it ────────
echo "$NEXT" > VERSION
git add VERSION CHANGELOG.md "$NOTE"
git commit -m "release: $NEXT ($SHORT)"
git tag -a "$NEXT" -F "$NOTE"

# ── 7. Cut a release branch on a major/minor so patches have a home ─────────
case "$LEVEL" in
  major|minor)
    LINE=$(python3 scripts/version.py --line "$NEXT")   # v1.1.0 -> 1.1
    git branch "release/$LINE" "$NEXT" 2>/dev/null \
      && echo "cut branch release/$LINE" || true ;;
esac

git push --follow-tags
echo "Released $NEXT ($SHORT). Install it with: ./scripts/tp install $NEXT"
