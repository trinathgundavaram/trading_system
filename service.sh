#!/bin/bash
# ============================================================================
# Background services via macOS launchd (2026-07-16, Akhil's ask: "keep it
# auto running in background without open terminal").
#
# WHY: anything started from a terminal (./run.sh, `make dev` for Maverick)
# is a child of that terminal's shell - closing the window, quitting the
# terminal app, or a sleep/wake glitch sends it SIGHUP and it dies. run.sh's
# supervision loops can't help because they die WITH the terminal. That's
# exactly why Maverick "auto stops and doesn't come back".
#
# launchd is macOS's system service manager: services run with NO terminal,
# start automatically at login, and KeepAlive relaunches them within seconds
# whenever they exit for any reason. This script installs three services:
#
#   com.tradingplatform.scheduler   python3 scheduler.py   (wrapped in
#                                   caffeinate -i so the Mac won't idle-sleep
#                                   through scan windows)
#   com.tradingplatform.ui          python3 main.py --ui   (localhost:8080)
#   com.tradingplatform.maverick    $MAVERICK_CMD          (only if set, or
#                                   ~/maverick-mcp exists)
#
# Usage:
#   ./service.sh install     # write plists + start everything
#   ./service.sh status      # what's running, with PIDs
#   ./service.sh restart     # bounce all services (e.g. after a code change)
#   ./service.sh stop        # stop + disable (until next install/start)
#   ./service.sh start       # re-enable after a stop
#   ./service.sh uninstall   # stop + remove the plists entirely
#   ./service.sh logs        # tail all service logs together
#
# After `install` you can close every terminal - the UI stays at
# http://localhost:8080, the scheduler keeps scanning, and Maverick is
# relaunched automatically whenever it dies. Service stdout/stderr goes to
# output/logs/launchd_*.log (the apps' own logs stay in output/logs/ too).
#
# NOTE: run.sh still works for interactive/foreground use - but don't run
# both at once (run.sh kills stray scheduler/UI processes at startup, which
# launchd will then dutifully restart, and the two will fight).
# ============================================================================
set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PYTHON3="$(command -v python3)"
CAFFEINATE="$(command -v caffeinate || true)"
# §38.5: the label prefix was fixed, so two installed versions would fight over
# the same launchd labels and the second install would silently take over the
# first's services. `tp promote` passes LABEL_SUFFIX=".<tag>" so each version's
# services are distinct - and uninstalls the outgoing primary before installing
# the incoming one, because exactly one version should be running in the
# background.
LABEL_PREFIX="com.tradingplatform${LABEL_SUFFIX:-}"
# Runtime data root. Unset, this is $APP_DIR/output exactly as before; `tp
# promote` points it at ~/tp/data/<tag>/output so a promoted version does not
# write into its own git worktree. Mirrors storage/paths.py.
OUT_DIR="${TP_OUTPUT_DIR:-$APP_DIR/output}"
SERVICES="scheduler ui maverick"
mkdir -p "$OUT_DIR/logs" "$AGENTS_DIR"

# Maverick launch command - same detection as run.sh.
if [ -z "$MAVERICK_CMD" ] && [ -d "$HOME/maverick-mcp" ]; then
  MAVERICK_CMD="cd $HOME/maverick-mcp && make dev"
fi

_plist_path() { echo "$AGENTS_DIR/$LABEL_PREFIX.$1.plist"; }

