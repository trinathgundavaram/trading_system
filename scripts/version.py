#!/usr/bin/env python3
"""The only place in this project that knows how versions work (§35).

Canonical storage form is ``vMAJOR.MINOR.PATCH``. It sorts correctly forever
and every tool understands it. The project shorthand (1.0 / 1.01 / 1.1 / 2.0)
is a DISPLAY form only.

Why the shorthand cannot be the storage form: it works as decimals right up to
the tenth patch and then breaks. 1.0 < 1.01 < 1.02 ... but after 1.09 the next
patch is 1.10, which is indistinguishable from 1.1, the label for a major
change. ``git tag --sort=v:refname`` will also order v1.1 before v1.01 under
some comparisons, because it segments on the dot rather than reading the
decimal. Storing three parts removes the ambiguity; shorthand() puts it back
for human consumption and degrades gracefully past nine patches.

The bump rule that matters here is NOT conventional semver's "does this break a
caller's code" - this system has no external callers. It is:

    Does this change alter the decision function - the mapping from market
    data to a buy, a size, or an exit?

If yes, every trade recorded before the release was produced by a different
strategy, and pooling the two sets of results is a measurement error. That
deserves the major bump. See scripts/classify_change.py, which reads the diff
and applies the rule mechanically.

Usage:
    version.py --bump patch --from v1.0.0     -> v1.0.1
    version.py --shorthand v1.0.1             -> 1.01
    version.py --line v1.1.0                  -> 1.1
    version.py --current                      -> contents of VERSION
    version.py --fill-note docs/releases/v1.0.1.md --tag v1.0.1 --prev v1.0.0
"""
import argparse
import datetime as _dt
import pathlib
import re
import sys

TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHORT = re.compile(r"^v?(\d+)\.(\d+)$")          # accepts 1.01 / 1.1 input

REPO = pathlib.Path(__file__).resolve().parent.parent


def parse(s: str):
    s = s.strip()
    m = TAG.match(s)
    if m:
        return tuple(int(g) for g in m.groups())
    m = SHORT.match(s)
    if m:
        major, rest = m.group(1), m.group(2)
        # '01' means patch 1 of minor 0; '1' means minor 1, patch 0. This is
        # exactly the ambiguity §35 describes, resolved here by treating a
        # LEADING ZERO as the patch marker - which is what the shorthand was
        # always intending.
        if len(rest) > 1 and rest.startswith("0"):
            return int(major), 0, int(rest)
        return int(major), int(rest), 0
    sys.exit(f"unparseable version: {s!r}")


def fmt(v) -> str:
    return "v%d.%d.%d" % v


def shorthand(v) -> str:
    major, minor, patch = v
    if patch == 0:
        return f"{major}.{minor}"               # 1.0, 1.1, 2.0
    if minor == 0 and patch < 10:
        return f"{major}.0{patch}"              # 1.01 .. 1.09
    return f"{major}.{minor}.{patch}"           # past 9 patches, be explicit


def line(v) -> str:
    """The release line a tag belongs to: v1.1.3 -> 1.1 (branch release/1.1)."""
    return f"{v[0]}.{v[1]}"


def bump(v, level: str):
    major, minor, patch = v
    try:
        return {"major": (major + 1, 0, 0),
                "minor": (major, minor + 1, 0),
                "patch": (major, minor, patch + 1)}[level]
    except KeyError:
        sys.exit(f"unknown level {level!r}: expected major|minor|patch")


def fill_note(path: pathlib.Path, tag: str, prev: str) -> None:
    """Pre-populate the front matter of a copied TEMPLATE.md."""
    v = parse(tag)
    text = path.read_text()
    subs = {
        r"^version:.*$":  f"version: {shorthand(v)}",
        r"^tag:.*$":      f"tag: {fmt(v)}",
        r"^date:.*$":     f"date: {_dt.date.today().isoformat()}",
        r"^previous:.*$": f"previous: {prev}",
    }
    for pattern, repl in subs.items():
        text = re.sub(pattern, repl, text, count=1, flags=re.MULTILINE)
    path.write_text(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bump", choices=("major", "minor", "patch"))
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--shorthand", metavar="TAG")
    ap.add_argument("--line", metavar="TAG")
    ap.add_argument("--current", action="store_true")
    ap.add_argument("--fill-note", metavar="PATH")
    ap.add_argument("--tag")
    ap.add_argument("--prev")
    a = ap.parse_args()

    if a.current:
        f = REPO / "VERSION"
        print(f.read_text().strip() if f.exists() else "unversioned")
        return 0
    if a.shorthand:
        print(shorthand(parse(a.shorthand)))
        return 0
    if a.line:
        print(line(parse(a.line)))
        return 0
    if a.fill_note:
        if not (a.tag and a.prev):
            sys.exit("--fill-note requires --tag and --prev")
        fill_note(pathlib.Path(a.fill_note), a.tag, a.prev)
        return 0
    if a.bump:
        base = parse(a.frm or "v0.0.0")
        print(fmt(bump(base, a.bump)))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
