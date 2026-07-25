-- 010_mae_mfe_integrity.sql  (Phase 2.5 step 2.5.4, §51)
--
-- ⚠ RUN §49's PURGE FIRST. This migration WILL FAIL on uncleaned data, and
--   that failure is the point - see the guard note below.
--
-- WHAT WAS WRONG
--
-- mae_mfe_data.trade_id is TEXT holding a stringified positions.id, with no
-- unique constraint, no foreign key and no book scope. On the 2026-07-25
-- snapshot:
--
--     trade_id  rows  distinct tickers
--     '1'         15    5     <- AAA, ADPT, FIX, MU, NVDA
--     '2'          4    1     <- ORCL, against a position that is FLYW
--     '3'          3    1     <- MU,   against a position that is NU
--     '10','23','31'  1 each  <- BMY, USB, SHEL: the real ones
--
-- Twenty-two of twenty-five rows are test-suite residue carrying the exact
-- tickers scripts/assess_test_damage.py lists as TEST_TICKERS, with
-- mae_pct = mfe_pct = 0.0. The 2026-07-25 cleanup purged paper_trades,
-- positions and pattern_database; mae_mfe_data was not in the PURGE list, so
-- it walked past the excursion table entirely.
--
-- The consequence is worse than the contamination. Joining
-- pattern_database -> positions -> mae_mfe_data on trade_id returned 37 rows
-- for 23 closed patterns, and the surplus was not duplicate records of one
-- trade - NVDA's excursion row was attaching itself to ADPT's pattern. Phase 3
-- wants to replace ev_engine's horizon proxies with real MAE statistics; doing
-- that against this table would have produced numbers that were wrong in a way
-- nothing about the query looked wrong.
--
-- Purely additive (two indexes). rollback_safe: true.
--
-- Apply:    ./scripts/apply_migration.sh migrations/010_mae_mfe_integrity.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

-- One excursion record per trade. There is no sense in which a single closed
-- position has two different maximum adverse excursions.
--
-- THIS IS ALSO THE CHECK ON §49. If it fails with a duplicate-key error, the
-- purge did not run or did not finish, and the correct response is to go and
-- finish it - NOT to drop the constraint. A migration that refuses to apply is
-- the cheapest possible place to find out the data is still dirty.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mae_mfe_trade_id
    ON mae_mfe_data (trade_id)
    WHERE trade_id IS NOT NULL;

-- The direct pattern -> excursion hop that §51's link_pattern_to_trade()
-- populates, replacing the transitive route through positions.pattern_id.
CREATE INDEX IF NOT EXISTS idx_pattern_trade_id
    ON pattern_database (trade_id)
    WHERE trade_id IS NOT NULL;

-- ── No backfill of pattern_database.trade_id ────────────────────────────────
-- It could be derived for existing rows - positions.pattern_id points back, so
-- an UPDATE ... FROM would fill most of them in. It is deliberately not done
-- here, for the same reason 008 refused to invent would_have_size: the rows
-- that would benefit are precisely the ones whose excursion data is the
-- contaminated set above. Backfilling the link would connect clean patterns to
-- dirty excursions and make the result look derived.
--
-- Trades opened from §51 onward carry the link natively. Excursion analysis
-- starts from a clean epoch, and the pre-§51 rows report as absent rather than
-- as zero.

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_pattern_trade_id;
-- DROP INDEX IF EXISTS idx_mae_mfe_trade_id;
--
-- Dropping the unique index does not make the duplicate rows safe again; it
-- makes them silent again. get_pattern_excursions() keeps a redundant
-- in-Python dedupe specifically so that a database restored without these
-- indexes degrades to a warning rather than to wrong averages.
