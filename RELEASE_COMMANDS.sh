#!/bin/bash
# RELEASE v3.4.0 - Dynamic Position Sizing
# Quick command reference (2026-07-27)
#
# Usage: Copy/paste commands in order
# Time needed: ~5 minutes
#

set -e  # Exit on error

cd /Users/trinathrao/trading_platform

echo "======================================================================"
echo "RELEASE v3.4.0: Dynamic Position Sizing"
echo "======================================================================"
echo ""

# ============================================================================
# PHASE 1: VERIFY
# ============================================================================
echo "[1/7] Verifying files..."
ls -la config.yaml engine/position_sizing.py scheduler.py \
  engine/portfolio_risk.py tests/test_dynamic_sizing.py \
  DYNAMIC_SIZING.md DYNAMIC_SIZING_IMPLEMENTATION.md

echo ""
echo "[2/7] Running tests..."
python3 -m pytest tests/test_dynamic_sizing.py -v

echo ""
echo "✅ Files verified and tests passing"
echo ""

# ============================================================================
# PHASE 2: UPDATE CHANGELOG
# ============================================================================
echo "[3/7] Update CHANGELOG.md"
echo ""
echo "⚠️  MANUAL STEP - Edit CHANGELOG.md:"
echo "   Replace [Unreleased] section with:"
echo ""
cat << 'CHANGELOG'
## [3.4.0] — v3.4.0 — 2026-07-27

### Decision function: UNCHANGED — infrastructure enhancement

Position sizing engine enhanced to scale with portfolio size.

#### Added — Dynamic Position Sizing

Position size now automatically scales with portfolio growth instead of
using fixed dollar amount. Configuration in config.yaml:

```yaml
trading:
  use_dynamic_sizing: true
  position_size_pct_of_portfolio: 3.0  # 3% per trade
```

Position size = Portfolio Total × Position Size % ÷ 100
- $60 portfolio → $1.80 per trade
- $300 portfolio → $9.00 per trade
- $3000 portfolio → $90 per trade

**Benefits**: Automatic scaling as portfolio grows, zero manual adjustments.

**Technical**: New `_get_portfolio_total()` in position_sizing.py,
updated `calculate()` signature, 6 unit tests added and passing.

**See**: DYNAMIC_SIZING.md (user guide), DYNAMIC_SIZING_IMPLEMENTATION.md (details)
CHANGELOG

echo ""
echo "Press ENTER when CHANGELOG.md is updated..."
read -r

echo ""
echo "✅ CHANGELOG.md updated"
echo ""

# ============================================================================
# PHASE 3: COMMIT CHANGES
# ============================================================================
echo "[4/7] Staging files for commit..."

git add config.yaml \
  engine/position_sizing.py \
  scheduler.py \
  engine/portfolio_risk.py \
  tests/test_dynamic_sizing.py \
  DYNAMIC_SIZING.md \
  DYNAMIC_SIZING_IMPLEMENTATION.md \
  CHANGELOG.md

echo ""
echo "Staged files:"
git status

echo ""
echo "[5/7] Committing changes..."

git commit -m "feat: dynamic position sizing - scales with portfolio growth

- Position size now = portfolio_total × position_size_pct / 100
- Default: 3% of portfolio per trade (configurable 2-5%)
- Eliminates manual adjustments as account grows
- Concentration limits auto-scale (implicit in algorithm)
- Backwards compatible (disabled by default)
- 6 unit tests added (all passing)

Config changes:
- use_dynamic_sizing: true (enable/disable)
- position_size_pct_of_portfolio: 3.0 (tunable)

Benefits:
- \$60 portfolio: \$1.80 per trade
- \$300 portfolio: \$9.00 per trade
- \$3000 portfolio: \$90 per trade
(scales automatically as portfolio grows)"

echo ""
echo "✅ Changes committed"
echo ""

# ============================================================================
# PHASE 4: VERIFY CLEAN TREE
# ============================================================================
echo "[6/7] Verifying clean working tree..."

if [ -z "$(git status --porcelain)" ]; then
  echo "✅ Working tree clean"
else
  echo "❌ FAIL: Working tree not clean"
  git status
  exit 1
fi

echo ""
echo "Current branch: $(git rev-parse --abbrev-ref HEAD)"

echo ""
echo "======================================================================"
echo "READY FOR RELEASE - Run release script:"
echo "======================================================================"
echo ""
echo "    ./scripts/release.sh minor"
echo ""
echo "This will:"
echo "  ✅ Verify clean tree"
echo "  ✅ Run full test suite"
echo "  ✅ Bump version (v3.3.0 → v3.4.0)"
echo "  ✅ Create/commit VERSION file"
echo "  ✅ Create git tag with release notes"
echo "  ✅ Create release branch (release/3.4)"
echo "  ✅ Push to git (commits + tags)"
echo ""
echo "After: ./scripts/tp install v3.4.0"
echo ""
echo "======================================================================"

# ============================================================================
# PHASE 5: RUN RELEASE SCRIPT (COMMENTED - USER RUNS MANUALLY)
# ============================================================================
# echo ""
# echo "[7/7] Running release script..."
# ./scripts/release.sh minor
# echo ""
# echo "✅ Release complete!"
# echo ""
# echo "Install with:"
# echo "  ./scripts/tp install v3.4.0"
# echo ""
# echo "Or deploy to new worktree:"
# echo "  ./scripts/tp create v3.4.0"
# echo "  ./scripts/tp promote v3.4.0"
