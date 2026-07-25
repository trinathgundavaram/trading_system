#!/usr/bin/env bash
# scripts/apply_migration.sh - apply a migration to the RIGHT database.
#
#     ./scripts/apply_migration.sh migrations/007_learning_data_quarantine.sql
#     ./scripts/apply_migration.sh migrations/007_*.sql --yes    # no prompt
#
# WHY THIS EXISTS
#
# Every migration header used to read `psql "$POSTGRES_DB" -f ...`. But
# POSTGRES_DB is NOT set in this project's .env - only in .env.template - and
# storage/database.py supplies "trading_platform" as a CODE default rather than
# an environment one. So that command expanded to `psql "" -f ...`, and psql
# with an empty database name falls back to $USER:
#
#     psql: FATAL: database "trinathrao" does not exist
#
# The error is the lucky outcome. Had a database with that name existed, the
# migration would have applied cleanly to the wrong one and reported success -
# and the same conftest.py docstring that documents the 2026-07-25 incident
# already warned about exactly this: POSTGRES_DB being unset RESOLVES to
# production through a code default, so anything that merely reads the variable
# is asking the wrong question.
#
# This script resolves the connection the same way storage/database.py does, in
# the same order, so the docs and the code cannot drift apart.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Arguments in any order, so `--yes <file>` works as well as `<file> --yes`.
# `${1:?msg}` was the first version of this and it renders as
# "apply_migration.sh: line 29: 1: usage: ..." - bash prefixing the parameter
# name and line number onto the message. That reads like an internal error
# rather than a usage note, which is the wrong first impression from a script
# whose entire job is to be trusted with the production database.
FILE=""
ASSUME_YES=""
for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES='--yes' ;;
    -h|--help) FILE="" ; break ;;
    *) [ -n "$FILE" ] || FILE="$arg" ;;
  esac
done

usage() {
  cat >&2 <<USAGE
usage: ./scripts/apply_migration.sh <file.sql> [--yes]

Applies one migration to the database this project actually uses, resolving
the connection the same way storage/database.py does.

Keep it on ONE line - a newline before the filename leaves this script with no
argument and hands the .sql file to your shell as a command.

Available:
USAGE
  for f in migrations/*.sql; do
    printf '  %s\n' "$f" >&2
  done
  echo >&2
  echo "e.g.  ./scripts/apply_migration.sh migrations/007_learning_data_quarantine.sql" >&2
}

if [ -z "$FILE" ]; then usage; exit 2; fi
if [ ! -f "$FILE" ]; then
  echo "FAIL: no such file: $FILE" >&2
  echo >&2
  usage
  exit 1
fi

# Read the POSTGRES_* keys out of .env WITHOUT sourcing it.
#
# `set -a; . ./.env` is the obvious way and it is wrong here. This project's
# .env contains placeholder lines like
#
#     NVIDIA_API_KEY=<rotate this key at https://build.nvidia.com>
#
# and `<` is shell redirection, so sourcing fails with a syntax error - which
# under `set -e` aborts this script before it reaches psql, for a reason that
# has nothing to do with the migration. Sourcing also EXECUTES whatever is in
# there: a value containing $(...) would run as a command substitution, and a
# secrets file is the last file that should be executable by accident.
#
# So: extract one key at a time, take the first match, strip surrounding
# quotes, and evaluate nothing.
env_get() {
  [ -f .env ] || return 0
  sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1=//p" .env 2>/dev/null \
    | head -n1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

# Precedence matches storage/database.py: a real environment variable wins over
# .env, and the code default is the last word.
DB="${POSTGRES_DB:-$(env_get POSTGRES_DB)}";      DB="${DB:-trading_platform}"
HOST="${POSTGRES_HOST:-$(env_get POSTGRES_HOST)}"; HOST="${HOST:-localhost}"
PORT="${POSTGRES_PORT:-$(env_get POSTGRES_PORT)}"; PORT="${PORT:-5432}"
USER_="${POSTGRES_USER:-$(env_get POSTGRES_USER)}"; USER_="${USER_:-${USER:-postgres}}"

# An empty name is what caused this in the first place; never pass one on.
[ -n "$DB" ] || { echo 'FAIL: resolved database name is empty'; exit 1; }

# The test harness pins POSTGRES_DB to a scratch database. Applying a
# production migration there is harmless but means you have not migrated what
# you think you have, so say so rather than let it look done.
case "$DB" in
  *_test|tests) echo "FAIL: \$POSTGRES_DB is '$DB', which is a TEST database."
                echo "      Unset it, or set it to the database you meant."
                exit 1 ;;
esac

echo "file:     $FILE"
echo "database: $DB  ($USER_@$HOST:$PORT)"
# The rollback_safe claim, shown BEFORE the prompt - it is the one fact you
# want in front of you at the moment you decide. Matched anywhere in a comment
# rather than at line start: every migration writes it mid-sentence
# ("Purely additive, nullable. rollback_safe: true."), and the first version of
# this anchored on '^-- rollback_safe', so it silently matched nothing and the
# prompt appeared with no safety note at all.
ROLLBACK=$(grep -m1 -o 'rollback_safe: *[a-z]*' "$FILE" 2>/dev/null || true)
echo "rollback: ${ROLLBACK:-not stated in this file - read it before applying}"

if [ "$ASSUME_YES" != '--yes' ]; then
  read -rp "Apply to '$DB'? [y/N] " ok
  [ "$ok" = y ] || { echo 'aborted'; exit 1; }
fi

# ON_ERROR_STOP=1 is the point of using this over a bare psql call.
#
# WITHOUT it, psql executes the remaining statements after a failure and exits
# 0. A migration whose first ALTER fails would report success while leaving the
# schema half-applied - a command that fails to "everything is fine", which is
# the same defect class as classify_change.py returning PATCH on an unreadable
# diff. A migration runner that cannot fail is worse than none, because the
# next thing you do is trust it.
#
# --single-transaction: all or nothing. A partially-applied migration is the
# state nobody has a rollback for, since the backward SQL at the bottom of each
# file assumes the forward half completed.
psql --host "$HOST" --port "$PORT" --username "$USER_" --dbname "$DB" \
     --single-transaction --set ON_ERROR_STOP=1 -f "$FILE"

echo
echo "applied. Now verify it is actually in force:"
echo "    python3 scripts/verify_phase2.py"
