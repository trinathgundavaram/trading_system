-- 005_drawdown_columns.sql  (Phase 2 step 2.5, §11)
--
-- daily_stats.max_drawdown has been declared since the schema was created,
-- defaulted to 0, read by the UI, and written by nothing. Drawdown is the
-- single most useful risk statistic this platform was not collecting:
-- expectancy tells you whether an edge exists, drawdown tells you whether you
-- can survive long enough to realise it.
--
-- Three new columns, and the book separation is the same call as 004. The only
-- equity curve that exists today is paper_equity_history, so every number this
-- migration can compute is a PAPER number. Writing it into `max_drawdown` -
-- which sits beside `realized_pnl`, a column audited as real-money-only -
-- would quietly redefine a live-ledger field under its existing readers.
-- `max_drawdown` and the new `running_drawdown` therefore stay empty and
-- honest until a live equity curve exists to fill them.
--
-- Purely additive, all defaulted. rollback_safe: true.
--
-- Apply:    psql "$POSTGRES_DB" -f migrations/005_drawdown_columns.sql
-- Rollback: see the commented block at the bottom.

ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS running_drawdown        REAL DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS paper_max_drawdown      REAL DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS paper_running_drawdown  REAL DEFAULT 0;

-- ── Backfill ────────────────────────────────────────────────────────────────
-- The equity curve already holds real history, so the metric starts with a
-- past instead of starting blank. That is what makes the caps in config.yaml
-- settable from evidence rather than guessed.
--
-- Run the Python backfill instead of hand-rolling the window functions here:
--
--     python3 scripts/backfill_drawdown.py
--
-- It shares update_drawdown()'s arithmetic and the same local-day conversion
-- (storage/database._local_day_window_utc), so the backfilled history and
-- everything written from today forward are the same calculation. A separate
-- SQL implementation would be a second definition of drawdown, and §15 is a
-- whole section about what happens when the same quantity gets computed twice.
--
-- It also prints the observed distribution, which is the input to choosing
-- max_intraday_drawdown_pct.
--
-- ASSIGNMENT vs high-water mark: the backfill uses GREATEST on
-- paper_max_drawdown, so re-running it can only ever raise the recorded
-- intraday figure, never erase one. paper_running_drawdown is assigned,
-- because it is a current distance from the all-time high rather than a
-- high-water mark.

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS paper_running_drawdown;
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS paper_max_drawdown;
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS running_drawdown;
--
-- Note: rolling back the COLUMNS does not roll back the CONTROL. Removing
-- these while risk.max_intraday_drawdown_pct is set in config.yaml leaves
-- drawdown_breach() reading a missing key, which it treats as 0 and therefore
-- as "no breach" - it fails open, quietly. If you roll this back, set both
-- drawdown caps to 0 in the same change so the config says what is true.
