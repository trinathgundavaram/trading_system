"""Test isolation guard. Nothing in tests/ may touch the production database.

WHY THIS EXISTS (2026-07-24, second occurrence)
------------------------------------------------
`storage/database.py`'s own `__init__` comment records a 2026-07-20 incident in
which a "supposedly-isolated" integration test wrote a live TEST buy and sell
into the production database, including a real hit to `paper_account`'s
realized P&L. The fix at the time was to stop binding `DB_PATH` as a default
argument, so `Database(path=...)` would honour the path it was given.

The 2026-07-21 SQLite -> Postgres migration silently un-fixed it. `path` is now
a dead parameter: it is stored on `self.path` and never used to open anything.
Every `Database()`, with or without a path, connects to `PG_DB` - which
defaults to `trading_platform`. Four test modules
(test_paper_trading, test_rotation, test_live_trader, test_account_sync) still
construct `Database(path=tmp_path / "...")` believing that isolates them.

It does not. On 2026-07-24 a `./scripts/release.sh patch` run executed the
suite against the live database. It inserted test positions, called
`reset_paper_account()` (which wipes the paper purse and the entire simulated
book), closed real positions, and wrote to `daily_stats`, `rotation_log`,
`trades` and `pattern_database`. The failures looked like ordinary red tests;
they were a data-corruption event in progress.

The reason this went unnoticed for three days is instructive: on a machine with
no Postgres listening, these same tests raise `OperationalError` at fixture
setup and read as "33 environmental errors, tracked as E-2". The dangerous
behaviour is invisible precisely where it is harmless, and silent where it is
not.

WHAT THIS DOES
--------------
Refuses to let the session start if the resolved Postgres database is not an
explicitly-nominated scratch database. It FAILS rather than skips: a skip is
how you end up believing you have test coverage that never ran.

    createdb trading_platform_test
    TP_TEST_POSTGRES_DB=trading_platform_test python3 -m pytest tests/ -q

Tests that need no database at all (test_sync_quarantine, test_live_arm_gate,
test_ui_auth, test_learning_freeze, test_scoring_sanity) run either way - the
guard only rewrites the connection target, it does not gate collection.

This is a stopgap. §12 (Phase 2 step 2.1) replaces it with a real per-test
database fixture and restores the four modules above. Until then, this file is
the only thing standing between `pytest` and your open positions.
"""
import os

import pytest

# Matches storage/database.py's own default. Kept as a literal so this guard
# does not need to import the module it is protecting you from.
PRODUCTION_DB_DEFAULT = "trading_platform"


def _resolved_production_db() -> str:
    return os.getenv("POSTGRES_DB") or PRODUCTION_DB_DEFAULT


def pytest_configure(config):
    """Runs before any fixture, any import of storage.database, any connection.

    Sets POSTGRES_DB for the whole session so that even a `Database()` built
    with a bogus `path=` lands on the scratch database.
    """
    scratch = os.getenv("TP_TEST_POSTGRES_DB")
    production = _resolved_production_db()

    if not scratch:
        # No scratch database nominated. Point the session at a name that
        # cannot exist by accident, so anything that does try to connect fails
        # loudly and locally instead of succeeding against real data.
        os.environ["POSTGRES_DB"] = "tp_tests_no_scratch_db_configured"
        config._tp_db_note = (
            "no TP_TEST_POSTGRES_DB set - database-backed tests will fail to "
            "connect BY DESIGN. Create one with:\n"
            "    createdb trading_platform_test\n"
            "    TP_TEST_POSTGRES_DB=trading_platform_test python3 -m pytest tests/ -q"
        )
        return

    if scratch == production:
        raise pytest.UsageError(
            f"TP_TEST_POSTGRES_DB={scratch!r} is the production database. "
            f"Refusing to run the suite against it - see tests/conftest.py."
        )
    if scratch == PRODUCTION_DB_DEFAULT:
        raise pytest.UsageError(
            f"TP_TEST_POSTGRES_DB={scratch!r} is storage/database.py's production "
            f"default. Refusing to run the suite against it."
        )

    os.environ["POSTGRES_DB"] = scratch
    config._tp_db_note = f"database-backed tests are using scratch database {scratch!r}"


def pytest_report_header(config):
    """Say which database this run is pointed at, every run, at the top.

    Not decoration. The failure this file exists to prevent was invisible
    because nothing ever stated the answer to 'which database am I about to
    write to'."""
    return f"trading_platform: {getattr(config, '_tp_db_note', 'database target unknown')}"
