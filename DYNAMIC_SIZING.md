# Dynamic Position Sizing & Concentration Caps

## Overview

**Dynamic sizing** (2026-07-27) automatically scales position size and concentration limits with portfolio growth, eliminating the need for manual adjustments as your account grows.

### Before (Static Sizing)
- Position size: `$100` (hardcoded)
- Works for $10K accounts, breaks for $100 accounts or $100K+ accounts
- Requires manual config changes as portfolio grows

### After (Dynamic Sizing)
- Position size: `3% of portfolio` (configurable)
- $60 portfolio → $1.80 per trade
- $300 portfolio → $9.00 per trade
- $1000 portfolio → $30 per trade
- ✅ Automatic scaling, no manual changes needed

---

## Configuration

### Enable Dynamic Sizing (config.yaml)

```yaml
trading:
  # Enable dynamic sizing (scales with portfolio)
  use_dynamic_sizing: true
  position_size_pct_of_portfolio: 3.0  # 3% per trade

  # Legacy (used as fallback for first trade or if dynamic disabled)
  trade_size_usd: 100

portfolio_risk:
  # Dynamic concentration caps (already enabled by design)
  # Sector cap 35% on $100 = $35; on $1000 = $350
  max_sector_exposure_pct: 35
  max_theme_exposure_pct: 40
```

### Disable Dynamic Sizing (Revert to Static)

```yaml
trading:
  use_dynamic_sizing: false
  trade_size_usd: 100  # Back to hardcoded size
```

---

## How It Works

### Position Sizing

**Static (old)**:
```
Position size = $100 (always)
Problem: $60 portfolio gets $100 position (166% of account!)
```

**Dynamic (new)**:
```
Position size = Portfolio Total × Position Size % ÷ 100
Position size = $60 × 3% ÷ 100 = $1.80 per trade
Position size = $300 × 3% ÷ 100 = $9.00 per trade
Position size = $3000 × 3% ÷ 100 = $90.00 per trade
```

### Concentration Caps

**Both static and dynamic use the same percentages**, because they're already percentages applied to the current portfolio total:

```
Sector cap: 35% of current portfolio
$100 portfolio → sector can be $35 max
$1000 portfolio → sector can be $350 max
(Cap percentage unchanged, absolute dollars scale automatically)
```

### Fallback Logic

On the **first trade of a session** (portfolio empty), dynamic sizing falls back to `trade_size_usd` since there's no portfolio yet. On subsequent trades, it uses the dynamic percentage.

---

## Implementation Details

### Changed Files

1. **config.yaml**
   - Added `use_dynamic_sizing: true`
   - Added `position_size_pct_of_portfolio: 3.0`
   - Documented with examples

2. **engine/position_sizing.py**
   - Added `_get_portfolio_total()` helper to sum open positions
   - Modified `calculate()` to accept `db` parameter
   - Implements dynamic sizing logic when enabled
   - Calculates: `base_allocation = portfolio_total × position_pct / 100`

3. **scheduler.py**
   - Updated `calc_position_size()` call to pass `db` parameter
   - Enables position_sizing.py to access portfolio data

4. **engine/portfolio_risk.py**
   - Added documentation that dynamic caps work implicitly
   - No code changes needed (percentages already scale with portfolio)

---

## Usage Examples

### Your Situation (3 holdings, $60 portfolio)

**Before fix**:
- `trade_size_usd: 100` → tried to add $100 to $60 portfolio
- Every sector blocked (not enough diversification headroom)

**After fix**:
```yaml
use_dynamic_sizing: true
position_size_pct_of_portfolio: 3.0  # $60 × 3% = $1.80 per trade
```

Result: Can now add positions as small as $1.80, giving diversification room:
- Existing: SAN $20, HNGE $20, AAPL $20 (total $60)
- New position: $1.80 (adds 3%, stays under limits)
- New portfolio: $61.80 (still diversified across sectors)

### Growing Account

```
After several cycles of profitable trades:
- Portfolio grows to $500
- Position size auto-scales: $500 × 3% = $15 per trade
- No config changes needed
- Keeps sizing proportional as account grows

Later, at $5000 portfolio:
- Position size: $5000 × 3% = $150 per trade
- Sector cap (35%): can hold $1750 per sector
- Theme cap (40%): can hold $2000 per theme
- All scales automatically
```

---

## Tuning

### Position Size Percentage

Default: `3.0%` (conservative, prioritizes diversification)

Adjust based on your risk tolerance:

```yaml
# Conservative (more positions, lower per-trade risk)
position_size_pct_of_portfolio: 2.0  # 2% per trade

# Aggressive (fewer positions, higher per-trade risk)
position_size_pct_of_portfolio: 5.0  # 5% per trade
```

### Concentration Caps

Defaults already optimal:

```yaml
portfolio_risk:
  max_sector_exposure_pct: 35      # ← Good default
  max_theme_exposure_pct: 40       # ← Good default
  hard_block_on_severe_breach: true # ← Enforce limits
```

Only adjust if you have a specific diversification strategy:

```yaml
# More concentration tolerance
max_sector_exposure_pct: 45
max_theme_exposure_pct: 50

# Stricter diversification
max_sector_exposure_pct: 25
max_theme_exposure_pct: 30
```

---

## Verification

### Check Dynamic Sizing is Active

Look for in logs:
```
[scheduler] Analyzing TICKER: position_size_pct_of_portfolio -> dynamic calc
```

Or trace in code:
1. `scheduler.py` calls `calc_position_size(..., db=db)`
2. `position_sizing.py` checks `use_dynamic_sizing` flag
3. If true and `db` provided, calculates `portfolio_total`
4. Position size = `portfolio_total × position_pct / 100`

### Test First Trade Still Works

On an empty portfolio (first trade of session):
- `_get_portfolio_total()` returns 0
- Falls back to `trade_size_usd`
- Trade places normally
- Second trade uses dynamic calc

---

## FAQ

**Q: My portfolio is $60 but position size is $3, not $1.80. Why?**
A: Other factors also scale position size: buy score, volatility, regime, portfolio risk headroom. 3% is the BASE - multiply by: score tier (25-100%), EV confidence (50-100%), volatility (45-100%), regime (0.5-1.2x), portfolio risk (0-1.0x). Result: $1.80 × (those multipliers) = actual size.

**Q: What if my portfolio shrinks (realized losses)?**
A: Position size shrinks automatically. Dynamic sizing responds to real portfolio changes.

**Q: Can I mix dynamic and static position sizing?**
A: Not recommended. Set `use_dynamic_sizing: true` or `false` globally in config. Mixing causes confusion.

**Q: What happens if I manually edit positions (add/remove)?**
A: Dynamic sizing recalculates every cycle based on current open positions. Manual edits are immediately reflected in position size calculations.

**Q: Does this work with paper trading?**
A: Yes. Paper book and real book are tracked separately. `use_dynamic_sizing` applies to whichever book you're trading (paper or live).

---

## Rollback

If dynamic sizing causes issues:

```yaml
trading:
  use_dynamic_sizing: false  # ← Disable
  trade_size_usd: 100        # ← Back to static
```

Restart scheduler and trading reverts to pre-2026-07-27 behavior.

---

## Next Steps

1. **Enable**: Set `use_dynamic_sizing: true` in config.yaml
2. **Test**: Run next cycle, verify position sizes are smaller and more granular
3. **Monitor**: Check that positions scale as portfolio grows
4. **Tune**: Adjust `position_size_pct_of_portfolio` if needed (start at 3%, move to 2-5% based on preference)
5. **Enjoy**: No more manual position size adjustments!
