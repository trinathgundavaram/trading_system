#!/usr/bin/env python3
"""Rehearse the Phase 2.5 cutover against a THROWAWAY database (§B0).

WHY THIS EXISTS. scripts/phase2_5_cutover.sh runs nine steps against the live
database, three of which are destructive and one of which (B5) applies four
migrations whose whole design is to REFUSE on unclean data. That refusal had
never been executed anywhere. It was assumed.

Rehearsing it found a defect that would have stopped B5 dead: migrations/012's
guard ran `trade_id !~ '^[0-9]+$'` unconditionally, and storage/database.py's
SCHEMA had since been updated to declare trade_id as INTEGER - so on any
database created by init_db(), which is every database `tp install` makes, the
migration died with `operator does not exist: integer !~ unknown` before
reading a single row. See migrations/012's step 0 comment.

WHAT IT PROVES. Two databases, because two shapes exist in the world:

  FRESH   built by the app's own Database.init_db(). Born post-012.
  LEGACY  a replica of the live box: trade_id TEXT, id TEXT holding uuid4,
          no FK, seeded with the 2026-07-25 contamination - duplicate
          trade_id, an orphan, and a non-numeric value.

Both must converge on the same shape; 010 and 012 must refuse on the legacy
shape until the purge has run; 012 must be re-runnable; and deleting a
position must NULL the excursion row rather than cascade it away - which is
the property reset_paper_account()'s docstring promises and nothing tested.

USAGE. Point it at a scratch Postgres. It refuses to touch anything that looks
like the real database, because it drops tables:

    POSTGRES_DB=tp_rehearsal python3 scripts/rehearse_cutover.py

Any Postgres will do. There does not need to be a spare server: PGlite's WASM
build works and is what this was developed against -
`npx @electric-sql/pglite-socket --port 55432` then POSTGRES_PORT=55432.
PGlite serves one client at a time, which is why TP_PG_POOL_MIN exists; this
script sets it to 1 for itself.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BANNER = "=" * 74

# Databases this script must never open. It drops and rebuilds tables; pointing
# it at the real one would destroy the thing the cutover is trying to protect.
FORBIDDEN_DB = {"trading_platform", "postgres_live", ""}

FAILURES: list[str] = []


def guard_target() -> dict:
    # One connection: a scratch server may not be able to give us two, and
    # nothing here is concurrent. Set before storage.database is imported,
    # since the bounds are read at module scope.
    os.environ.setdefault("TP_PG_POOL_MIN", "1")
    os.environ.setdefault("TP_PG_POOL_MAX", "1")

    db = os.getenv("POSTGRES_DB", "")
    if db.lower() in FORBIDDEN_DB or db.lower().startswith("tp_v"):
        sys.exit(
            f"refusing to run against POSTGRES_DB={db!r}.\n"
            f"This script DROPS AND REBUILDS tables. Point it at a scratch\n"
            f"database - e.g. POSTGRES_DB=tp_rehearsal - never at the live one\n"
            f"and never at a `tp install` version database."
        )
    return dict(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=db,
        user=os.getenv("POSTGRES_USER") or os.getenv("USER") or "postgres",
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


def sql_of(name: str) -> str:
    return (REPO / "migrations" / name).read_text()


def strip_psql_meta(sql: str) -> str:
    """psql meta-commands (\\echo, \\set) are not wire-protocol statements."""
    return "\n".join(l for l in sql.splitlines() if not l.strip().startswith("\\"))


def attempt(cur, sql):
    try:
        cur.execute(sql)
        return True, ""
    except Exception as e:
        cur.execute("ROLLBACK")
        return False, f"{type(e).__name__}: {e}".strip().replace("\n", " ")[:200]


def migrations(lo: int, hi: int):
    for p in sorted((REPO / "migrations").glob("*.sql")):
        n = int(p.name.split("_")[0])
        if lo <= n <= hi:
            yield p.name


def expect(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         expected {want!r}, got {got!r}")
        FAILURES.append(label)
    return ok


def shape(cur) -> dict:
    cur.execute("""select column_name, data_type from information_schema.columns
                   where table_name='mae_mfe_data' and column_name in ('id','trade_id')""")
    return dict(cur.fetchall())


def fk_of(cur) -> list:
    cur.execute("""
        select c.conname, c.confdeltype from pg_constraint c
        join unnest(c.conkey) k(attnum) on true
        join pg_attribute a on a.attrelid=c.conrelid and a.attnum=k.attnum
        where c.conrelid='mae_mfe_data'::regclass and c.contype='f'
          and a.attname='trade_id'""")
    return cur.fetchall()


def ensure_database(dsn: dict, recreate: bool) -> None:
    """Create the scratch database if it is not there.

    DATABASE A is only meaningful if it is genuinely fresh - the whole claim
    is "this is what init_db() produces from nothing". So a leftover database
    from a previous rehearsal is not a convenience, it is a different test.
    Missing: created. Present and empty: used. Present with tables: refused
    unless --recreate, because silently reusing it would quietly weaken the
    one assertion this script exists to make.

    (Developed against PGlite, which ignores the database name entirely - so
    this path was never exercised until it met a real Postgres. Noted because
    it is the same class of gap as migrations/012's: a code path that only the
    unusual environment takes.)"""
    import psycopg2
    from psycopg2 import sql

    admin = dict(dsn, dbname="postgres")
    conn = psycopg2.connect(**admin)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dsn["dbname"],))
    exists = cur.fetchone() is not None

    if exists and recreate:
        cur.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(dsn["dbname"])))
        print(f"  dropped and will recreate {dsn['dbname']}")
        exists = False

    if not exists:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dsn["dbname"])))
        print(f"  created scratch database {dsn['dbname']}")
    cur.close()
    conn.close()

    if exists:
        c2 = psycopg2.connect(**dsn)
        c2.autocommit = True
        k = c2.cursor()
        k.execute("SELECT COUNT(*) FROM information_schema.tables "
                  "WHERE table_schema='public'")
        n = k.fetchone()[0]
        c2.close()
        if n:
            sys.exit(
                f"{dsn['dbname']} already exists and holds {n} table(s), so "
                f"'DATABASE A - FRESH' would not be fresh.\n"
                f"Re-run with --recreate to drop and rebuild it.")


def build_fresh():
    """Build the schema through the app's own path, not a hand-copied
    approximation - the point is to test what init_db() actually produces.

    The pool is closed afterwards because a small Postgres (PGlite serves one
    client) cannot also give this script a connection while it is held."""
    import storage.database as sdb

    sdb.Database()
    if sdb._POOL is not None:
        sdb._POOL.closeall()
        sdb._POOL = None


def build_legacy(cur):
    """Recreate the pre-012 mae_mfe_data the live box actually has, with the
    contamination the 2026-07-25 snapshot contained."""
    cur.execute("DROP TABLE IF EXISTS mae_mfe_data CASCADE")
    cur.execute("""
        CREATE TABLE mae_mfe_data (
            id TEXT PRIMARY KEY,
            trade_id TEXT,
            ticker TEXT, setup_type TEXT, regime TEXT,
            mae_pct REAL, mfe_pct REAL, outcome_pct REAL,
            hold_hours REAL, recorded_at TEXT, data_quality TEXT
        )""")
    # Burn ids so the genuine position cannot collide with the literal '1' the
    # contaminated rows carry - on the live box the real rows were 10, 23, 31.
    cur.execute("INSERT INTO positions (ticker, status) "
                "SELECT 'FILLER','CLOSED' FROM generate_series(1,9)")
    cur.execute("INSERT INTO positions (ticker, status) VALUES ('REAL','CLOSED')")
    cur.execute("select id from positions order by id desc limit 1")
    live_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO mae_mfe_data (id, trade_id, ticker, mae_pct, mfe_pct) VALUES
            ('uuid-1', %s,            'BMY',  -2.1, 3.4),
            ('uuid-2', '1',           'AAA',   0.0, 0.0),
            ('uuid-3', '1',           'NVDA',  0.0, 0.0),
            ('uuid-4', '999999',      'ORPH',  0.0, 0.0),
            ('uuid-5', 'not-a-number','JUNK',  0.0, 0.0)
    """, (str(live_id),))
    return live_id


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recreate", action="store_true",
                    help="drop and rebuild the scratch database first")
    args = ap.parse_args()

    dsn = guard_target()
    import psycopg2

    print(BANNER)
    print(f"DATABASE A - FRESH, built by the app's own init_db()   [{dsn['dbname']}]")
    print(BANNER)
    ensure_database(dsn, args.recreate)
    build_fresh()
    conn = psycopg2.connect(**dsn)
    conn.autocommit = True
    cur = conn.cursor()

    s = shape(cur)
    print(f"  born as: id={s.get('id')}, trade_id={s.get('trade_id')}")
    for name in migrations(1, 12):
        ok, err = attempt(cur, strip_psql_meta(sql_of(name)))
        print(f"  {'OK ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       {err}")
            FAILURES.append(f"fresh/{name}")

    print()
    print("  --- assertions on the fresh shape ---")
    s = shape(cur)
    expect("trade_id is integer", s.get("trade_id"), "integer")
    expect("id is bigint", s.get("id"), "bigint")
    fks = fk_of(cur)
    expect("exactly one FK on trade_id", len(fks), 1)
    if fks:
        expect("FK delete rule is SET NULL", fks[0][1], "n")

    print()
    print("  --- 012 applied a SECOND time (idempotence) ---")
    ok, err = attempt(cur, strip_psql_meta(sql_of("012_mae_mfe_fk.sql")))
    expect("012 is re-runnable", ok, True)
    if not ok:
        print(f"       {err}")

    print()
    print(BANNER)
    print("DATABASE B - LEGACY, the shape the live box actually has")
    print(BANNER)
    live_id = build_legacy(cur)
    s = shape(cur)
    print(f"  built as: id={s.get('id')}, trade_id={s.get('trade_id')}")
    print(f"  seeded 5 rows: 1 real (trade_id={live_id}), 2 duplicates, "
          f"1 orphan, 1 non-numeric")

    print()
    print("  --- 009-012 against CONTAMINATED legacy data ---")
    legacy = {}
    for name in migrations(9, 12):
        ok, err = attempt(cur, strip_psql_meta(sql_of(name)))
        legacy[name] = ok
        print(f"  {'APPLIED' if ok else 'REFUSED'} {name}")
        if err:
            print(f"       {err[:170]}")
    expect("010 refuses on duplicate trade_id",
           legacy.get("010_mae_mfe_integrity.sql"), False)
    expect("012 refuses on contamination",
           legacy.get("012_mae_mfe_fk.sql"), False)

    print()
    print("  --- B3: purge the residue, then retry ---")
    # By ticker, the way repair_test_damage.py's TEST_TICKERS predicate does -
    # not by trade_id, which is the corrupted column and cannot identify
    # anything.
    cur.execute("DELETE FROM mae_mfe_data WHERE ticker IN ('AAA','NVDA','ORPH','JUNK')")
    cur.execute("select count(*) from mae_mfe_data")
    print(f"  purged; {cur.fetchone()[0]} row(s) remain")
    for name in migrations(9, 12):
        ok, err = attempt(cur, strip_psql_meta(sql_of(name)))
        print(f"  {'OK ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"       {err[:200]}")
            FAILURES.append(f"legacy-clean/{name}")

    print()
    print("  --- assertions after the legacy migration ---")
    s = shape(cur)
    expect("trade_id converted to integer", s.get("trade_id"), "integer")
    expect("id converted to bigint", s.get("id"), "bigint")
    fks = fk_of(cur)
    expect("exactly one FK on trade_id", len(fks), 1)
    if fks:
        expect("FK delete rule is SET NULL", fks[0][1], "n")
    cur.execute("select count(*) from mae_mfe_data where trade_id = %s", (live_id,))
    expect("the real row survived with its link intact", cur.fetchone()[0], 1)

    print()
    print("  --- ON DELETE SET NULL: the reset must not destroy history ---")
    cur.execute("DELETE FROM positions WHERE id = %s", (live_id,))
    cur.execute("select count(*), count(trade_id) from mae_mfe_data")
    rows, linked = cur.fetchone()
    expect("excursion row survives the position delete", rows, 1)
    expect("its trade_id became NULL rather than cascading", linked, 0)

    print()
    print(BANNER)
    if FAILURES:
        print(f"REHEARSAL FAILED - {len(FAILURES)} problem(s)")
        for f in FAILURES:
            print(f"  - {f}")
        print(BANNER)
        return 1
    print("REHEARSAL CLEAN - both shapes converge, guards fire, SET NULL holds")
    print(BANNER)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
