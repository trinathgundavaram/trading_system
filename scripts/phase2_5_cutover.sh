#!/usr/bin/env bash
# =============================================================================
#  scripts/phase2_5_cutover.sh - the Phase 2.5 operational tail (§B1-§B9),
#  in the one order that works, with a gate between every step.
#
#  Phase 2.5 (§48-§55) and the second review's follow-ups (§C1-§C3, §D) are
#  code, and code is done. What is left needs the live database, and it needs
#  to happen in sequence because each step's output is the next step's input:
#
#      B1  assess          read-only. What is actually contaminated.
#      B2  backup          verified pg_dump. The restore point for B3/B6.
#      B3  purge           §49. Deletes mae_mfe_data test residue.
#      B4  re-assess       proves B3 finished. migrations/010 and 012 will
#                          refuse to apply until it did.
#      B5  migrations      009, 010, 011, 012.
#      B6  reset           §48. Destructive. Wipes purse/ledger/positions/curve.
#      B7  re-baseline     init_paper_account + backfill_drawdown.
#      B8  calibrate       §52. Recommends caps. Writes nothing.
#      B9  tests           full suite against Postgres (93 skip without it).
#
#  DRY RUN BY DEFAULT, like scripts/repair_test_damage.py. Prints what each
#  step would do and changes nothing until you pass --apply.
#
#  WHY A SCRIPT AND NOT A CHECKLIST
#
#  Because the ordering constraint is not advisory. Running B5 before B3 gets
#  you a migration that aborts (by design - see migrations/010's guard note).
#  Running B8 before B6 gets you a calibration against an equity curve that
#  spans a purse re-seed, which is how v1.3.1 came to exist: a 1491 -> 1000
#  step reads as a 33% intraday drawdown and blocks entries for the rest of
#  the day for an accounting event. A checklist relies on the reader not
#  skipping; this refuses.
#
#  RESUMABLE. --from <step> picks up mid-sequence, because the most likely
#  failure is B5 aborting on data B3 did not fully clean, and re-running B1-B4
#  after fixing that is wasted work, not extra safety.
#
#  Usage:
#      ./scripts/phase2_5_cutover.sh                          # dry run, all steps
#      ./scripts/phase2_5_cutover.sh --apply
#      ./scripts/phase2_5_cutover.sh --apply --from B5
#      ./scripts/phase2_5_cutover.sh --apply --starting-cash 1000
#      ./scripts/phase2_5_cutover.sh --only B1                # just look
# =============================================================================
set -uo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$(readlink -f "$0")")")"
REPO="$PWD"
PY="${PYTHON:-python3}"

APPLY=""
FROM="B1"
ONLY=""
STARTING_CASH=""
RESET_CONFIRM_PHRASE="RESET AND REBASELINE"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)          APPLY=1 ;;
    --from)           FROM="${2:?--from needs a step id, e.g. B5}"; shift ;;
    --only)           ONLY="${2:?--only needs a step id, e.g. B1}"; shift ;;
    --starting-cash)  STARTING_CASH="${2:?--starting-cash needs a number}"; shift ;;
    -h|--help)        sed -n '2,48p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ── output helpers ──────────────────────────────────────────────────────────
BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
step()  { printf '\n%s\n%s  %s\n%s\n' "════════════════════════════════════════════════════════════════════" "${BOLD}$1${RESET}" "$2" "════════════════════════════════════════════════════════════════════"; }
note()  { printf '  %s\n' "$*"; }
warn()  { printf '  !! %s\n' "$*"; }
die()   { printf '\n  ABORT: %s\n\n' "$*" >&2; exit 1; }

# Steps are ordered; --from compares position, not string equality.
STEPS=(B1 B2 B3 B4 B5 B6 B7 B8 B9)
_idx() { local i=0; for s in "${STEPS[@]}"; do [ "$s" = "$1" ] && { echo "$i"; return; }; i=$((i+1)); done; echo -1; }
FROM_I=$(_idx "$FROM"); [ "$FROM_I" -ge 0 ] || die "--from: unknown step '$FROM'"
if [ -n "$ONLY" ]; then _idx "$ONLY" >/dev/null; [ "$(_idx "$ONLY")" -ge 0 ] || die "--only: unknown step '$ONLY'"; fi

want() {
  local i; i=$(_idx "$1")
  if [ -n "$ONLY" ]; then [ "$1" = "$ONLY" ]; return; fi
  [ "$i" -ge "$FROM_I" ]
}

# Every mutating action goes through this, so dry-run cannot be forgotten for
# one branch. Read-only steps call their command directly.
run() {
  if [ -z "$APPLY" ]; then
    note "[dry run] would run: $*"
    return 0
  fi
  note "\$ $*"
  "$@" || return $?
}

