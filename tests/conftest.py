"""Test isolation: one ephemeral Postgres per session, one clean schema per test.

§12 (Phase 2 step 2.1). Replaces the emergency guard added in v1.0.1 with a
real harness, so the four money-touching test modules can run again instead of
merely being prevented from doing harm.

WHY THIS EXISTS
---------------
`storage/database.py`'s `__init__` comment records a 2026-07-20 incident in
which a "supposedly-isolated" test wrote a live buy and sell into production.
The fix then was to stop binding `DB_PATH` as a default argument.

The 2026-07-21 Postgres migration silently un-fixed it. `path` became a dead
parameter - stored on `self.path`, never used to open anything. Every
`Database()` connects to `PG_DB`, which defaults to `trading_platform`. Four
test modules still passed `Database(path=tmp_path / "...")` believing that
isolated them.

On 2026-07-25 a `./scripts/release.sh patch` run executed the suite against the
live database: it inserted 35 positions, called `reset_paper_account()` (which
DELETEs the whole purse, the whole `paper_trades` ledger, and every
`simulated=1` position), closed real positions, and polluted `daily_stats`,
`rotation_log`, `trades` and `pattern_database`. The paper ledger was
unrecoverable from Postgres; 60 of its rows came back from the pre-migration
SQLite snapshot.

The reason it went unnoticed for four days is worth remembering: with no
Postgres listening, these tests raise `OperationalError` at fixture setup and
read as "33 environmental errors, tracked as E-2". The dangerous behaviour is
invisible exactly where it is harmless.

DESIGN
------
Session-scoped SERVER: starting Postgres costs a couple of seconds, and paying
that per test would make the suite unusable, which is how suites stop being run.

Function-scoped SCHEMA: tests must not see each other's rows. The whole public
schema is dropped and recreated after each test, which is faster than
per-test-database creation and leaves no cross-test residue.

`database._POOL = None` on BOTH sides of the fixture. `_get_pool()` memoises
the pool at module scope; without resetting it, the second test silently reuses
the first test's connection parameters - a failure that presents as flakiness
and costs a day to find.

NO SQLITE FALLBACK, deliberately. Testing against a different engine than you
run is what produced the original divergence.

TWO WAYS TO GET A SERVER
------------------------
1. `pytest-postgresql`, which starts a genuinely throwaway instance. Best
   isolation. Requires the plugin AND libpq (it pulls psycopg3, a second
   driver alongside the psycopg2 this codebase uses).
2. A scratch DATABASE on the Postgres you already run locally. No new system
   dependency, no second driver, and it works on the machine as it is today.

The harness prefers (1) and falls back to (2). That is a deliberate departure
from §12, which names pytest-postgresql only. The lesson of 2026-07-25 is that
a suite which does not actually run provides no protection whatsoever, and a
harness with fewer moving parts is more likely to be run. The safety property
is identical under both: the database name is never the production one, and
`pytest_configure` enforces that before a connection can be opened.
"""
import os
import tempfile

import pytest

# storage/database.py's own default. Kept as a literal so this guard does not
# need to import the module it is protecting you from.
PRODUCTION_DB_DEFAULT = "trading_platform"
EPHEMERAL_DB_NAME = "tests"

# Fallback mode: a scratch database on the local server. Overridable so CI can
# point somewhere else.
SCRATCH_DB_NAME = os.getenv("TP_TEST_POSTGRES_DB", "trading_platform_test")

try:
    from pytest_postgresql import factories
    postgresql_proc = factories.postgresql_proc(port=None)
    _HAVE_PYTEST_POSTGRESQL = True
except Exception:  # pragma: no cover - depends on the machine, not the code
    _HAVE_PYTEST_POSTGRESQL = False


# ── The production guard ────────────────────────────────────────────────────
# Runs before any fixture, any import of storage.database, any connection.

