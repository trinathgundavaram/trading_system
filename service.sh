#!/bin/bash
# ============================================================================
#  Background services - now a thin shim over scripts/services.py (§45).
#
#  WHAT CHANGED IN PHASE 3. Everything this file used to do was launchd:
#  `launchctl bootstrap`, ~/Library/LaunchAgents, plist XML. That works on
#  exactly one operating system. On any other OS the scheduler ran in a
#  foreground terminal and died with the window - the very failure this file
#  was written to fix on macOS, reintroduced everywhere else.
#
#  scripts/services.py implements the same three services (scheduler, ui,
#  maverick) against launchd on macOS, systemd user units on Linux, and Task
#  Scheduler on Windows. Every macOS-specific detail this file had earned the
#  hard way is preserved there and commented with the incident that caused it:
#  the 4096/8192 open-file limits from the 2026-07-17 fd exhaustion, the full
#  installing-shell PATH for npx/uvx MCP subprocesses, caffeinate -i on the
#  scheduler, and the LABEL_SUFFIX versioning from §38.5.
#
#  This file survives ONLY as an interface. `scripts/tp promote` calls
#  `LABEL_SUFFIX=".$tag" ./service.sh install`, muscle memory calls
#  `./service.sh status`, and both should keep working. The original is kept
#  verbatim as service.launchd.sh.bak if you ever need to compare behaviour.
#
#  Usage (unchanged):
#    ./service.sh install     # write service definitions + start everything
#    ./service.sh status      # what's running
#    ./service.sh restart     # bounce all services (e.g. after a code change)
#    ./service.sh stop        # stop
#    ./service.sh start       # start
#    ./service.sh uninstall   # stop + remove the service definitions
#    ./service.sh logs        # follow the scheduler log
# ============================================================================
set -e
cd "$(dirname "$0")"

PYTHON3="$(command -v python3 || command -v python)"
if [ -z "$PYTHON3" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

# LABEL_SUFFIX is the §38.5 per-version suffix; scripts/services.py reads it
# as TP_LABEL_SUFFIX so the Python side has one name for it across all three
# platforms. Exported rather than passed as a flag so an existing
# `LABEL_SUFFIX=".v1.4.0" ./service.sh install` keeps working untouched.
export TP_LABEL_SUFFIX="${LABEL_SUFFIX:-${TP_LABEL_SUFFIX:-}}"

case "${1:-}" in
  install|uninstall|start|stop|restart|status|logs)
    exec "$PYTHON3" scripts/services.py "$@"
    ;;
  *)
    grep '^#' "$0" | sed -n '2,32p' | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
