-- 012_mae_mfe_fk.sql  (§C1, 2026-07-25 second external review)
--
-- ⚠ RUN §49's PURGE AND migrations/010 FIRST. This migration WILL FAIL on
--   uncleaned data, and - as with 010 - that failure is the point.
--
-- WHAT 010 LEFT UNFINISHED
--
-- migrations/010 added the unique index on mae_mfe_data.trade_id, which was
-- the load-bearing half: it is what stops one excursion row attaching itself
-- to five different patterns. It deliberately did not touch the column TYPES,
-- and the second external review was right that the types are still wrong:
--
--     mae_mfe_data.id        TEXT PRIMARY KEY    <- uuid4 strings
--     mae_mfe_data.trade_id  TEXT                <- a stringified positions.id
--     positions.id           SERIAL              <- int4
--
-- So the join in get_pattern_excursions() has to say
-- `CAST(m.trade_id AS TEXT) = CAST(p.trade_id AS TEXT)`, and every comparison
-- against a real integer key is a string comparison that happens to agree.
-- Nothing enforces that trade_id names a position that exists, which is how
-- trade_id='1' came to be claimed by AAA, ADPT, FIX, MU and NVDA at once.
--
-- WHY THE FK WAS THE HARD HALF (and why it waited for a decision)
--
-- A foreign key needs an ON DELETE policy, and that policy is a statement
-- about what an excursion row MEANS - which nobody had made explicitly.
-- reset_paper_account() deletes every simulated position; §48 made it delete
-- the equity curve too. It deliberately does NOT delete mae_mfe_data, and the
-- docstring says so. So the two facts to reconcile are:
--
--   - positions rows are deleted routinely, by design, on reset.
--   - excursion rows are meant to SURVIVE that, also by design.
--
-- ON DELETE CASCADE would therefore be actively wrong: it would silently make
-- reset_paper_account() destroy the excursion history it explicitly promises
-- to keep, and nobody would notice until an MAE average came back thin.
--
-- ON DELETE SET NULL is the correct reading. The maximum adverse excursion of
-- a trade that happened is a fact about that trade, and it stays true after
-- the position row is gone. What is no longer true is that we can say WHICH
-- position it was - so trade_id becomes NULL, which is exactly what NULL
-- means, and get_pattern_excursions() already excludes NULL trade_id rows
-- (`AND p.trade_id IS NOT NULL`) rather than counting them as zero.
--
-- The one consequence to be aware of: after a reset, orphaned excursion rows
-- accumulate that can never be joined to a pattern again. They are still
-- readable via get_recent_mae_mfe() for the percentile comparison, which does
-- not need the link. If they ever need clearing, that is a
-- repair_test_damage.py predicate (`trade_id IS NULL AND recorded_at < ...`),
-- not a cascade.
--
-- NOT rollback_safe in the strict sense - see the BACKWARD block.
--
-- Apply:    ./scripts/apply_migration.sh migrations/012_mae_mfe_fk.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)

BEGIN;

-- ── 1. Refuse to run on dirty data ──────────────────────────────────────────
-- The type change below would otherwise be the thing that fails, with a cast
-- error naming a row rather than naming the problem. Fail on the actual
-- invariant instead, with a message that says what to do.
DO $$
DECLARE
    n_orphan  bigint;
    n_dupe    bigint;
    n_nonnum  bigint;
BEGIN
    SELECT COUNT(*) INTO n_nonnum
      FROM mae_mfe_data
     WHERE trade_id IS NOT NULL
       AND trade_id !~ '^[0-9]+$';
    IF n_nonnum > 0 THEN
        RAISE EXCEPTION
          'mae_mfe_data has % row(s) whose trade_id is not an integer string. '
          'Run scripts/assess_test_damage.py, then repair_test_damage.py --apply.',
          n_nonnum;
    END IF;

    SELECT COUNT(*) INTO n_orphan
      FROM mae_mfe_data m
     WHERE m.trade_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM positions p
                        WHERE CAST(p.id AS TEXT) = m.trade_id);
    IF n_orphan > 0 THEN
        RAISE EXCEPTION
          'mae_mfe_data has % row(s) whose trade_id names no position. '
          'These are the test residue §49 exists to purge - run '
          'scripts/repair_test_damage.py --apply before this migration.',
          n_orphan;
    END IF;

    SELECT COUNT(*) INTO n_dupe
      FROM (SELECT trade_id FROM mae_mfe_data
             WHERE trade_id IS NOT NULL
             GROUP BY trade_id HAVING COUNT(*) > 1) d;
    IF n_dupe > 0 THEN
        RAISE EXCEPTION
          'mae_mfe_data has % duplicated trade_id value(s). migrations/010 '
          'should already have refused for this reason - do not drop its '
          'unique index, finish the purge.',
          n_dupe;
    END IF;
