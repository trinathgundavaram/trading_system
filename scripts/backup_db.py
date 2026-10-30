#!/usr/bin/env python3
"""Nightly Postgres dump of the trading database (the paper ledgers and the forward-test history live only there).

    python3 scripts/backup_db.py                 # -> output/backups/trading_platform_YYYYmmdd_HHMM.dump
    TP_BACKUP_DIR=~/Backups python3 scripts/backup_db.py

Keeps the newest KEEP dumps. Restore with:  pg_restore -c -d trading_platform <file>
A local copy protects against a corrupted DB, not a dead disk - point TP_BACKUP_DIR at a synced or external folder
for that."""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEEP = 14


def backup_dir() -> Path:
    d = os.getenv("TP_BACKUP_DIR")
    return Path(d).expanduser() if d else Path(__file__).resolve().parent.parent / "output" / "backups"


def run_backup(keep: int = KEEP) -> Path:
    from storage import database as dbm
    out_dir = backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{dbm.PG_DB}_{datetime.now():%Y%m%d_%H%M}.dump"
    env = dict(os.environ)
    if dbm.PG_PASSWORD:
        env["PGPASSWORD"] = dbm.PG_PASSWORD
    subprocess.run(["pg_dump", "-Fc", "-h", dbm.PG_HOST, "-p", str(dbm.PG_PORT), "-U", dbm.PG_USER, "-f", str(out), dbm.PG_DB],
                   check=True, env=env, timeout=600, capture_output=True)
    if out.stat().st_size < 1024:
        raise RuntimeError(f"backup {out} is suspiciously small ({out.stat().st_size} bytes)")
    for old in sorted(out_dir.glob(f"{dbm.PG_DB}_*.dump"))[:-keep]:
        old.unlink()
    return out


if __name__ == "__main__":
    p = run_backup()
    print(f"backup written: {p} ({p.stat().st_size / 1e6:.1f} MB)")