_xml_escape() {
  # plist is XML: a Maverick command like "cd ... && make dev" would break
  # the file without &amp; escaping (found by the install-time validation).
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

_write_plist() {
  # $1 = service name, $2... = ProgramArguments (already shell-ready strings)
  local name="$1"; shift
  local args=""
  for a in "$@"; do args="$args<string>$(_xml_escape "$a")</string>"; done
  cat > "$(_plist_path "$name")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_PREFIX.$name</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$OUT_DIR/logs/launchd_$name.log</string>
  <key>StandardErrorPath</key><string>$OUT_DIR/logs/launchd_$name.log</string>
  <!-- 2026-07-17 (fd-exhaustion fix): macOS's launchd default per-process
       open-file limit (256) is too low for this app - scheduler.py fans out
       up to cycle_max_parallel_tickers concurrent tickers, and each ticker's
       analyze() can spawn up to 9 concurrent MCP calls, several of which are
       a FRESH subprocess (uvx/npx, no persistent session). A burst of
       concurrent ticker analysis was hitting [Errno 24] Too many open files,
       which then cascaded into unrelated-looking failures in the SAME
       process - SQLite "unable to open database file", Maverick's SSE
       stream erroring, yfinance calls failing - all at once, since they all
       need a free file descriptor. Raising both soft and hard limits here
       gives real headroom without needing any Python-level change.
  -->
  <key>SoftResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key><integer>4096</integer>
  </dict>
  <key>HardResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key><integer>8192</integer>
  </dict>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- launchd gives services a minimal PATH; the scheduler spawns npx/uvx
         subprocesses (MCP servers) and Maverick's make dev needs its own
         toolchain, so bake in the installing shell's full PATH. -->
    <key>PATH</key><string>$PATH</string>
    <key>HOME</key><string>$HOME</string>
    <!-- §38: the per-version environment. Without these, a promoted version
         would write into the shared output/ and talk to the shared database,
         which is the collision the version manager exists to prevent. -->
    <key>TP_OUTPUT_DIR</key><string>$OUT_DIR</string>
    <key>POSTGRES_DB</key><string>${POSTGRES_DB:-trading_platform}</string>
    <key>TP_VERSION</key><string>${TP_VERSION:-unversioned}</string>
  </dict>
</dict>
</plist>
PLIST
  echo "  wrote $(_plist_path "$name")"
}

_free_port_8080() {
  # 2026-07-16 (Akhil's 'UI :8080 DOWN' report): a stray orphaned UI process
  # from an earlier run.sh/terminal session held port 8080 - not serving,
  # just squatting - so the launchd UI service crash-looped on
  # "[Errno 48] address already in use" forever (KeepAlive faithfully
  # relaunching it into the same wall every 10s). pkill-by-command-line
  # missed it; killing by SOCKET can't. Boot out our own UI service first so
  # we never shoot the healthy launchd-managed copy.
  launchctl bootout "gui/$(id -u)/$LABEL_PREFIX.ui" 2>/dev/null || true
  local pids
  pids=$(lsof -ti:8080 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "  freeing port 8080 (stray pid(s): $pids)"
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
}

_bootstrap() {
  local name="$1"
  launchctl bootout "gui/$(id -u)/$LABEL_PREFIX.$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$(_plist_path "$name")"
  echo "  started $LABEL_PREFIX.$name"
}

_bootout() {
  local name="$1"
  launchctl bootout "gui/$(id -u)/$LABEL_PREFIX.$name" 2>/dev/null \
    && echo "  stopped $LABEL_PREFIX.$name" \
    || echo "  $LABEL_PREFIX.$name was not running"
}

cmd_install() {
  echo "Installing launchd services (app dir: $APP_DIR)..."
  # scheduler, wrapped in caffeinate -i when available so the Mac doesn't
  # idle-sleep through scan windows (display can still sleep/lock).
  if [ -n "$CAFFEINATE" ]; then
    _write_plist scheduler "$CAFFEINATE" -i "$PYTHON3" scheduler.py
  else
    _write_plist scheduler "$PYTHON3" scheduler.py
  fi
  _write_plist ui "$PYTHON3" main.py --ui
  if [ -n "$MAVERICK_CMD" ]; then
    _write_plist maverick /bin/bash -c "$MAVERICK_CMD"
  else
    echo "  maverick: skipped (set MAVERICK_CMD=... or install to ~/maverick-mcp, then re-run install)"
  fi
  # Don't let a leftover foreground run.sh session fight launchd.
  pkill -f "python3 scheduler.py" 2>/dev/null || true
  pkill -f "python3 main.py --ui" 2>/dev/null || true
  sleep 1
  _free_port_8080
  for s in $SERVICES; do
    [ -f "$(_plist_path "$s")" ] && _bootstrap "$s"
  done
  echo ""
  echo "Done. Everything now runs in the background - you can close this terminal."
  echo "  UI:      http://localhost:8080"
  echo "  status:  ./service.sh status"
  echo "  logs:    ./service.sh logs"
}

cmd_start() {
  _free_port_8080
  for s in $SERVICES; do
    [ -f "$(_plist_path "$s")" ] && _bootstrap "$s"
  done
}

cmd_stop() {
  for s in $SERVICES; do _bootout "$s"; done
  # KeepAlive is gone once booted out, but make sure the processes are too.
  pkill -f "python3 scheduler.py" 2>/dev/null || true
  pkill -f "python3 main.py --ui" 2>/dev/null || true
}

cmd_restart() { cmd_stop; sleep 2; cmd_start; }

cmd_uninstall() {
  cmd_stop
  for s in $SERVICES; do
    rm -f "$(_plist_path "$s")" && echo "  removed $LABEL_PREFIX.$s.plist"
  done
}

cmd_status() {
  echo "launchd services:"
  for s in $SERVICES; do
    if launchctl print "gui/$(id -u)/$LABEL_PREFIX.$s" >/dev/null 2>&1; then
      pid=$(launchctl print "gui/$(id -u)/$LABEL_PREFIX.$s" 2>/dev/null | awk '/pid =/{print $3; exit}')
      if [ -n "$pid" ]; then
        echo "  $s: RUNNING (pid $pid)"
      else
        echo "  $s: loaded but NOT alive - likely crash-looping; check: tail $OUT_DIR/logs/launchd_$s.log"
      fi
    elif [ -f "$(_plist_path "$s")" ]; then
      echo "  $s: installed but NOT loaded (./service.sh start)"
    else
      echo "  $s: not installed"
    fi
  done
  echo ""
  echo "health checks:"
  curl -s -m 3 -o /dev/null http://localhost:8080 && echo "  UI :8080        OK" || echo "  UI :8080        DOWN"
  curl -s -m 3 -o /dev/null http://127.0.0.1:8003/mcp && echo "  Maverick :8003  OK" || echo "  Maverick :8003  DOWN"
}

cmd_logs() {
  tail -n 20 -f "$OUT_DIR"/logs/launchd_scheduler.log "$OUT_DIR"/logs/launchd_ui.log "$OUT_DIR"/logs/launchd_maverick.log 2>/dev/null
}

# When sourced (tests), just expose the functions - don't dispatch.
[ "${BASH_SOURCE[0]}" != "$0" ] && return 0

case "$1" in
  install)   cmd_install ;;
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  restart)   cmd_restart ;;
  status)    cmd_status ;;
  uninstall) cmd_uninstall ;;
  logs)      cmd_logs ;;
  *) grep '^#' "$0" | sed -n '2,40p' | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
