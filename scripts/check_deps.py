#!/usr/bin/env python3
"""Fail if the installed packages drift from requirements.txt (§13, Fix 3).

A version number means nothing if its dependencies float. This is what keeps
`tp install v1.0.0` reproducing v1.0.0's numbers rather than v1.0.0's code
running against September's pandas.

Run by scripts/release.sh and by the pre-commit hook on requirements.txt.

    python3 scripts/check_deps.py            # exit 1 on drift
    python3 scripts/check_deps.py --warn     # exit 0, report only
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
REQS = REPO / "requirements.txt"

# Packages whose drift changes indicator values, and therefore every score.
# Reported separately because the response is different: a routine drift is a
# `pip install -r`, a score-affecting drift means results computed since the
# drift are not comparable with results computed before it.
SCORE_AFFECTING = {"pandas", "pandas-ta", "numpy", "scipy", "yfinance"}

_NORM = re.compile(r"[-_.]+")


def norm(name: str) -> str:
    """PEP 503 normalisation - pandas_ta, pandas-ta and Pandas.TA are one name."""
    return _NORM.sub("-", name.strip().lower())


def wanted() -> dict:
    out = {}
    for line in REQS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        version = rest.split("#")[0].strip()
        if name.strip() and version:
            out[norm(name)] = version
    return out


def installed() -> dict:
    """What is importable by THIS interpreter, from its installed metadata.

    NOT `pip freeze` (2026-07-25). freeze renders a package installed from a
    local wheel or built by conda as

        pandas @ file:///croot/pandas_1234567890/work

    rather than `pandas==2.3.3`, because that is the form that would reinstall
    it. The old parser here kept only lines containing '==', so on the release
    machine - an Anaconda base env - sixteen packages that were present and
    working were reported as NOT INSTALLED, two of them flagged
    SCORE-AFFECTING. pytest was in that list while running the suite that had
    just passed.

    That is worse than a missing check. §13 exists to make a real pandas drift
    impossible to miss, and a guard that cries wolf about sixteen packages is
    one nobody reads by the third release. importlib.metadata reads the same
    .dist-info the import system reads, so it reports the version actually
    loaded regardless of how it got there."""
    from importlib import metadata

    out = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue  # a broken .dist-info with no METADATA; not ours to fix
        version = (dist.version or "").strip()
        if version:
            out[norm(name)] = version
    return out


def main() -> int:
    warn_only = "--warn" in sys.argv
    want, have = wanted(), installed()

    missing = sorted(k for k in want if k not in have)
    drift = sorted((k, want[k], have[k]) for k in want
                   if k in have and have[k] != want[k])

    if not missing and not drift:
        print(f"dependencies: {len(want)} pinned, all match")
        return 0

    critical = False
    if missing:
        print("NOT INSTALLED:")
        for k in missing:
            flag = "  <- SCORE-AFFECTING" if k in SCORE_AFFECTING else ""
            critical = critical or k in SCORE_AFFECTING
            print(f"  {k}=={want[k]}{flag}")
    if drift:
        print("DEPENDENCY DRIFT - scores may not be comparable:")
        for k, w, h in drift:
            flag = "  <- SCORE-AFFECTING" if k in SCORE_AFFECTING else ""
            critical = critical or k in SCORE_AFFECTING
            print(f"  {k}: want {w}, have {h}{flag}")

    print()
    print("  Fix:  pip install -r requirements.txt")
    print("  Or, if the installed set is the one you intend to keep:")
    print("        python3 scripts/pin_requirements.py --write")
    if critical:
        print()
        print("  At least one score-affecting package differs. Results computed")
        print("  under this environment are NOT comparable with results computed")
        print("  under the pinned one - re-validation is required before arming")
        print("  live execution (§23).")

    return 0 if warn_only else 1


if __name__ == "__main__":
    sys.exit(main())
