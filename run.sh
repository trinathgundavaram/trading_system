#!/bin/bash
echo "==================================="
echo " TRADING PLATFORM"
echo "==================================="
echo "Usage: ./run.sh [--ui]"
echo "  no flag: terminal dashboard (scheduler runs in this same process)"
echo "  --ui:    FastAPI web UI at http://localhost:8080 (scheduler runs as a"
echo "           SEPARATE process - main.py --ui does not scan on its own)"
echo ""

# MaverickMCP is optional (HTTP, localhost:8003) - warn but don't block.
curl -s http://127.0.0.1:8003/mcp > /dev/null 2>&1 \
  && echo "MaverickMCP: OK" \
  || echo "WARNING: MaverickMCP not running - start with: cd ~/maverick-mcp && make dev"

cd "$(dirname "$0")"

# ── Guard preflight (Phase 1 + Phase 2) ─────────────────────────────────────
#
# "Implemented" and "in force" are different claims, and the gap between them
# is where the 2026-07-25 incident lived. These two scripts answer the second
# question: are the config flags actually set, are the guards actually wired,
# is any write route unguarded. Source-only (--no-db), so this costs about a
# second and needs no database.
#
# WHAT A FAILURE DOES, and why it is split:
#
#   The web UI starts REGARDLESS. It is read-mostly and it is how you diagnose
#   the failure - a preflight that locks you out of the tool you would use to
#   fix the problem is a preflight people disable.
#
#   The SCHEDULER asks first. That is the process that scores tickers and
#   places paper trades, so it is the one whose guards have to be real. An
#   explicit y/N is the smallest thing that makes "I saw the warning" true
#   rather than assumed.
#
# TP_SKIP_PREFLIGHT=1 skips the whole thing. Deliberately an env var and not a
# flag: it should be awkward enough that nobody reaches for it by habit.
PREFLIGHT_FAILED=0
if [ "${TP_SKIP_PREFLIGHT:-0}" = "1" ]; then
  echo "preflight: SKIPPED (TP_SKIP_PREFLIGHT=1) - guards are unverified this run"
else
  echo "── preflight: are the Phase 1 + Phase 2 guards in force? ──"
  for check in "scripts/verify_phase1.py" "scripts/verify_phase2.py --no-db"; do
    # shellcheck disable=SC2086
    if OUT=$(python3 $check 2>&1); then
      echo "  OK   $(echo "$check" | awk '{print $1}')  $(echo "$OUT" | grep -E '^[0-9]+ checks:' | head -1)"
    else
      PREFLIGHT_FAILED=1
      echo "  FAIL $(echo "$check" | awk '{print $1}')"
      echo "$OUT" | grep -E '^\s+FAIL ' | sed 's/^/      /'
    fi
  done
  if [ "$PREFLIGHT_FAILED" = "1" ]; then
    echo
    echo "  One or more guards are NOT in force. Full detail:"
    echo "      python3 scripts/verify_phase1.py"
    echo "      python3 scripts/verify_phase2.py"
    echo "  The UI will still start - it is read-mostly and it is how you look."
  fi
  echo
fi

