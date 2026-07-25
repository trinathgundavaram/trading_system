#!/usr/bin/env python3
"""Rewrite the == pins in requirements.txt from the current environment (§13).

The pins must describe the environment you are ACTUALLY running, not a set of
plausible-looking numbers. This reads `pip freeze`, replaces the version on
every already-pinned line, and leaves every comment, blank line and grouping
exactly where it was - the comments are grouped by blast radius and are the
most useful thing in the file.

    python3 scripts/pin_requirements.py            # show what would change
    python3 scripts/pin_requirements.py --write    # rewrite in place
    python3 scripts/pin_requirements.py --lock     # also write requirements.lock.txt

A package named in requirements.txt but not installed is reported and left
alone - silently unpinning it would be the opposite of the point.
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
REQS = REPO / "requirements.txt"
LOCK = REPO / "requirements.lock.txt"

_NORM = re.compile(r"[-_.]+")
PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)==(?P<ver>[^\s#]+)(?P<tail>.*)$")


def norm(name: str) -> str:
    return _NORM.sub("-", name.strip().lower())


def freeze() -> dict:
    res = subprocess.run([sys.executable, "-m", "pip", "freeze"],
                         capture_output=True, text=True)
    out = {}
    for line in res.stdout.splitlines():
        if "==" in line and not line.startswith("#"):
            k, _, v = line.partition("==")
            out[norm(k)] = v.strip()
    return out, res.stdout


def main() -> int:
    write = "--write" in sys.argv
    have, raw = freeze()

    changed, absent, lines = [], [], []
    for line in REQS.read_text().splitlines():
        m = PIN.match(line)
        if not m:
            lines.append(line)
            continue
        key = norm(m.group("name"))
        if key not in have:
            absent.append(m.group("name"))
            lines.append(line)
            continue
        new_ver = have[key]
        if new_ver != m.group("ver"):
            changed.append((m.group("name"), m.group("ver"), new_ver))
        lines.append(f"{m.group('name')}=={new_ver}{m.group('tail')}")

    if absent:
        print("named in requirements.txt but NOT installed (left unchanged):")
        for n in absent:
            print(f"  {n}")
        print()

    if not changed:
        print("all pins already match the installed environment")
    else:
        print(f"{'rewriting' if write else 'would rewrite'} {len(changed)} pin(s):")
        for name, old, new in changed:
            print(f"  {name}: {old} -> {new}")

    if write:
        REQS.write_text("\n".join(lines) + "\n")
        print(f"wrote {REQS}")
        if "--lock" in sys.argv:
            LOCK.write_text(raw)
            print(f"wrote {LOCK}")
    elif changed:
        print("\n(dry run - pass --write to apply)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
