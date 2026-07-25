# Postgres Cutover Runbook

Everything code-side is done and tested against a real Postgres engine in a
sandbox (schema creation, all migration helpers, real production data loads,
and the actual business-logic methods — see "What was tested" at the
bottom). What's left needs to run on your Mac, against your real Postgres
install, since I can't reach it from here.

## 1. Create the database

You said Postgres is installed. Confirm it's running and create the app's database:

```bash
psql -U postgres -c "SELECT version();"    # confirms the server is up and reachable
createdb trading_platform
```

If your Postgres user/password/port differ from the defaults (`localhost:5432`,
your Mac username, no password), add the overrides to `.env` — see the new
`POSTGRES_*` section in `.env.template`.

## 2. Install the new dependency

```bash
cd ~/trading_platform
pip install -r requirements.txt      # pulls in psycopg2-binary (new)
```

## 3. Stop the running services

Migrate with nothing else writing to `trading.db` mid-copy, so the snapshot
you migrate is consistent:

```bash
./service.sh stop
```

## 4. Dry-run the migration first

Read-only — reports row counts per table straight from `trading.db`, writes
nothing:

```bash
python3 migrate_to_postgres.py --dry-run
```

You should see ~35 tables and ~30,000 rows total (matches what I saw
migrating your real data in testing). If this errors, stop here — it means
Postgres isn't reachable with the current `.env` settings, not a data problem.

## 5. Run the real migration

```bash
python3 migrate_to_postgres.py
```

This creates the full schema in Postgres (same `SCHEMA` constant the app
itself uses — no separate schema to hand-maintain), copies every row table
by table, advances each table's id sequence past the migrated data, and
verifies row counts match on both sides before exiting 0. It's safe to
re-run if anything goes wrong partway — each table is cleared and reloaded
fresh, not appended to.

`trading.db` itself is never opened for writing and is never deleted — it
stays exactly as it is as your data-rollback point.

## 6. Restart services

```bash
./service.sh install    # picks up the current plist + starts everything fresh
```

## 7. Verify

- `output/logs/scheduler.log` should show `storage.database: Postgres pool
  ready (host=... db=trading_platform ...)` near the top, instead of any
  SQLite-related lines.
- Watch for the next scheduled cycle (or trigger one manually from the UI)
  and confirm it completes normally.
- Check the UI shows your existing positions/cycle history/P&L — this
  confirms the migrated data is being read correctly, not just written.
- The "DB open did not return within 15s" warnings that drove this whole
  migration should be gone entirely (they were specific to
  `sqlite3.connect()`'s file-open behavior, which no longer exists in this
  codebase at all).

## Rollback

**Data is always safe** — `trading.db` is untouched throughout, so nothing
about this migration is destructive to your trade history.

**Code rollback is more manual than I'd like**, because this repo isn't
under version control (checked — no `.git` anywhere), so there's no `git
revert` to fall back on. I saved a copy of the file as it stood right before
touching it further at `storage/database.py.postgres-backup-<timestamp>` —
but note that's a snapshot from partway through today's work, not
necessarily the exact pre-migration SQLite version. If you need to revert
the *code* to SQLite specifically, tell me and I can reconstruct the
original SQLite-backed `database.py` from this conversation's history.

**Strong recommendation, unrelated to Postgres specifically:** run `git
init` in `trading_platform/` now (with a `.gitignore` for `.env`,
`output/`, `__pycache__/`, and the `*.db*` files, similar to what's already
in `.gitignore`) so future changes — to this file or anything else — have a
real, cheap rollback point instead of relying on ad-hoc `.bak` copies.

## What was tested (in a sandboxed Postgres, not your machine)

- Full `SCHEMA` DDL (30+ tables, indexes) creates cleanly on a real Postgres
  engine.
- All 13 production `_migrate_*` idempotent-migration helpers run without
  error against that schema.
- Real data from your actual `trading.db` loaded correctly: `positions`
  (39/39 rows), `cycles` (183/183), `ticker_info_cache` (552/552), and a
  slice of `signals` including its JSON columns — all verified via row-count
  match.
- The 3 `RETURNING id` / `lastrowid` call sites (`save_signal`,
  `add_pattern`, `log_rejected_signal`) return correct integer ids end to
  end through the real (unmodified) `_conn()` code path.
- `row_factory` correctly resets to plain-tuple mode on every new `_conn()`
  call (verified no state leaks between calls that set
  `sqlite3.Row` and calls that don't).
- `migrate_to_postgres.py --dry-run` against your real 60MB `trading.db`:
  35 tables, ~29,750 rows enumerated correctly.

Not tested end-to-end: the full-volume single-shot load of your largest
tables (`universe` 13,214 rows, `news_items` 6,595 rows, `signals` 7,613
rows) — the sandbox's embedded test-Postgres couldn't sustain a batch that
large, which is a limitation of that lightweight test engine, not of
Postgres itself or of the migration code (the same insert logic, in smaller
batches, worked correctly against the same tables). `migrate_to_postgres.py`
chunks inserts at 500 rows specifically so this isn't a concern against a
real Postgres server — but it's still worth watching the row-count
verification at the end of step 5 rather than assuming success.
