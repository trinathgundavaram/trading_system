-- 004_per_book_daily_counters.sql  (Phase 2 steps 2.2/2.3, §7 + §8)
--
-- daily_stats has always been a LIVE-book table. `trades_placed` is
-- incremented only by engine/live_trader.py and confirm_fill.py, and
-- `realized_pnl` is written only when close_position(simulated=False).
--
-- That was defensible while paper trading was a display feature. It stopped
-- being defensible the moment paper became the primary operating mode,
-- because RiskEngine reads those columns: 31 buys across seven days against a
-- 10/day cap, with "0 trades placed" reported every single day, and a $500
-- daily-loss limit that read $0.00 forever.
--
-- Separate columns rather than widening the existing ones. A paper drawdown
-- must be able to trip the paper session's limit without contaminating the
-- live ledger, and `realized_pnl` is audited as real-money-only - a guarantee
-- worth preserving rather than quietly redefining under its existing readers.
--
-- Purely additive, all defaulted. rollback_safe: true.
--
-- Apply:    ./scripts/apply_migration.sh migrations/004_per_book_daily_counters.sql
--           (NOT `psql "$POSTGRES_DB" -f ...` - POSTGRES_DB is unset in
--            this project's .env, so that expands to an empty database
--            name and psql silently falls back to $USER. See the script.)
-- Rollback: see the commented block at the bottom.

ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS paper_trades_placed  INTEGER DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS paper_winning_trades INTEGER DEFAULT 0;
ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS paper_realized_pnl   REAL    DEFAULT 0;

-- ── Backfill from the ledger ────────────────────────────────────────────────
-- Seven days of real behaviour already sit in paper_trades. Backfilling makes
-- the fix immediately comparable against the past instead of starting from
-- zero, which matters because the whole point of the counters is to answer
-- "how often does this actually happen".
--
-- created_at is naive UTC; the counters are keyed on LOCAL calendar days to
-- match db.paper_realized_pnl_today(). An evening close at 00:30 UTC belongs
-- to the trading day that just ended.
--
-- ASSIGNMENT, not accumulation: re-running this file must not double-count.
INSERT INTO daily_stats (date, paper_trades_placed, paper_winning_trades, paper_realized_pnl)
SELECT
    to_char((created_at::timestamp AT TIME ZONE 'UTC') AT TIME ZONE
            current_setting('TimeZone'), 'YYYY-MM-DD')          AS d,
    COUNT(*),
    COUNT(*) FILTER (WHERE side = 'sell' AND COALESCE(pnl, 0) > 0),
    COALESCE(SUM(pnl) FILTER (WHERE side = 'sell'), 0)
FROM paper_trades
GROUP BY d
ON CONFLICT (date) DO UPDATE SET
    paper_trades_placed  = EXCLUDED.paper_trades_placed,
    paper_winning_trades = EXCLUDED.paper_winning_trades,
    paper_realized_pnl   = EXCLUDED.paper_realized_pnl;

-- ── BACKWARD ────────────────────────────────────────────────────────────────
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS paper_realized_pnl;
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS paper_winning_trades;
-- ALTER TABLE daily_stats DROP COLUMN IF EXISTS paper_trades_placed;
