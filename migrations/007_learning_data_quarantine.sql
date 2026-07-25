-- 007_learning_data_quarantine.sql  (Phase 2 step 2.7, §15)
--
-- mae_mfe_data - a table the learning loop reads - contains rows that are not
-- trades. NVDA at +6.67% held for 0.0000034 hours (12 milliseconds). MU at
-- +10.00% held for 10ms. A ticker literally named "AAA". All three with MAE
-- and MFE of exactly 0.0.
--
-- storage/database.py's own commentary records the likely origin: an
-- integration test that was meant to be isolated "wrote a real TEST buy+sell
-- into production, including a real (if small) hit to paper_account's realized
-- P&L". The §12 conftest guard prevents recurrence. This file cleans up what
-- is already there.
--
-- QUARANTINE, NOT DELETE. Deleting destroys the evidence of how the
-- contamination happened. Marking keeps the forensics and still protects the
-- learner, because every read filters on data_quality='ok'
-- (query_mae_winners, get_recent_mae_mfe, get_patterns). Callers that WANT the
-- quarantined rows pass include_quarantined=True.
--
-- rollback_safe: true - the column is additive and the marks are advisory.
-- Note though that rolling back the CODE without rolling back this file is
-- harmless (nothing reads the column), while rolling back this file without
-- the code is not: the reads would filter on a column that does not exist.
--
-- Apply:    ./scripts/apply_migration.sh migrations/007_learning_data_quarantine.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE mae_mfe_data     ADD COLUMN IF NOT EXISTS data_quality TEXT DEFAULT 'ok';
ALTER TABLE pattern_database ADD COLUMN IF NOT EXISTS data_quality TEXT DEFAULT 'ok';

-- ── 1. Physically impossible hold times ─────────────────────────────────────
-- Nothing real closes in milliseconds. 0.01h = 36 seconds, which is already
-- generous for a system whose fastest configured cycle is minutes.
UPDATE mae_mfe_data SET data_quality = 'synthetic'
 WHERE hold_hours IS NOT NULL AND hold_hours < 0.01;

-- ── 2. Arithmetically impossible excursions ─────────────────────────────────
-- A trade that MOVED cannot have had zero MAE and zero MFE: the outcome is
-- itself an excursion. This catches fabricated rows that chose a plausible
-- hold time.
UPDATE mae_mfe_data SET data_quality = 'synthetic'
 WHERE COALESCE(mae_pct, 0) = 0
   AND COALESCE(mfe_pct, 0) = 0
   AND COALESCE(outcome_pct, 0) <> 0;

-- ── 3. Orphans ──────────────────────────────────────────────────────────────
-- A ticker with no corresponding position row in EITHER book was never held.
-- Applied only to rows still marked 'ok', so a row already identified as
-- synthetic keeps the more specific label - "how did this get here" is a
-- better answer than "it does not join".
UPDATE mae_mfe_data m SET data_quality = 'orphan'
 WHERE COALESCE(m.data_quality, 'ok') = 'ok'
   AND NOT EXISTS (SELECT 1 FROM positions p WHERE p.ticker = m.ticker);

-- ── 4. Everything taken under the stop bug removed on 2026-07-20 (T-4) ──────
-- Not fabricated - these are real trades. But they were produced by a system
-- that no longer exists, so they must not train the model that replaces it.
-- Marked LAST and only over rows still 'ok', so the sharper diagnoses above
-- survive.
UPDATE pattern_database SET data_quality = 'pre_stop_fix'
 WHERE COALESCE(data_quality, 'ok') = 'ok'
   AND recorded_at < '2026-07-20T00:00:00';
UPDATE mae_mfe_data SET data_quality = 'pre_stop_fix'
 WHERE COALESCE(data_quality, 'ok') = 'ok'
   AND recorded_at < '2026-07-20T00:00:00';

-- ── What to expect ──────────────────────────────────────────────────────────
-- Roughly zero rows remain 'ok'. That is the honest position: there is no
-- clean learning data yet. It is not a reason to loosen a rule above - the
-- Bayesian gate is already held shut at 150 trades by §17, and it should
-- reach that number on post-fix trades rather than on a sample topped up with
-- rows that were only ever going to teach the model the old bug.
--
-- Confirm with:
--     SELECT COALESCE(data_quality,'ok') AS q, COUNT(*)
--       FROM mae_mfe_data GROUP BY 1 ORDER BY 2 DESC;
--     SELECT COALESCE(data_quality,'ok') AS q, COUNT(*)
--       FROM pattern_database GROUP BY 1 ORDER BY 2 DESC;
--
-- Then run scripts/reconcile.py, which fails loudly on the cross-table
-- disagreements this quarantine does NOT fix (the ADPT case: the same trade
-- recorded as -1.88% over 6.34h in paper_trades and -3.20% over 5.0h in
-- mae_mfe_data). Those were caused by computing the same quantity twice;
-- close_position() now returns hold_hours and is the single definition.

CREATE INDEX IF NOT EXISTS idx_mae_mfe_data_quality
    ON mae_mfe_data (data_quality);
CREATE INDEX IF NOT EXISTS idx_pattern_data_quality
    ON pattern_database (data_quality);

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_pattern_data_quality;
-- DROP INDEX IF EXISTS idx_mae_mfe_data_quality;
-- ALTER TABLE pattern_database DROP COLUMN IF EXISTS data_quality;
-- ALTER TABLE mae_mfe_data     DROP COLUMN IF EXISTS data_quality;
--
-- Dropping the columns un-quarantines every row at once, silently, and the
-- reads fall back to COALESCE(...,'ok') = 'ok' matching everything. If you
-- roll this back, roll back the code with it.
