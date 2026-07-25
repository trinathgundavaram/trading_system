"""What version produced this row (§37).

The signals table already stamps rule_engine_version, threshold_version and
regime_version. Those describe the rules; none of them describes the *build*.
Without a release version on the row you cannot answer "which version produced
this trade" - and that is the question every post-mortem starts with.

app_version() deliberately returns `git describe --tags --always --dirty`
rather than the plain VERSION file. A row stamped 'v1.1.0-3-gabc123-dirty' is
telling you something important: it was NOT produced by v1.1.0. Three commits
of uncommitted drift sit between the tag and the process that wrote the row,
and any comparison against the v1.1.0 release note is invalid.
"""
from __future__ import annotations

import functools
import os
import pathlib
import re
import subprocess

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def app_version() -> str:
    """Released version + git state. Cached - this is called per row."""
    # scripts/tp exports TP_VERSION for an installed worktree, where `git
    # describe` would describe the detached checkout rather than the release.
    env = os.getenv("TP_VERSION")
    if env:
        return env

    version_file = REPO_DIR / "VERSION"
    base = version_file.read_text().strip() if version_file.exists() else "unversioned"
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=3).stdout.strip()
        return described or base
    except Exception:
        return base


# A tag and nothing else. 'v1.1.0' passes; 'v1.1.0-3-gabc123', 'v1.1.0-dirty',
# a bare commit sha and 'unversioned' all fail.
_EXACT_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


@functools.lru_cache(maxsize=1)
def is_release_build() -> bool:
    """True only when the tree is exactly a tagged release.

    False for '-dirty', for any commit after the tag, and for an untagged
    checkout where `git describe` falls back to a bare commit sha. §32 uses
    this to refuse to arm live execution from a working tree - a build nobody
    can reconstruct must not be allowed to place orders.
    """
    return bool(_EXACT_TAG.match(app_version()))


@functools.lru_cache(maxsize=1)
def ta_backend() -> str:
    """Which technical-analysis implementation computed the indicators (§13).

    pandas_ta and engine/ta_fallback.py are not bit-identical, so a score from
    July and a score from September are not comparable unless this matches.
    Imported lazily: storage/ must not pull the analysis stack into processes
    that only touch the database.
    """
    try:
        from engine.ticker_analyzer import TA_BACKEND
        return TA_BACKEND
    except Exception:
        return "unknown"