def pytest_configure(config):
    """Make it impossible for this session to resolve to the real database.

    The subtlety that mattered on 2026-07-25: POSTGRES_DB is NOT set in this
    project's .env, so a naive `if os.getenv('POSTGRES_DB') == 'trading_platform'`
    check passes cleanly while `Database()` still opens production via the code
    default. The guard must therefore set the variable, not just inspect it.
    """
    target = EPHEMERAL_DB_NAME if _HAVE_PYTEST_POSTGRESQL else SCRATCH_DB_NAME
    if target == PRODUCTION_DB_DEFAULT:
        raise pytest.UsageError(
            f"The test database name resolves to {target!r}, which is production. "
            f"Set TP_TEST_POSTGRES_DB to something else.")

    ambient = os.getenv("POSTGRES_DB")
    if ambient not in (None, "", PRODUCTION_DB_DEFAULT, target):
        raise pytest.UsageError(
            f"POSTGRES_DB={ambient!r} is neither the test database ({target!r}) "
            f"nor unset. Refusing to run - see tests/conftest.py.")

    # Unset RESOLVES to production via storage/database.py's default, so the
    # guard must SET the variable, not merely inspect it. That distinction is
    # the whole of the 2026-07-25 incident.
    os.environ["POSTGRES_DB"] = target
    config._tp_target_db = target
    config._tp_db_note = (
        f"POSTGRES_DB pinned to {target!r} via "
        + ("pytest-postgresql (ephemeral server)" if _HAVE_PYTEST_POSTGRESQL
           else "a scratch database on the local server"))

    # ── The same isolation, for output (2026-07-26) ──────────────────────────
    # The database guard above is thorough and the output directory had none,
    # so every test run wrote its log lines into output/logs/scheduler.log -
    # the PRODUCTION log, interleaved with the real scheduler's. 234 such lines
    # had accumulated by the time anyone noticed.
    #
    # It is not merely untidy. That file is the audit trail you read after an
    # incident, and it now contains fixture trades on synthetic tickers with
    # round numbers, formatted identically to real ones. On 2026-07-26 it
    # produced a concrete wrong conclusion: two fixture lines - `ASTS ... purse
    # now $400.00` and `NEW ... purse now $1010.00` - were read as consecutive
    # real trades and offered as evidence of a purse re-seed, during the
    # investigation of a kill-switch trip. The reading was wrong. A log that
    # cannot be trusted during an incident is worse than no log, because it is
    # consulted precisely when care is scarcest.
    #
    # storage/paths.py routes every output path through TP_OUTPUT_DIR, so
    # pinning it here moves logs, backtest results and prompt files together.
    log_root = tempfile.mkdtemp(prefix="tp-test-output-")
    os.environ["TP_OUTPUT_DIR"] = log_root
    config._tp_output_dir = log_root


def pytest_report_header(config):
    """State the database target on every run, at the top.

    Not decoration. The 2026-07-25 failure was invisible because nothing ever
    answered 'which database am I about to write to'."""
    return (f"trading_platform: {getattr(config, '_tp_db_note', 'target unknown')}\n"
            f"trading_platform: output pinned to "
            f"{getattr(config, '_tp_output_dir', 'UNPINNED - logs go to production')}")


@pytest.fixture(autouse=True)
def _never_touch_production(pytestconfig):
    """Belt and braces, per test. `pytest_configure` pins the variable once;
    this catches a test that reassigns it mid-session."""
    resolved = os.getenv("POSTGRES_DB") or PRODUCTION_DB_DEFAULT
    expected = getattr(pytestconfig, "_tp_target_db", None)
    if resolved == PRODUCTION_DB_DEFAULT or (expected and resolved != expected):
        pytest.fail(f"Refusing to run: POSTGRES_DB resolves to {resolved!r}, "
                    f"expected {expected!r}.")
    yield


# ── The server ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def pg_dsn(request):
    """Connection parameters for whichever server this machine can provide.

    Skips - loudly, naming the remedy - rather than silently passing. A
    database test that quietly does not run is how a suite comes to be trusted
    for coverage it does not have, which is the failure this whole file exists
    to prevent.
    """
    if _HAVE_PYTEST_POSTGRESQL:
        proc = request.getfixturevalue("postgresql_proc")
        return {"host": proc.host, "port": proc.port, "user": proc.user,
                "dbname": EPHEMERAL_DB_NAME}

    # Fallback: a scratch database on the already-running local server.
    try:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    except Exception as e:
        pytest.skip(f"no Postgres driver available ({e})")

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER") or os.getenv("USER") or "postgres"

    try:
        admin = psycopg2.connect(host=host, port=port, user=user, dbname="postgres")
    except Exception as e:
        pytest.skip(
            f"no local Postgres to build a scratch database on ({e}).\n"
            f"    Either start Postgres, or install the ephemeral harness:\n"
            f"        pip install 'pytest-postgresql>=6'")
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (SCRATCH_DB_NAME,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{SCRATCH_DB_NAME}"')
    admin.close()

    return {"host": host, "port": port, "user": user, "dbname": SCRATCH_DB_NAME}


@pytest.fixture
def db(pg_dsn, monkeypatch):
    """A Database on a clean schema. The only sanctioned way to get one."""
    import storage.database as database

    monkeypatch.setenv("POSTGRES_HOST", str(pg_dsn["host"]))
    monkeypatch.setenv("POSTGRES_PORT", str(pg_dsn["port"]))
    monkeypatch.setenv("POSTGRES_USER", str(pg_dsn["user"]))
    monkeypatch.setenv("POSTGRES_DB", str(pg_dsn["dbname"]))
    monkeypatch.setenv("POSTGRES_PASSWORD", "")

    # The module read these at import time; the pool caches them again.
    monkeypatch.setattr(database, "PG_HOST", pg_dsn["host"], raising=False)
    monkeypatch.setattr(database, "PG_PORT", int(pg_dsn["port"]), raising=False)
    monkeypatch.setattr(database, "PG_USER", pg_dsn["user"], raising=False)
    monkeypatch.setattr(database, "PG_DB", pg_dsn["dbname"], raising=False)
    monkeypatch.setattr(database, "PG_PASSWORD", "", raising=False)

    database._POOL = None          # see module docstring - both sides matter
    d = database.Database()        # __init__ calls init_db(), building the schema
    try:
        yield d
    finally:
        try:
            with d._conn() as conn:
                conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        except Exception:
            pass
        database._POOL = None
