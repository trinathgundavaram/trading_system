#!/usr/bin/env bash
# Caffeinated + logged wrapper for the scripts/sweep_*_configs.py family
# (2026-07-28). Originally written for sweep_backtest_configs.py, now also
# covers scripts/sweep_regime_timed_configs.py (the regime-timing-overlay
# sweep) - both are multi-hour runs with the exact same two problems:
#   1. macOS can idle-sleep partway through and silently kill the run.
#   2. All that progress output only lives in the terminal scrollback - if
#      the terminal is closed/backgrounded, it's gone.
# `caffeinate -i` blocks idle sleep for as long as this script's own process
# tree is alive (i.e. exactly as long as the sweep is running, no manual
# caffeinate/kill bookkeeping needed). `tee` mirrors everything to a
# timestamped log file so you can `tail -f` it from another terminal, or read
# it after the fact even if this shell got closed.
#
# NOTE: caffeinate -i stops IDLE sleep, not LID-CLOSE sleep on a laptop -
# keep the lid open (or plugged in with an external display) for the run.
#
# Usage:
#   ./scripts/run_sweep_caffeinated.sh --starting-equity 100000
#     (defaults to sweep_backtest_configs.py, any args passed straight through)
#   ./scripts/run_sweep_caffeinated.sh scripts/sweep_regime_timed_configs.py --starting-equity 100000
#     (first arg ending in .py picks a different sweep script to run)
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="scripts/sweep_backtest_configs.py"
if [[ "${1:-}" == *.py ]]; then
    TARGET="$1"
    shift
fi

LOG="sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Caffeinated run starting ($TARGET) - macOS idle sleep is blocked until this finishes."
echo "Live output also being written to: $LOG"
echo "(tail -f \"$LOG\" from another terminal to watch it without this window)"
echo

caffeinate -i python3 "$TARGET" "$@" 2>&1 | tee "$LOG"
