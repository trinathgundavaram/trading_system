-- 001_app_version_stamps.sql  (Phase 0 step 0.6, §37 + §13)
--
-- Stamp the build onto every row that records a decision or its outcome.
-- Purely additive: every column is nullable with no default, so a previous
-- version reading this database sees columns it does not know about and
-- ignores them. rollback_safe: true.
--
-- Apply:    psql "$POSTGRES_DB" -f migrations/001_app_version_stamps.sql
-- Rollback: see the commented block at the bottom.

ALTER TABLE signals           ADD COLUMN IF NOT EXISTS app_version TEXT;
ALTER TABLE signals           ADD COLUMN IF NOT EXISTS ta_backend  TEXT;
ALTER TABLE paper_trades      ADD COLUMN IF NOT EXISTS app_version TEXT;
ALTER TABLE trades            ADD COLUMN IF NOT EXISTS app_version TEXT;
ALTER TABLE pattern_database  ADD COLUMN IF NOT EXISTS app_version TEXT;
ALTER TABLE cycles            ADD COLUMN IF NOT EXISTS app_version TEXT;

-- Rows written before this migration were produced by an unknown build. Say so
-- explicitly rather than leaving NULL, which reads as "not recorded yet".
UPDATE signals          SET app_version = 'pre-v1.0.0' WHERE app_version IS NULL;
UPDATE paper_trades     SET app_version = 'pre-v1.0.0' WHERE app_version IS NULL;
UPDATE trades           SET app_version = 'pre-v1.0.0' WHERE app_version IS NULL;
UPDATE pattern_database SET app_version = 'pre-v1.0.0' WHERE app_version IS NULL;
UPDATE cycles           SET app_version = 'pre-v1.0.0' WHERE app_version IS NULL;

CREATE INDEX IF NOT EXISTS idx_signals_app_version      ON signals (app_version);
CREATE INDEX IF NOT EXISTS idx_paper_trades_app_version ON paper_trades (app_version);

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_paper_trades_app_version;
-- DROP INDEX IF EXISTS idx_signals_app_version;
-- ALTER TABLE cycles           DROP COLUMN IF EXISTS app_version;
-- ALTER TABLE pattern_database DROP COLUMN IF EXISTS app_version;
-- ALTER TABLE trades           DROP COLUMN IF EXISTS app_version;
-- ALTER TABLE paper_trades     DROP COLUMN IF EXISTS app_version;
-- ALTER TABLE signals          DROP COLUMN IF EXISTS ta_backend;
-- ALTER TABLE signals          DROP COLUMN IF EXISTS app_version;
