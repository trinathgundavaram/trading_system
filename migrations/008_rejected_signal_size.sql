-- 008_rejected_signal_size.sql  (Phase 2 step 2.9, §18)
--
-- portfolio_risk_log holds 244 rows recording evaluations. rejected_signals
-- held 0. There is a complete record of every trade the system took and none
-- at all of any trade it declined, which makes false negatives unmeasurable:
-- you can audit the trades you took but not the ones you skipped.
--
-- §18 starts writing a row per rejection from scheduler.py (portfolio_risk,
-- veto and threshold stages). This adds the one column that makes such a row
-- a counterfactual rather than a note - without the dollar amount the trade
-- would have taken, "what did skipping this cost?" has no denominator.
--
-- This is the dataset analytics/missed_opportunity.py and the regret modules
-- were built to consume and have never been given: missed_opportunity_outcomes
-- is empty.
--
-- Purely additive, nullable. rollback_safe: true.
--
-- Apply:    ./scripts/apply_migration.sh migrations/008_rejected_signal_size.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE rejected_signals ADD COLUMN IF NOT EXISTS would_have_size REAL;

-- No backfill. The existing rows predate the column and there is no honest way
-- to reconstruct what size they would have been given - position sizing
-- depends on conviction, risk and crowding as they were at that moment, none
-- of which was recorded. Leaving them NULL says "not known", which is true.
-- Inventing a flat trade_size_usd for them would make the counterfactual look
-- computable when it is not, and analytics would silently average real figures
-- together with fabricated ones.

CREATE INDEX IF NOT EXISTS idx_rejected_signals_stage
    ON rejected_signals (reject_stage);

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_rejected_signals_stage;
-- ALTER TABLE rejected_signals DROP COLUMN IF EXISTS would_have_size;
