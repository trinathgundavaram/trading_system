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
# HARD GATE as of Phase 2. This used to soft-fail with a warning and a y/N
# prompt, because §12 had not yet restored the suite and 33 of 58 tests could
# not run (finding E-2). §12 is done, so the exemption is over.
#
# The prompt is not merely redundant now, it is dangerous: the 2026-07-25
# incident was a release.sh run whose suite executed against the LIVE database,
# and a gate you can answer 'y' to is a gate that gets answered 'y' at 5pm.
python3 -m pytest tests/ -q

python3 scripts/check_deps.py            # §13 - no dependency drift
python3 scripts/check_config_secrets.py  # §34 - no literal secret in config
command -v gitleaks >/dev/null && gitleaks detect --no-banner --redact \
  || echo 'note: gitleaks not installed - `brew install gitleaks` (§34.4)'

# ── 2b. The guards must be IN FORCE, not merely implemented ─────────────────
# Tests prove the code is correct. These prove the correct code is what is
# running here - config flags actually set, migrations actually applied, no
# new write route left unguarded. That gap is where the 2026-07-25 incident
# lived, and it is not a gap a code review closes.
#
# Blocking, both of them. A Phase 1 or Phase 2 regression is exactly the kind
# of thing nobody would choose to release past if they were asked in a
# language stronger than a prompt.
python3 scripts/verify_phase1.py --release   # --release also demands a clean tree
python3 scripts/verify_phase2.py

# Cross-table integrity (§15). Non-blocking BY DESIGN: this reads the state of
# the DATA, not of the code, and a book that needs reconciling is not a reason
# to refuse a release that might contain the fix for it. It is a reason to know
# before you tag.
if ! python3 scripts/reconcile.py --quiet; then
  echo
  echo 'NOTE: reconcile.py reported findings (see above). These describe the'
  echo '      DATABASE, not this release. Releasing is allowed - resolving'
  echo '      them by writing corrective rows at invented prices is not.'
fi

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
# The commit is conditional because a "release prep" commit that already
# stamped VERSION, wrote the CHANGELOG entry and added the note leaves nothing
# to stage - and `git commit` on an empty index exits non-zero, which under
# `set -e` killed this script one line BEFORE `git tag`. That is not a
# hypothetical: it is why v2.0.0 has a release note, a CHANGELOG entry and a
# VERSION bump but no tag, and why the next release computed its version from
# v1.3.1. A release that half-happens and reports failure is the worst case -
# the tag, which is the thing everything else keys off, is the part that got
# skipped.
echo "$NEXT" > VERSION
git add VERSION CHANGELOG.md "$NOTE"
if git diff --cached --quiet; then
  echo "note: VERSION, CHANGELOG and $NOTE were already committed (release prep)."
  echo "      Nothing to commit - tagging $NEXT on HEAD as-is."
else
  git commit -m "release: $NEXT ($SHORT)"
fi
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
