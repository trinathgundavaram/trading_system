# Buy Signal Analysis: Why Trades Are Not Being Confirmed

## Executive Summary
**Root Cause Found**: Buy signals are being generated correctly (17 BUY signals in the last cycle with scores 64-80%), but **100% of them are being blocked by portfolio risk constraints** before they reach the execution layer.

---

## The Signal → Execution Flow

```
Signal Generation → Portfolio Risk Check → Execution
      ✅ WORKING          ❌ BLOCKED           (never reached)
  17 BUY candidates   All rejected          0 trades placed
   (scores 64-80%)   (portfolio risk veto)
```

---

## Current Blockages (from scheduler.log)

All of these signals are being rejected at the **portfolio risk gate**:

| Ticker | Score | Reason | Block Condition |
|--------|-------|--------|-----------------|
| TSN    | 80%   | Consumer Defensive sector at 62% cap | >= 1.5x the 35% configured cap |
| OMER   | 79%   | Healthcare sector at 75% cap | >= 1.5x the 35% configured cap |
| BMNR   | 74%   | Financial Services sector at 75% cap | >= 1.5x the 35% configured cap |
| SONY   | 64%   | Technology sector at 75% cap | >= 1.5x the 35% configured cap |
| SHAK   | 69%   | Consumer Cyclical sector at 62% cap | >= 1.5x the 40% cap for theme |

**Pattern**: Every single BUY CANDIDATE is hitting the sector/theme concentration limits.

---

## Where Trades Get Blocked

### In Execution Order:

1. **scheduler.py:863** - `watch_mode` is determined
2. **scheduler.py:1312-1322** - Conditional execute_buy is called
   - If watch_mode = True → `paper_trader.execute_buy()`
   - If watch_mode = False → `live_trader.execute_buy_live()`

3. **Before order placement**, six guards check:
   - ✅ `is_watch_mode()` - PASS
   - ✅ Buy signal generated - PASS (score > threshold)
   - ❌ **Portfolio Risk Check** - **FAIL** (sector concentration)
   - ❌ Position/rotation logic - never reached due to above
   - ❌ Cash check - never reached
   - ❌ Max positions - never reached

### The Portfolio Risk Gate (Critical)

**Location**: `scheduler.py:1295-1307` in the ticker loop, before `execute_buy()` call

```python
if portfolio_risk_result and not portfolio_risk_result.should_buy:
    db.log_ui_event("buy_blocked", {
        "ticker": ticker,
        "stage": "portfolio_risk",
        "reason": portfolio_risk_result.reason,  # "sector at 75% (>= 1.5x the 35% cap)"
    })
    continue_buy = False  # ← Order never placed
```

---

## Why This Is Happening

Your current config has:
- **Sector cap**: ~35% of portfolio
- **Theme cap**: ~40% of portfolio
- **Hard limit**: 1.5x the cap (52.5% sector / 60% theme) triggers a block

Your portfolio is already at ~62-75% in several sectors (Financial Services, Healthcare, Technology, Consumer Defensive), which exceeds these multiples, so **no new buys are allowed** in those sectors until existing positions are reduced.

---

## Diagnosis: Two Possible Issues

### 1. **Portfolio is Overconcentrated** (Most Likely)
Your paper book or real positions are too heavy in certain sectors. The system is correctly preventing further concentration.

**Check this with**:
```bash
# See current sector allocation
SELECT sector, SUM(cost_basis) as sector_total
FROM positions WHERE simulated=true AND closed_at IS NULL
GROUP BY sector ORDER BY sector_total DESC;
```

### 2. **Risk Caps Are Too Conservative** (Possible)
Your configured sector/theme caps in `config.yaml` are stricter than intended.

**Check this with**:
```yaml
# In config.yaml, look for:
risk:
  sector_concentration_cap: 0.35  # 35% per sector
  theme_concentration_cap: 0.40   # 40% per theme
  concentration_hard_limit_multiplier: 1.5  # 1.5x = hard stop
```

---

## Why Signals Are Generating But Not Executing

### The Correct Separation of Concerns:

1. **Signal Generation** (`engine/ticker_analyzer.py` + scoring rules)
   - Purpose: Answer "should we buy this stock?"
   - Does NOT check: Portfolio constraints, sector concentration, etc.
   - Result: Feeds back objective buy/sell signals based on pure stock metrics

2. **Portfolio Risk Check** (`rules/portfolio_risk.py` or similar)
   - Purpose: Answer "can we buy this stock *given current portfolio*?"
   - Checks: Sector limits, theme limits, correlation, risk decay
   - Result: Veto applied BEFORE execution

3. **Trade Execution** (`engine/paper_trader.execute_buy()` or `engine/live_trader.execute_buy_live()`)
   - Purpose: Place the actual trade
   - Only runs if portfolio risk approved
   - Currently: **Never reached** for any of your candidates

This design is **correct**. Signals should be pure, but execution should be constrained by portfolio state.

---

## Your Exact Configuration (config.yaml)

```yaml
portfolio_risk:
  enabled: true
  max_sector_exposure_pct: 35          # ← Base cap
  max_theme_exposure_pct: 40           # ← Base cap
  hard_block_on_severe_breach: true    # ← HARD BLOCK IS ON
  severe_breach_multiple: 1.5          # ← Multiplier
```

