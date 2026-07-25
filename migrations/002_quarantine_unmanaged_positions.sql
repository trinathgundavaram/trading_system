-- 002_quarantine_unmanaged_positions.sql  (Phase 1 step 1.2, §5)
--
-- Disarm the stop machinery sitting on positions this engine must never
-- close. The code changes in §5 stop NEW stops from being written to SYNC and
-- SEED rows (engine/position_management.py's Loop B now iterates
-- get_managed_positions), and three independent layers now refuse to exit
-- them - but the rows in the database TODAY already carry live exit state:
-- as of the 2026-07-24 audit, KMB sat in TREND_FOLLOWING, RVI in
-- PROFIT_PROTECT, and SMFL carried a stop exactly equal to its entry price
-- ($614.2501 / $614.2501). That is not dormant data. It is an armed exit
-- waiting for a switch to be flipped back on.
--
-- The old values are PRESERVED, not discarded: quarantined_stop_price and
-- quarantined_stop_state keep them for the audit trail and make the backward
-- migration exact. rollback_safe: true - a previous version reading this
-- database sees NULL stops on SYNC/SEED rows, which is precisely the
-- behaviour intended, and sees two extra columns it ignores.
--
-- Apply:    ./scripts/apply_migration.sh migrations/002_quarantine_unmanaged_positions.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE positions ADD COLUMN IF NOT EXISTS quarantined_stop_price REAL;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS quarantined_stop_state TEXT;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS quarantined_at         TEXT;

-- Preserve first. Only rows that actually carry exit state, and only once
-- (quarantined_at IS NULL), so re-running this file is a no-op rather than a
-- second pass that would copy the already-NULLed values over the originals.
UPDATE positions
   SET quarantined_stop_price = current_stop_price,
       quarantined_stop_state = stop_state,
       quarantined_at         = to_char(now() AT TIME ZONE 'UTC',
                                        'YYYY-MM-DD"T"HH24:MI:SS')
 WHERE status = 'open'
   AND COALESCE(UPPER(trade_mode), '') IN ('SYNC', 'SEED')
   AND quarantined_at IS NULL;

-- Then disarm.
UPDATE positions
   SET current_stop_price = NULL,
       stop_state         = NULL
 WHERE status = 'open'
   AND COALESCE(UPPER(trade_mode), '') IN ('SYNC', 'SEED');

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- UPDATE positions
--    SET current_stop_price = quarantined_stop_price,
--        stop_state         = quarantined_stop_state,
--        quarantined_at     = NULL
--  WHERE quarantined_at IS NOT NULL;
-- ALTER TABLE positions DROP COLUMN IF EXISTS quarantined_at;
-- ALTER TABLE positions DROP COLUMN IF EXISTS quarantined_stop_state;
-- ALTER TABLE positions DROP COLUMN IF EXISTS quarantined_stop_price;
