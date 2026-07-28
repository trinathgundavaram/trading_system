#!/usr/bin/env python3
"""
Enhanced version management for trading_platform.
Fixes service startup issues and auto-removes old versions on promote.

Usage:
    python3 scripts/tp_enhanced.py promote <tag>    # Auto-clean old versions
    python3 scripts/tp_enhanced.py cleanup           # Remove all non-primary versions
    python3 scripts/tp_enhanced.py fix-services      # Fix launchctl bootstrap issues
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Import from the main tp script (no .py extension)
tp_path = Path(__file__).parent / "tp"
spec = importlib.util.spec_from_file_location("tp", tp_path)
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)

IS_MAC = sys.platform == "darwin"


def fix_launchctl_bootstrap():
    """Fix launchctl bootstrap issues by using modern approach."""
    if not IS_MAC:
        print("launchctl fixes only apply to macOS")
        return 0

    root = tp.tp_root()
    primary = tp.read_primary()

    if not primary:
        print("No primary version set")
        return 1

    print(f"Fixing launchctl bootstrap for {primary}...")

    services_dir = root / "versions" / primary
    if not services_dir.exists():
        print(f"Services directory not found: {services_dir}")
        return 1

    # Clear stuck launchctl entries
    for service in ("scheduler", "ui", "maverick"):
        label = f"com.tradingplatform.{primary}.{service}"

        # Try to unload (old way)
        subprocess.run(["launchctl", "unload",
                       f"~/Library/LaunchAgents/{label}.plist"],
                      capture_output=True)

        # Try to bootout (new way)
        subprocess.run(["launchctl", "bootout", f"gui/501/{label}"],
                      capture_output=True)

        # Remove plist
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        if plist.exists():
            plist.unlink()
            print(f"  Cleaned: {label}")

    # Reinstall services
    print(f"Reinstalling services for {primary}...")
    result = subprocess.run([sys.executable, str(services_dir / "scripts" / "services.py"),
                            "install"],
                           capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Service install failed: {result.stderr}")
        return 1

    # Bootstrap services (with retry)
    for service in ("scheduler", "ui", "maverick"):
        label = f"com.tradingplatform.{primary}.{service}"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

        if not plist_path.exists():
            print(f"  WARNING: {label}.plist not found, skipping")
            continue

        # Try bootstrap with sudo if needed
        for attempt in range(2):
            cmd = ["launchctl", "bootstrap", "gui/501", str(plist_path)]
            if attempt == 1:
                cmd = ["sudo"] + cmd

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                print(f"  Bootstrapped: {label}")
                break
            elif attempt == 0:
                print(f"  Bootstrap failed, trying with sudo...")
        else:
            print(f"  ERROR: Could not bootstrap {label}")
            print(f"  Try manually: launchctl bootstrap gui/501 {plist_path}")

    print("\nServices should now be running. Check with:")
    print("  ./service.sh status")
    return 0


def cleanup_old_versions():
    """Remove all non-primary versions."""
    root = tp.tp_root()
    primary = tp.read_primary()

    if not primary:
        print("No primary version set - nothing to clean")
        return 0

    registry = tp.registry_load()
    removed = []

    for tag in list(registry.keys()):
        if tag == primary:
            print(f"  {tag} - PRIMARY (keeping)")
            continue

        print(f"  {tag} - removing...")

        # Backup first
        tp.cmd_backup(argparse.Namespace(tag=tag, label=None, db=None))

        # Remove worktree
        worktree = root / "versions" / tag
        subprocess.run(["git", "-C", str(tp.REPO), "worktree", "remove", "--force", str(worktree)],
                      capture_output=True)

        # Remove venv
        venv_dir = root / "venvs" / tag
        shutil.rmtree(venv_dir, ignore_errors=True)

        # Remove data
        data_dir = root / "data" / tag
        shutil.rmtree(data_dir, ignore_errors=True)

        # Drop database
        db = registry[tag].get("db")
        if db and shutil.which("dropdb"):
            subprocess.run(["dropdb", db], capture_output=True)

        # Remove from registry
        registry.pop(tag, None)
        removed.append(tag)

    # Save updated registry
    if removed:
        tp.registry_save(registry)
        print(f"\nRemoved {len(removed)} old version(s): {', '.join(removed)}")
        print(f"Remaining versions:")
        for tag in registry.keys():
            size = tp.dir_size_h(root / "versions" / tag) if (root / "versions" / tag).exists() else "-"
            primary_mark = " ← PRIMARY" if tag == primary else ""
            print(f"  {tag:<12} {size}{primary_mark}")
    else:
        print("No old versions to clean")

    return 0


def promote_and_cleanup(tag):
    """Promote version and automatically remove old ones."""
    print(f"Promoting {tag}...")

    # Call original promote
    result = tp.cmd_promote(argparse.Namespace(tag=tag))
    if result != 0:
        return result

    print("\nCleaning up old versions...")
    return cleanup_old_versions()


def main():
    parser = argparse.ArgumentParser(
        description="Enhanced version management for trading_platform",
        epilog="Examples:\n"
               "  python3 scripts/tp_enhanced.py promote v3.3.1\n"
               "  python3 scripts/tp_enhanced.py cleanup\n"
               "  python3 scripts/tp_enhanced.py fix-services"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Promote command
    promote_parser = subparsers.add_parser("promote", help="Promote version and auto-cleanup")
    promote_parser.add_argument("tag", help="Version tag to promote")

    # Cleanup command
    cleanup_parser = subparsers.add_parser("cleanup", help="Remove all non-primary versions")

    # Fix services command
    fix_parser = subparsers.add_parser("fix-services", help="Fix launchctl bootstrap issues")

    args = parser.parse_args()

    if args.command == "promote":
        return promote_and_cleanup(args.tag)
    elif args.command == "cleanup":
        return cleanup_old_versions()
    elif args.command == "fix-services":
        return fix_launchctl_bootstrap()
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