if [ "$1" = "--ui" ]; then
  # 2026-07-14: real incident - a previous run.sh's `main.py --ui` got
  # orphaned (terminal window closed without triggering the EXIT trap below,
  # which only fires on a CLEAN shell exit - Ctrl+C or `exit`) and kept
  # holding port 8080 invisibly. The next `./run.sh --ui` then failed to bind
  # with a cryptic "[Errno 48] address already in use" buried in scrolling
  # output, while the browser kept talking to the old dead process - looked
  # exactly like a frontend bug ("keeps loading, shows nothing") when it was
  # really "you have two of these running."
  #
  # Same-day follow-up: an initial version of this fix just detected the
  # conflict and told the user to run a kill command by hand before
  # retrying. That's not actually a fix for the underlying problem - a
  # terminal window closed via the red button, a force-quit, a crashed
  # terminal emulator, or the laptop sleeping mid-session can all tear down
  # the shell via a path the EXIT trap below never gets a chance to run for,
  # so this was going to keep recurring no matter how carefully Ctrl+C gets
  # used. Self-heal instead: on a single-user local dev machine, anything
  # already on 8080 (or a stray scheduler.py) when this script starts is
  # essentially always a leftover from exactly this scenario, never a real
  # conflict with some unrelated service - so clean it up automatically and
  # continue, rather than making the user diagnose and fix it every time.
  STALE_UI_PID=$(lsof -ti:8080 2>/dev/null)
  if [ -n "$STALE_UI_PID" ]; then
    echo "Port 8080 was already in use (PID $STALE_UI_PID) - almost certainly a"
    echo "previous run.sh --ui whose terminal didn't shut it down cleanly."
    echo "Killing it and continuing..."
    kill -9 $STALE_UI_PID 2>/dev/null
    sleep 1
  fi
  STALE_SCHED_PIDS=$(pgrep -f "python3 scheduler.py" 2>/dev/null)
  if [ -n "$STALE_SCHED_PIDS" ]; then
    echo "Found leftover scheduler.py process(es) (PID $STALE_SCHED_PIDS) from an"
    echo "earlier run - killing before starting a fresh one..."
    kill -9 $STALE_SCHED_PIDS 2>/dev/null
    sleep 1
  fi

  # 2026-07-14: Trinath asked how to keep the Mac from sleeping while this
  # runs, even with zero activity, so the scheduler doesn't miss scan windows
  # and (per the incident above) a lid-close/sleep event can't contribute to
  # another orphaned-process situation. `caffeinate -i -w $$` holds a
  # prevent-idle-sleep assertion for exactly as long as THIS shell (run.sh
  # itself) is alive - it notices on its own when $$ exits, for any reason
  # (Ctrl+C, crash, kill), and releases the assertion then, so there's
  # nothing extra to remember to turn back off. `-i` only blocks idle sleep,
  # not display sleep, so the screen can still dim/lock normally. macOS-only;
  # skips silently (falls back to normal sleep behavior) anywhere caffeinate
  # doesn't exist.
  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i -w $$ &
    echo "caffeinate: system will stay awake (idle sleep blocked) for as long as this session runs"
  fi

  # ── Supervision (2026-07-16, Akhil's ask: auto-restart when down) ──
  # Each long-running piece gets a restart loop: if the process exits for
  # ANY reason (crash, hang killed by hand, OOM), it's relaunched after 5s.
  # A marker file tears the loops down cleanly on exit so `wait` doesn't
  # respawn things while you're trying to stop them.
  mkdir -p output/logs
  RUN_MARKER="output/.run_sh_alive"
  touch "$RUN_MARKER"

  supervise() {
    local name="$1"; shift
    while [ -f "$RUN_MARKER" ]; do
      echo "$(date '+%H:%M:%S') starting $name..."
      "$@"
      local code=$?
      [ -f "$RUN_MARKER" ] || break
      echo "$(date '+%H:%M:%S') $name exited (code $code) - restarting in 5s..."
      sleep 5
    done
  }

  supervise "web UI (main.py --ui)" python3 main.py --ui &
  UI_SUP_PID=$!

  # The scheduler is the process that scores tickers and places paper trades,
  # so it is the one whose guards have to be real. Asked AFTER the UI is
  # already coming up, so you can answer with the dashboard in front of you.
  START_SCHEDULER=1
  if [ "$PREFLIGHT_FAILED" = "1" ]; then
    echo
    echo "  The SCHEDULER trades. Its guards did not verify (see above)."
    read -rp "  Start the scheduler anyway? [y/N] " ok
    [ "$ok" = y ] || START_SCHEDULER=0
  fi
  SCHED_SUP_PID=""
  if [ "$START_SCHEDULER" = "1" ]; then
    supervise "scheduler" python3 scheduler.py &
    SCHED_SUP_PID=$!
  else
    echo "  scheduler NOT started. The UI is up; fix the guards, then re-run."
  fi

  # ── MaverickMCP watchdog ── optional local dependency (localhost:8003).
  # If MAVERICK_CMD is set (or ~/maverick-mcp exists, the default install
  # location), ping the health endpoint every 60s and (re)start the server
  # whenever it's down - Maverick blips no longer degrade cycles until
  # someone notices. Its output goes to output/logs/maverick.log.
  if [ -z "$MAVERICK_CMD" ] && [ -d "$HOME/maverick-mcp" ]; then
    MAVERICK_CMD="cd $HOME/maverick-mcp && make dev"
  fi
  MAV_SUP_PID=""
  if [ -n "$MAVERICK_CMD" ]; then
    (
      while [ -f "$RUN_MARKER" ]; do
        if ! curl -s -m 3 http://127.0.0.1:8003/mcp > /dev/null 2>&1; then
          echo "$(date '+%H:%M:%S') MaverickMCP down - (re)starting: $MAVERICK_CMD"
          bash -c "$MAVERICK_CMD" >> output/logs/maverick.log 2>&1 &
          sleep 20   # give it time to bind before re-checking
        fi
        sleep 60
      done
    ) &
    MAV_SUP_PID=$!
    echo "MaverickMCP watchdog: ON (checks :8003 every 60s; restart cmd: $MAVERICK_CMD)"
  else
    echo "MaverickMCP watchdog: off (set MAVERICK_CMD=... or install to ~/maverick-mcp to enable)"
  fi

  trap "rm -f '$RUN_MARKER'; kill $UI_SUP_PID $SCHED_SUP_PID $MAV_SUP_PID 2>/dev/null; \
        pkill -f 'python3 main.py --ui' 2>/dev/null; pkill -f 'python3 scheduler.py' 2>/dev/null" EXIT

  # Auto-open the browser once the server actually answers, instead of
  # leaving it to open manually - poll rather than a fixed sleep since
  # startup time varies (Maverick availability check, DB init, etc.).
  (
    for _ in $(seq 1 60); do
      if curl -s -o /dev/null http://localhost:8080; then
        command -v open >/dev/null 2>&1 && open http://localhost:8080
        break
      fi
      sleep 1
    done
  ) &

  wait
else
  # Terminal dashboard: main.py runs the scheduler IN THIS PROCESS (see the
  # usage text at the top), so a failed preflight gates it the same way the
  # --ui branch gates the separate scheduler. There is no read-only half to
  # let through here - the whole thing trades.
  if [ "$PREFLIGHT_FAILED" = "1" ]; then
    echo "  This mode runs the scheduler IN THIS PROCESS - it trades."
    read -rp "  Start anyway? [y/N] " ok
    [ "$ok" = y ] || { echo "  aborted. Fix the guards, or use ./run.sh --ui to look first."; exit 1; }
  fi
  python3 main.py
fi