END $$;

-- ── 2. trade_id: TEXT -> INTEGER ────────────────────────────────────────────
-- Safe now that step 1 has proven every non-NULL value is a numeric string
-- naming a live position. INTEGER, not BIGINT, to match positions.id (SERIAL
-- = int4) exactly - a FK between int8 and int4 works but costs an implicit
-- cast on every join, which is the class of thing this migration exists to
-- remove.
DROP INDEX IF EXISTS idx_mae_mfe_trade_id;

ALTER TABLE mae_mfe_data
    ALTER COLUMN trade_id TYPE INTEGER USING NULLIF(trade_id, '')::INTEGER;

ALTER TABLE mae_mfe_data
    ADD CONSTRAINT fk_mae_mfe_trade
    FOREIGN KEY (trade_id) REFERENCES positions (id)
    ON DELETE SET NULL;

-- Re-create 010's unique index against the new type. Still partial: many rows
-- may legitimately have NULL trade_id (pre-§51 rows, and post-reset orphans),
-- and NULLs are not equal to each other anyway - the partial predicate makes
-- that explicit rather than relying on the reader knowing it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mae_mfe_trade_id
    ON mae_mfe_data (trade_id)
    WHERE trade_id IS NOT NULL;

-- ── 3. id: TEXT (uuid4) -> BIGINT identity ──────────────────────────────────
-- Nothing references mae_mfe_data.id. Not a foreign key anywhere, not read by
-- get_recent_mae_mfe() (SELECT *), not read by get_pattern_excursions(), not
-- in any WHERE clause in the repository. It is a surrogate key that was being
-- generated as a uuid4 string by insert_mae_mfe() for no reason beyond the
-- table having been created that way.
--
-- So the uuids can simply be discarded rather than preserved. Ordering by
-- recorded_at keeps the new ids monotonic in the same direction the old rows
-- were written, which costs nothing and means a smaller id is an older row.
ALTER TABLE mae_mfe_data DROP CONSTRAINT IF EXISTS mae_mfe_data_pkey;
ALTER TABLE mae_mfe_data DROP COLUMN IF EXISTS id;

ALTER TABLE mae_mfe_data
    ADD COLUMN id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY;

COMMIT;

-- ── Verify ──────────────────────────────────────────────────────────────────
-- \d mae_mfe_data  should now show:
--     id        bigint   not null  generated by default as identity
--     trade_id  integer            REFERENCES positions(id) ON DELETE SET NULL
--   Indexes: "mae_mfe_data_pkey" PRIMARY KEY (id)
--            "idx_mae_mfe_trade_id" UNIQUE, (trade_id) WHERE trade_id IS NOT NULL
--   Foreign-key constraints: "fk_mae_mfe_trade"

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- BEGIN;
--   ALTER TABLE mae_mfe_data DROP CONSTRAINT IF EXISTS fk_mae_mfe_trade;
--   ALTER TABLE mae_mfe_data ALTER COLUMN trade_id TYPE TEXT USING trade_id::TEXT;
--   ALTER TABLE mae_mfe_data DROP COLUMN id;
--   ALTER TABLE mae_mfe_data ADD COLUMN id TEXT;
--   UPDATE mae_mfe_data SET id = gen_random_uuid()::TEXT;
--   ALTER TABLE mae_mfe_data ADD PRIMARY KEY (id);
-- COMMIT;
--
-- NOT a clean rollback, and this is the one migration in the series where that
-- is true. The original uuid4 values are gone - the BACKWARD block mints new
-- ones. That is harmless precisely BECAUSE nothing referenced them (see step 3
-- above); if that ever stops being true, this migration has to be revisited
-- before the assumption is relied on again.
--
-- Rolling back also reinstates the ability to write an orphaned trade_id, so
-- if you roll this back, re-read §49 before trusting excursion joins again.
