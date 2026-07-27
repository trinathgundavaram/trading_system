#!/bin/bash
# FINAL RELEASE COMMANDS FOR v3.4.0
# Copy and run these commands in your terminal (not in bash subprocess)
# Time: ~5 minutes

cd /Users/trinathrao/trading_platform

# ============================================================================
# STEP 1: Verify clean state and commit any pending changes
# ============================================================================
echo "Step 1: Check git status"
git status

# If there are uncommitted changes, commit them:
echo ""
echo "Step 2: Stage all changes"
git add -A

# ============================================================================
# STEP 2: Create CHANGELOG entry (if not already done)
# ============================================================================
echo ""
echo "Step 3: Update CHANGELOG.md"
echo ""
echo "⚠️  MANUAL: Edit CHANGELOG.md and replace [Unreleased] with:"
echo ""
cat << 'CHANGELOG'
## [3.4.0] — v3.4.0 — 2026-07-27

### Decision function: UNCHANGED — infrastructure enhancement

Position sizing engine enhanced to scale with portfolio size.

#### Added — Dynamic Position Sizing

Position size now automatically scales with portfolio growth.

**Configuration** (config.yaml):
```yaml
trading:
  use_dynamic_sizing: true
  position_size_pct_of_portfolio: 3.0
```

**Benefits**:
- Position size = Portfolio × 3% ÷ 100
- $60 portfolio → $1.80 per trade
- $300 portfolio → $9.00 per trade
- Automatic scaling, no manual adjustments

**Technical**:
- New: engine/position_sizing.py:_get_portfolio_total()
- Modified: calculate() function signature (added db parameter)
- Tests: 6 new unit tests (all passing)

See DYNAMIC_SIZING.md for user guide.
CHANGELOG

echo ""
echo "After editing CHANGELOG.md, run:"
echo ""
echo "  git add CHANGELOG.md"
echo "  git commit -m 'feat: dynamic position sizing - scales with portfolio growth'"
echo ""
echo "Then run the release script:"
echo ""
echo "  ./scripts/release.sh minor"
echo ""
echo "============================================================================"
echo "IMPORTANT: Run these commands directly in your terminal, not in a"
echo "subprocess. The git lock issue is environment-specific."
echo "============================================================================"
