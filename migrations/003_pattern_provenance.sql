-- 003_pattern_provenance.sql  (Phase 1 step 1.4, §17)
--
-- The reason the contamination problem could arise at all is that a pattern
-- row carried no record of the code and configuration that produced it. When
-- the stop bug was fixed on 2026-07-20, nothing in the database distinguished
-- a pattern recorded before the fix from one recorded after - the only
-- available filter was a hand-remembered date, which is exactly the kind of
-- control that fails six months later.
--
-- With these two columns, future contamination becomes FILTERABLE instead of
-- fatal, and the Phase 4 recalibration (§19) gets its boundary for free: after
-- the scoring weights change, every pre-change pattern is automatically
-- distinguishable from every post-change one, without anyone having to
-- remember the date it happened.
--
-- 001 already added app_version to this table (the released build). This adds:
--   engine_version      - the rules/decision engine version specifically, which
--                         moves independently of the release when a hotfix
--                         touches nothing behavioural.
--   config_fingerprint  - a hash of every config value that can change a score
--                         or an exit. Two patterns with different fingerprints
--                         were produced by different strategies and must not be
--                         pooled, however close their timestamps are.
--
-- Purely additive, both nullable, no default. rollback_safe: true.
--
-- Apply:    ./scripts/apply_migration.sh migrations/003_pattern_provenance.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE pattern_database ADD COLUMN IF NOT EXISTS engine_version     TEXT;
ALTER TABLE pattern_database ADD COLUMN IF NOT EXISTS config_fingerprint TEXT;

-- Rows written before this migration were produced by an unrecorded
-- configuration. Say so explicitly rather than leaving NULL, which reads as
-- "not recorded yet" and would let a future pooling query treat them as
-- merely-missing rather than as known-incomparable.
UPDATE pattern_database
   SET config_fingerprint = 'pre-provenance'
 WHERE config_fingerprint IS NULL;

CREATE INDEX IF NOT EXISTS idx_pattern_config_fingerprint
    ON pattern_database (config_fingerprint);
CREATE INDEX IF NOT EXISTS idx_pattern_recorded_at
    ON pattern_database (recorded_at);

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_pattern_recorded_at;
-- DROP INDEX IF EXISTS idx_pattern_config_fingerprint;
-- ALTER TABLE pattern_database DROP COLUMN IF EXISTS config_fingerprint;
-- ALTER TABLE pattern_database DROP COLUMN IF EXISTS engine_version;
