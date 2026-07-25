#!/usr/bin/env python3
"""One-time data migration: copy every row from the legacy SQLite
output/trading.db into the new Postgres database (2026-07-21 migration - see
prod_readiness_plan.md for why: hours-long OS-level file-open stalls on the
single SQLite file, root-caused via a live py-spy dump to every worker
thread blocking inside a bare sqlite3.connect() with zero exceptions raised).

Safe to re-run: each table is TRUNCATEd in Postgres before being reloaded
from SQLite, so running this twice re-syncs rather than duplicating rows.
Never touches or deletes trading.db - the SQLite file is left exactly as-is,
so it remains a full rollback point until you're confident in the Postgres
copy.

Usage:
    python3 migrate_to_postgres.py                  # migrate output/trading.db -> Postgres
    python3 migrate_to_postgres.py --source /path/to/other.db
    python3 migrate_to_postgres.py --dry-run         # just report row counts, write nothing

Prerequisites: Postgres running locally, a `trading_platform` database
created (`createdb trading_platform`), and POSTGRES_* env vars set in .env
if your setup differs from the defaults (see .env.template).
"""
import argparse
import sqlite3
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=None,
                         help="Path to the SQLite trading.db (default: output/trading.db)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Only report row counts on both sides, write nothing to Postgres")
    args = parser.parse_args()

    # Imported here (not top-level) so --dry-run's SQLite-only path doesn't
    # require Postgres to even be reachable yet.
    from storage.database import DB_PATH, Database, _get_pool

    source_path = Path(args.source) if args.source else DB_PATH
    if not source_path.exists():
        print(f"ERROR: source SQLite file not found: {source_path}")
        sys.exit(1)

    print(f"Source (SQLite): {source_path}")
    sconn = sqlite3.connect(str(source_path))
    sconn.row_factory = sqlite3.Row

    # Table list straight from SQLite's own catalog - whatever tables
    # actually exist in THIS trading.db, not a hardcoded list that could
    # drift from the real schema over time.
    tables = [r[0] for r in sconn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    print(f"Found {len(tables)} tables in source.")

    if args.dry_run:
        total = 0
        for t in tables:
            n = sconn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            total += n
            print(f"  {t}: {n} rows")
        print(f"\nDry run - {total} rows total, nothing written to Postgres.")
        sconn.close()
        return

    # Constructing Database() runs init_db(), which creates every
    # table/index/column via the SAME SCHEMA constant the live app uses -
    # there's no separate "Postgres schema" to hand-maintain in parallel.
    print("Ensuring Postgres schema exists (via Database().init_db())...")
    db = Database()

    pool = _get_pool()
    pg_conn = pool.getconn()
    pg_cur = pg_conn.cursor()

    def ensure_table_and_columns(t: str, sqlite_cols: list[str]):
        """2026-07-21 (found during a real cutover): storage/database.py has
        several tables/columns created LAZILY inside specific business-logic
        methods (e.g. screener_candidates.last_source/last_decomposition,
        added only the first time upsert_screener_candidate() runs; also
        universe/source_health/estimate_snapshots as whole tables) rather
        than in init_db()'s fixed sequence - a live SQLite db that's been
        running for days has all of these already; a freshly-created
        Postgres schema (from just calling Database() once) does not. Rather
        than hand-enumerating every such call site, reconcile generically:
        create the table from SQLite's own DDL if Postgres doesn't have it
        yet, then add any column SQLite has that Postgres doesn't, using
        SQLite's own declared type (TEXT/INTEGER/REAL are all valid native
        Postgres type names too, so no translation needed)."""
        pg_cur.execute("SELECT to_regclass(%s)", (t,))
        if pg_cur.fetchone()[0] is None:
            ddl = sconn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]
            ddl = ddl.replace("AUTOINCREMENT", "")  # PK alone is enough for these ad-hoc tables
            pg_cur.execute(ddl)
            pg_conn.commit()
            print(f"    (created missing table {t} from SQLite's own DDL)")

        pg_cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,)
        )
        pg_cols = {r[0] for r in pg_cur.fetchall()}
        for row in sconn.execute(f"PRAGMA table_info({t})").fetchall():
            name, coltype = row[1], row[2]
            if name not in pg_cols:
                pg_cur.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {name} {coltype or 'TEXT'}")
                print(f"    (added missing column {t}.{name} {coltype or 'TEXT'})")
        pg_conn.commit()

    per_table_counts = {}
    total_rows = 0
    try:
        for t in tables:
            cols = [d[0] for d in sconn.execute(f"SELECT * FROM {t} LIMIT 0").description]
            rows = sconn.execute(f"SELECT * FROM {t}").fetchall()
            n = len(rows)
            per_table_counts[t] = n
            if n == 0:
                print(f"  {t}: 0 rows, skipping")
                continue

            ensure_table_and_columns(t, cols)

            # Re-runnable: clear the Postgres table before reloading so a
            # second run re-syncs instead of duplicating.
            pg_cur.execute(f"TRUNCATE TABLE {t} CASCADE")

            col_list = ", ".join(cols)
            placeholders = ", ".join(["%s"] * len(cols))
            insert_sql = f"INSERT INTO {t} ({col_list}) VALUES ({placeholders})"
            values = [tuple(row) for row in rows]
            # Chunked rather than one giant executemany() - keeps peak memory
            # bounded and gives visible progress on the biggest tables
            # (news_items/universe run into the thousands of rows).
            CHUNK = 500
            for i in range(0, len(values), CHUNK):
                pg_cur.executemany(insert_sql, values[i:i + CHUNK])

            # Advance the SERIAL sequence past the highest migrated id (if
            # this table has one) - otherwise the next natural INSERT after
            # cutover would collide with a migrated row's id. Tables whose
            # 'id' is a plain PK (not SERIAL - the singleton CHECK(id=1)
            # tables, or 1:1 tables keyed on a foreign id) have no sequence
            # to advance; pg_get_serial_sequence returns NULL for those and
            # we just skip.
            if "id" in cols:
                pg_cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (t,))
                seq = pg_cur.fetchone()[0]
                if seq:
                    pg_cur.execute(f"SELECT setval(%s, (SELECT MAX(id) FROM {t}))", (seq,))

            print(f"  {t}: migrated {n} rows")
            total_rows += n
        pg_conn.commit()
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        pool.putconn(pg_conn)

    print(f"\nDone - {total_rows} total rows migrated across {len(tables)} tables.")
    print("Verifying row counts...")
    all_ok = True
    with db._conn() as vconn:
        for t, expected in per_table_counts.items():
            actual = vconn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            ok = actual == expected
            all_ok = all_ok and ok
            print(f"  {t}: sqlite={expected} postgres={actual} [{'OK' if ok else 'MISMATCH'}]")

    sconn.close()
    if all_ok:
        print("\nAll row counts match. Postgres copy is ready.")
    else:
        print("\nSOME ROW COUNTS DO NOT MATCH - investigate before cutting the app over to Postgres.")
        sys.exit(1)


if __name__ == "__main__":
    main()