confirm_phrase() {
  local phrase="$1"
  [ -n "$APPLY" ] || { note "[dry run] would require the phrase '$phrase'"; return 0; }
  local typed
  printf '\n  Type %s to proceed (anything else aborts): ' "'$phrase'"
  read -r typed || die "no input - nothing was changed"
  [ "$typed" = "$phrase" ] || die "phrase did not match - nothing was changed"
}

# ── preflight ───────────────────────────────────────────────────────────────
step "PREFLIGHT" "checking the tools this sequence needs exist"
for f in scripts/assess_test_damage.py scripts/repair_test_damage.py \
         scripts/apply_migration.sh scripts/backfill_drawdown.py \
         scripts/calibrate_risk_caps.py scripts/tp; do
  [ -f "$REPO/$f" ] || die "missing $f - are you in the right checkout?"
done
for m in 009_exit_kind 010_mae_mfe_integrity 011_entry_atr_pct 012_mae_mfe_fk; do
  ls "$REPO"/migrations/${m}*.sql >/dev/null 2>&1 || die "missing migrations/${m}*.sql"
done
command -v pg_dump >/dev/null || die "pg_dump not found - B2 cannot make a restore point"
note "all present."
[ -n "$APPLY" ] || warn "DRY RUN. Nothing will change. Re-run with --apply."

# ── B1 ──────────────────────────────────────────────────────────────────────
if want B1; then
  step "B1  ASSESS" "read-only. Establishes what is actually contaminated."
  note "This is the baseline every later step is checked against. Read the"
  note "mae_mfe_data section in particular - it is the one §49 found, and its"
  note "counts are what B3 has to bring to zero."
  "$PY" scripts/assess_test_damage.py || die "assess failed - fix that before going further"
fi

# ── B2 ──────────────────────────────────────────────────────────────────────
if want B2; then
  step "B2  BACKUP" "verified pg_dump. The restore point for B3 and B6."
  note "tp backup dumps, then reads the dump back before reporting success."
  note "An unverified backup is a belief, not a backup."
  run ./scripts/tp backup phase2_5_cutover || die "backup failed - refusing to continue"
  if [ -n "$APPLY" ]; then
    note ""
    note "Sync ~/tp/archive off-machine, but NEVER to the git remote - a dump"
    note "holds your positions and full signal history."
  fi
fi

# ── B3 ──────────────────────────────────────────────────────────────────────
if want B3; then
  step "B3  PURGE (§49)" "deletes mae_mfe_data test residue, by evidence not by window"
  note "mae_mfe_data has no provenance columns, so the time-window filter that"
  note "works for every neighbouring table cannot work here. Three predicates:"
  note "  - ticker in TEST_TICKERS"
  note "  - mae_pct = 0 AND mfe_pct = 0"
  note "  - trade_id resolving to no position of the same ticker"
  note ""
  note "Showing the dry run first regardless of --apply, because these counts"
  note "deserve to be read line by line before anything is deleted."
  "$PY" scripts/repair_test_damage.py || die "purge dry run failed"
  if [ -n "$APPLY" ]; then
    confirm_phrase "PURGE"
    run "$PY" scripts/repair_test_damage.py --apply || die "purge failed"
  fi
fi

# ── B4 ──────────────────────────────────────────────────────────────────────
if want B4; then
  step "B4  RE-ASSESS" "proves B3 finished. migrations/010 and 012 depend on it."
  note "If duplicate trade_ids or orphan rows survive here, 010 will refuse to"
  note "apply and 012 will RAISE with a message naming the count. That refusal"
  note "is the gate working - the fix is to finish the purge, NOT to drop the"
  note "unique index."
  "$PY" scripts/assess_test_damage.py || die "re-assess failed"
fi

# ── B5 ──────────────────────────────────────────────────────────────────────
if want B5; then
  step "B5  MIGRATIONS" "009 exit_kind, 010 mae_mfe integrity, 011 entry_atr_pct, 012 mae_mfe FK"
  note "Via apply_migration.sh, NOT psql \"\$POSTGRES_DB\" -f. POSTGRES_DB is"
  note "unset in this project's .env, so that expands to an empty database name"
  note "and psql silently falls back to \$USER."
  for m in 009_exit_kind 010_mae_mfe_integrity 011_entry_atr_pct 012_mae_mfe_fk; do
    f=$(ls "$REPO"/migrations/${m}*.sql | head -1)
    note ""
    note "-- ${m} --"
    run ./scripts/apply_migration.sh "$f" --yes \
      || die "${m} failed. If it named duplicate or orphan trade_ids, go back to B3; that is the guard, not a bug."
  done
fi

