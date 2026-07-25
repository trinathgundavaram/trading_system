#!/usr/bin/env bash
# =============================================================================
#  §42.3 (Phase 3) - build an image straight from a git tag.
#
#  The point is that the image is built from the TAG, not from your working
#  tree. A build from the tree would carry whatever is uncommitted, which is
#  exactly the ambiguity storage/version.py's '-dirty' suffix exists to warn
#  about - and an image is meant to be the thing you can rebuild in 2029.
#
#    ./scripts/build_version.sh v1.3.1
#    ./scripts/build_version.sh v1.4.0
#
#  Then the Phase 3 exit criterion:
#    TP_VERSION=v1.3.1 TP_UI_PORT=8080 \
#      docker compose --env-file .env.runtime -p tp-v1-3-1 up -d
#    TP_VERSION=v1.4.0 TP_UI_PORT=8081 \
#      docker compose --env-file .env.runtime -p tp-v1-4-0 up -d
#  run the same backtest window in both, and confirm the shared code paths
#  produce identical numbers. If they diverge, the pinning is still loose.
# =============================================================================
set -euo pipefail

TAG="${1:?usage: build_version.sh v1.4.0}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="${TMPDIR:-/tmp}/tp-build-${TAG}"

cd "$REPO_ROOT"

if ! git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "error: ${TAG} is not a tag in this repository" >&2
  echo "       existing tags:" >&2
  git tag --list 'v*' | tail -10 | sed 's/^/         /' >&2
  exit 1
fi

# A leftover worktree from an interrupted build would make `worktree add` fail
# with a message about an existing path rather than about what went wrong.
if [ -e "$WORKTREE" ]; then
  git worktree remove --force "$WORKTREE" 2>/dev/null || rm -rf "$WORKTREE"
fi

cleanup() {
  git worktree remove --force "$WORKTREE" 2>/dev/null || rm -rf "$WORKTREE"
}
trap cleanup EXIT

echo "==> checking out ${TAG} into ${WORKTREE}"
git worktree add --detach "$WORKTREE" "$TAG" >/dev/null

echo "==> docker build -t trading-platform:${TAG}"
docker build -t "trading-platform:${TAG}" "$WORKTREE"

echo
echo "built trading-platform:${TAG}"
echo "run it:  TP_VERSION=${TAG} TP_UI_PORT=8080 \\"
echo "           docker compose --env-file .env.runtime -p tp-${TAG//./-} up -d"