**Math**: 35% × 1.5 = **52.5% hard limit per sector**

Your current sectors are at 62-75%, which is **9.5-22.5% over the hard limit**.

---

## Solutions

### Option A: Reduce Sector Concentration (Recommended)
Close or trim positions in overweight sectors to get below the hard limit.

**Target**: Get each sector below 52.5% (preferably back to the 35% base cap)

Example for Financial Services at 75%:
```
Current exposure:    75%
Hard limit:          52.5%
Need to reduce by:   22.5% ($225k if portfolio is $1M)
Action:              Sell 1-2 Financial Services positions
```

### Option B: Disable Hard Block (Risk-Aware)
If you want to allow new buys while rebalancing, you can temporarily disable the hard block:

```yaml
portfolio_risk:
  hard_block_on_severe_breach: false  # Changed from true
```

**But understand**: This weakens risk management. The system will still SCALE DOWN position size through `size_multiplier`, but will not refuse the entry outright.

### Option C: Relax the Severe Breach Multiple (Risk-Aware)
Increase the multiplier from 1.5x to 1.8x:

```yaml
portfolio_risk:
  severe_breach_multiple: 1.8  # Increased from 1.5
```

**Effect**: Hard limit becomes 35% × 1.8 = 63% (allowing your current 62-75% positions)

**Warning**: This weakens diversification controls. Only do this if you've validated that sector concentration is intentional.

### Option D: Increase Base Sector Cap (Risk-Aware)
Raise the base sector cap from 35% to 40-45%:

```yaml
portfolio_risk:
  max_sector_exposure_pct: 40  # Increased from 35
  # Hard limit becomes: 40% × 1.5 = 60%
```

This still controls concentration but allows more sector weight.

### Option E: Combination Approach (Recommended)
1. **Immediately**: Sell 1-2 positions in the most overweight sectors to get below 52.5%
2. **After rebalancing**: Adjust config caps if you want higher concentration going forward
3. **Monitor**: Track `portfolio_risk_log` to see how often the engine wants to buy vs. has to skip

---

## Immediate Action Items

### 1. Verify the Issue (Run These SQL Queries)

```sql
-- Check which sectors are overweight
SELECT
  ticker,
  sector,
  dollar_amount,
  ROUND(100.0 * dollar_amount /
    (SELECT SUM(dollar_amount) FROM positions WHERE simulated=true AND closed_at IS NULL),2) as pct_of_portfolio
FROM positions
WHERE simulated=true AND closed_at IS NULL
ORDER BY dollar_amount DESC;

-- Summary by sector
SELECT
  COALESCE(sector, 'N/A') as sector,
  COUNT(*) as position_count,
  ROUND(SUM(dollar_amount), 2) as sector_total,
  ROUND(100.0 * SUM(dollar_amount) /
    (SELECT SUM(dollar_amount) FROM positions WHERE simulated=true AND closed_at IS NULL), 1) as pct_of_portfolio
FROM positions
WHERE simulated=true AND closed_at IS NULL
GROUP BY sector
ORDER BY sector_total DESC;
```

### 2. Pick Your Strategy

**A) Reduce Concentration** (Recommended)
- Identify the 2-3 most overweight sectors
- Sell 1-2 positions per sector
- Goal: Get all sectors below 52.5% hard limit
- Benefit: No config changes, keeps risk controls intact

**B) Adjust Configuration** (If concentration is intentional)
- Edit `config.yaml` with one of the options above
- Restart scheduler: `python scheduler.py` or equivalent
- Monitor next cycle for resumed trading

### 3. Monitor the Fix

After your change, check scheduler.log:
```bash
# Should start seeing "BUY CANDIDATE" followed by actual trades
tail -f output/logs/scheduler.log | grep -E "BUY CANDIDATE|FILLED buy|BUY blocked"
```

**Before fix**: Every "BUY CANDIDATE" → "BUY blocked by portfolio risk"
**After fix**: "BUY CANDIDATE" → Either "FILLED buy" or skipped for other reasons

---

## Verification Checklist

- [ ] **Confirm**: Check `scheduler.log` for "BUY blocked by portfolio risk" - VERIFIED, all 17 signals blocked
- [ ] **Identify**: Which sectors/themes are overweight? - IDENTIFIED (Financial Services, Healthcare, Technology, Consumer Defensive/Cyclical at 62-75%)
- [ ] **Measure**: By how much do they exceed 1.5x the cap? - MEASURED (62-75% vs 52.5% hard limit = 9.5-22.5% over)
- [ ] **Decide**: Reduce positions or relax caps? - YOUR CHOICE (see Options A-E above)
- [ ] **Execute**: Apply chosen solution
- [ ] **Monitor**: Next cycle should show trades executing if issue is resolved

---

## Code References

| Component | File | Line | Purpose |
|-----------|------|------|---------|
| Portfolio Risk Check | scheduler.py | 1295-1307 | Veto gate before execution |
| Risk Engine | rules/portfolio_risk.py | — | Sector/theme calculation |
| Paper Execution | engine/paper_trader.py | 83-290 | Execute buy (if approved) |
| Live Execution | engine/live_trader.py | 394-569 | Execute buy (if approved) |
| Signal Log | scheduler.py | 1377-1394 | Records why each signal fired/failed |