# ── B6 ──────────────────────────────────────────────────────────────────────
if want B6; then
  step "B6  RESET (§48)" "DESTRUCTIVE. Wipes purse, ledger, simulated positions, equity curve."
  note "Why the curve goes too: it is the input to every drawdown figure, and a"
  note "'clean slate' account that inherits the previous account's curve is the"
  note "measurement inconsistency this whole phase exists to remove."
  note ""
  note "NOT touched: pattern_database (the learning record) and mae_mfe_data."
  note "Your B2 backup is the only way back from this."
  if [ -z "$STARTING_CASH" ] && [ -n "$APPLY" ]; then
    die "B6 needs --starting-cash <amount> - the purse it re-seeds to. Refusing to guess."
  fi
  confirm_phrase "$RESET_CONFIRM_PHRASE"
  if [ -n "$APPLY" ]; then
    "$PY" - "$STARTING_CASH" <<'PYEOF' || die "reset failed"
import sys
sys.path.insert(0, ".")
from storage.database import Database
db = Database()
db.reset_paper_account()
db.init_paper_account(float(sys.argv[1]))
acct = db.get_paper_account()
print(f"  reset complete. purse cash ${acct['cash']:.2f}, "
      f"starting_cash ${acct['starting_cash']:.2f}")
print(f"  equity curve points remaining: "
      f"{len(db.get_paper_equity_history(limit=100000) or [])} (expect 0)")
PYEOF
  else
    note "[dry run] would reset_paper_account() then init_paper_account(${STARTING_CASH:-<required>})"
  fi
fi

# ── B7 ──────────────────────────────────────────────────────────────────────
if want B7; then
  step "B7  RE-BASELINE" "backfill_drawdown over the now single-epoch curve"
  note "Recomputes daily_stats drawdown for every day in paper_equity_history."
  note "After B6 that history starts empty and fills from the next cycle, so on"
  note "a fresh reset this is a no-op that confirms the reset took."
  run "$PY" scripts/backfill_drawdown.py || die "backfill failed"
fi

# ── B8 ──────────────────────────────────────────────────────────────────────
if want B8; then
  step "B8  CALIBRATE (§52)" "recommends max_intraday_drawdown_pct. Writes nothing."
  note "Read the output rather than pasting it. Two things to check:"
  note "  - Below --min-days it refuses to recommend. Four observations do not"
  note "    have a 99th percentile, and a number that looks derived is worse"
  note "    than no number."
  note "  - Any day showing >=10% intraday drawdown is far more likely a purse"
  note "    re-seed than a trading loss. If one appears, B6 did not take and"
  note "    the calibration is not ready."
  "$PY" scripts/calibrate_risk_caps.py || warn "calibration exited non-zero - read the output above"
  note ""
  note "config.yaml is NOT edited by this script. Setting the cap is a judgement,"
  note "and judgements get made by a person and recorded in the CHANGELOG."
fi

# ── B9 ──────────────────────────────────────────────────────────────────────
if want B9; then
  step "B9  TESTS" "full suite against Postgres"
  note "93 tests skip without a database. They have never run against the"
  note "Phase 2.5 changes, so this is the first real exercise of them."
  "$PY" -m pytest tests/ -q || die "tests failed - do NOT release"
fi

# ── what is left, and why it is not in this script ──────────────────────────
step "REMAINING" "needs elapsed time, not another command"
cat <<'EOF'
  Two calibrations cannot be done today, and automating them would only
  automate guessing:

  high_vol_atr_pct_threshold (still 5.0)
      §53 made the two sides of this comparison share a unit; the threshold
      itself is still the original guess. Recalibrate once the book has turned
      over enough that positions.entry_atr_pct is populated - grep the logs for
      "has no entry_atr_pct" and wait until that stops appearing. The packet
      now prints the proxy share on the high-vol line, so you can see it too.

  data_quality.stale_indicator_veto_threshold (still 3)
      §55's instrumentation is live but the sample does not exist yet. After a
      week:

        SELECT substr(timestamp, 12, 2) AS utc_hour, COUNT(*)
          FROM rejected_signals
         WHERE reject_stage = 'data_quality'
         GROUP BY 1 ORDER BY 1;

      Expect a spike in the first minutes after the open - VWAP needs intraday
      bars that have not accumulated. If that spike is the ONLY concentration,
      the threshold is fine and the right fix is a warm-up window, not a looser
      threshold.

  Then release. The decision function CHANGED (§53): portfolio_risk counts a
  different quantity as high-volatility, stricter than before, and that both
  sizes and blocks entries. config_fingerprint is unchanged, so pattern rows
  stay individually poolable - but anything reasoning about POSITION SIZING
  across this boundary has to account for it.
EOF

if [ -z "$APPLY" ]; then
  printf '\n  %s\n\n' "Dry run only. Re-run with --apply once the above looks right."
fi
