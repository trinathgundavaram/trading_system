# Strategies running side by side (paper)

Three independent strategies run from the same scheduler. They share the data
feed and the process, nothing else: each has its own cash, positions and
history, so one cannot spend, close or distort another.

| Strategy | Ledger (tables) | Runs | Switch in `config.yaml` |
|---|---|---|---|
| **Swing engine** (original 7-bucket scoring, ~5-min scan) | `paper_account`, `positions` (simulated=1), `paper_trades`, `paper_equity_history` | every 5 min, Mon-Fri 9:00-16:30 ET | `trading.*` (live gates closed; WATCH/paper) |
| **Sector momentum** (blend, 2.0x, SPY 200d rule + 2% band) | `sm_account`, `sm_positions`, `sm_state`, `sm_equity_log` | daily 16:15 ET; rebalances at month-end | `sector_momentum_sleeve.paper_enabled` |
| **RSI(2) SPY sleeve** | `rsi2_account`, `rsi2_positions` | daily 16:10 ET | `rsi2_sleeve.paper_enabled` |
| TradingAgents (advisory only, no orders) | `ta_decisions` | daily 16:30 ET | `tradingagents_advisor.enabled` |

## Comparing them

    python3 scripts/sleeve_vs_spy_report.py        # sector momentum vs swing engine vs SPY, by month

`output/sleeve_vs_spy.md` is refreshed automatically on each month-end.

## Rules that keep them separate

* Never write sector-momentum or RSI2 rows into the swing tables, or the reverse.
* The swing engine's kill switch (`risk.kill_switch_triggered`) halts the swing
  engine only. The sleeves are paper-only and do not read it.
* Judge each strategy against SPY on its own ledger; do not pool trade history
  across strategies (see the decision-function note at the top of `CHANGELOG.md`).
* Evidence to date (research/): the swing engine did not beat SPY in any walk-forward test; the sector
  blend did on risk-adjusted terms. Treat the swing engine as a benchmark to be
  beaten, not as a source of capital, until its live paper results say otherwise.
