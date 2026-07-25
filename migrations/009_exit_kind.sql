-- 009_exit_kind.sql  (Phase 2.5 step 2.5.3, §50)
--
-- pattern_database.exit_reason is a human-readable sentence with the price
-- interpolated into it:
--
--     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $82.56 <= stop $83.15
--     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $22.13 <= stop $22.39
--     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $19.00 <= stop $20.62
--     paper_sell_rules:Dynamic stop hit (INITIAL_RISK): price $18.38 <= stop $18.44
--
-- Four stop-loss exits, four distinct strings. GROUP BY exit_reason returns one
-- row per trade, so the column cannot be counted, filtered or joined on - which
-- is why engine/ev_engine.py's HONESTY NOTE says p_stop_loss is a horizon proxy
-- and cannot be an actual P(stop): "no code path yet writes a genuine
-- stop_loss/take_profit exit_reason", and filtering on it "would silently
-- return zero matches, not a smaller sample".
--
-- exit_kind is the countable companion. A closed set (rules/common.py's
-- EXIT_KINDS), written BESIDE the sentence, not instead of it - the sentence is
-- what the UI shows and what analytics/regret_analysis.py narrates, and it is
-- good at that job. It was only ever bad at being two things at once.
--
-- Purely additive, nullable. rollback_safe: true.
--
-- Apply:    ./scripts/apply_migration.sh migrations/009_exit_kind.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE pattern_database ADD COLUMN IF NOT EXISTS exit_kind TEXT;

CREATE INDEX IF NOT EXISTS idx_pattern_exit_kind
    ON pattern_database (exit_kind)
    WHERE exit_kind IS NOT NULL;

-- ── No backfill, deliberately ───────────────────────────────────────────────
-- The existing closed rows would have to be reverse-engineered from the very
-- strings this column exists because nobody can group. Some are mechanical
-- ("paper_price_watch:stop_loss"), but "paper_sell_rules:Earnings in 0 days"
-- maps to a kind only by inference, and inference is exactly what a countable
-- column must not contain. A bucket half-filled by guesswork is worse than an
-- empty one, because it looks complete.
--
-- NULL reads as "not recorded", which is true. Consumers filter
-- `exit_kind IS NOT NULL` - the same posture §008 took for would_have_size and
-- §17 took for config_fingerprint='unstamped'.
--
-- Practically this means EV work keyed on exit_kind starts accumulating a
-- sample from the day this ships, and the pre-existing 23 closed patterns are
-- not part of it. Given §15's quarantine and learning.min_pattern_recorded_at
-- already exclude most of them from the live path, that costs less than it
-- sounds like.

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS idx_pattern_exit_kind;
-- ALTER TABLE pattern_database DROP COLUMN IF EXISTS exit_kind;
--
-- Safe: nothing reads exit_kind as of this migration. It is written by
-- close_pattern() and read by no decision path - lifting ev_engine's
-- p_stop_loss onto it is Phase 3 work and will be its own declared
-- decision-function change. Rolling back before then loses recorded data and
-- no behaviour.
