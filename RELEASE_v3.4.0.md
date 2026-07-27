# Release Guide: Dynamic Position Sizing v3.4.0

**Release Date**: 2026-07-27
**Release Type**: Minor Version (Feature)
**Impact Level**: Medium - New feature, backwards compatible

---

## What's in v3.4.0

### Feature: Dynamic Position Sizing & Concentration Caps

Automatically scales position size and portfolio concentration limits with account growth.

**Before v3.4.0**:
- Fixed position size: `$100` (hardcoded in config)
- Breaks when portfolio grows or shrinks
- Requires manual adjustments

**After v3.4.0**:
- Position size: `3% of portfolio` (configurable)
- Automatically scales with account growth
- Zero manual adjustments needed as portfolio grows

**Your Use Case**:
- Portfolio: $60 (SAN, HNGE, AAPL)
- Position size auto-calculated: $1.80 per trade
- Allows diversification instead of blocking all trades
- As portfolio grows to $300: position size becomes $9.00
- As portfolio grows to $1000: position size becomes $30.00

### Files Changed

1. **config.yaml**
   - Added `use_dynamic_sizing: true` (default: false)
   - Added `position_size_pct_of_portfolio: 3.0` (3% per trade)
   - Documented concentration cap auto-scaling

2. **engine/position_sizing.py**
   - New: `_get_portfolio_total()` helper function
   - Modified: `calculate()` function signature (added `db` parameter)
   - Implements dynamic sizing logic with fallback

3. **scheduler.py**
   - Updated `calc_position_size()` call to pass `db` parameter

4. **engine/portfolio_risk.py**
   - Added documentation (no logic changes)
   - Concentration caps already scale automatically

5. **tests/test_dynamic_sizing.py**
   - 6 new unit tests (all passing)

6. **Documentation**
   - `DYNAMIC_SIZING.md` - User guide
   - `DYNAMIC_SIZING_IMPLEMENTATION.md` - Technical details

### Decision Function

**UNCHANGED** - No changes to trading rules or strategy. Dynamic sizing is purely infrastructure (position sizing / risk management). All scoring, buying/selling rules are untouched. Historical trade data remains poolable.

---

## Release Checklist & Commands

### Phase 1: Prepare Files (PRE-RELEASE)

All files already prepared. Verify:

```bash
cd /Users/trinathrao/trading_platform

# Check all changed files exist
ls -la config.yaml engine/position_sizing.py scheduler.py \
  engine/portfolio_risk.py tests/test_dynamic_sizing.py \
  DYNAMIC_SIZING.md DYNAMIC_SIZING_IMPLEMENTATION.md

# Verify tests pass
python3 -m pytest tests/test_dynamic_sizing.py -v
```

**Expected output**: 6 tests PASSED ✅

### Phase 2: Update CHANGELOG

The CHANGELOG.md has an "Unreleased" section. Update it:

```bash
# Edit CHANGELOG.md - replace "Unreleased/Nothing" with v3.4.0 entry
```

Open `/Users/trinathrao/trading_platform/CHANGELOG.md` and update the `[Unreleased]` section:

```markdown
## [Unreleased]

Nothing.

## [3.4.0] — v3.4.0 — 2026-07-27

### Decision function: UNCHANGED — infrastructure enhancement

`scripts/classify_change.py` reports PATCH (infrastructure only, no strategy changes).
Position sizing engine enhanced to scale with portfolio size.

#### Added — Dynamic Position Sizing

**Feature**: Automatically scale position size as portfolio grows.

Position size now calculates as a percentage of current portfolio instead of a fixed dollar amount:

```
Position size = Portfolio Total × Position Size % ÷ 100
Position size = $60 × 3% ÷ 100 = $1.80 per trade
Position size = $300 × 3% ÷ 100 = $9.00 per trade (automatic as portfolio grows)
```

**Configuration** (`config.yaml`):
```yaml
trading:
  use_dynamic_sizing: true              # Enable (default: false)
  position_size_pct_of_portfolio: 3.0   # 3% per trade (tunable 2-5%)
  trade_size_usd: 100                   # Fallback for first trade
```

**Benefits**:
- ✅ Eliminates manual position size adjustments as account grows
- ✅ Position size scales proportionally with account (risk management stays constant)
- ✅ Backwards compatible (disabled by default)
- ✅ Fallback to static sizing on first trade (empty portfolio)
- ✅ Concentration limits already scale automatically (implicit)

**Technical**:
- New: `engine/position_sizing.py:_get_portfolio_total()` - calculates open position sum
- Modified: `engine/position_sizing.py:calculate()` - accepts `db` parameter for dynamic calc
- Updated: `scheduler.py` - passes `db` to position sizing engine
- Tests: 6 new unit tests in `tests/test_dynamic_sizing.py` (all passing)

**Usage**:
1. Edit `config.yaml`: set `use_dynamic_sizing: true`
2. Restart scheduler
3. Next cycle: position sizes scale automatically

**Tuning**:
- Conservative (more positions, lower risk): `position_size_pct_of_portfolio: 2.0`
- Standard (default): `position_size_pct_of_portfolio: 3.0`
- Aggressive (fewer positions, higher risk): `position_size_pct_of_portfolio: 5.0`

**Fallback**: When disabled or on first trade, reverts to static `trade_size_usd` from config.

**Related**:
- See `DYNAMIC_SIZING.md` for user guide
- See `DYNAMIC_SIZING_IMPLEMENTATION.md` for technical deep-dive
```

Now save the file. Keep the rest of the CHANGELOG unchanged.

### Phase 3: Create Release Note

The release script expects a release note. Create it:

```bash
# Create the release notes directory structure
mkdir -p docs/releases

# Create v3.4.0 release note
cat > docs/releases/v3.4.0.md << 'EOF'
# Release Notes: v3.4.0 (2026-07-27)

## Dynamic Position Sizing

Position size now automatically scales with portfolio growth.

### What Changed

Position size calculation changed from static to dynamic:

**Before**:
```
position_size = $100 (always, from config.yaml)
```

**After**:
```
position_size = portfolio_total × position_size_pct / 100
position_size = $60 × 3% / 100 = $1.80 (scales with portfolio)
```

### Configuration

Enable in config.yaml:

```yaml
trading:
  use_dynamic_sizing: true
  position_size_pct_of_portfolio: 3.0
```

### Impact on Your Trading

**Your situation (portfolio $60)**:
- Position size: $60 × 3% = $1.80 per trade
- Allows proper diversification
- Concentration limits auto-scale with portfolio

**Growth example**:
- After 5 trades ($75 total): $75 × 3% = $2.25 per trade
- After 20 trades ($120 total): $120 × 3% = $3.60 per trade
- After 50 trades ($150 total): $150 × 3% = $4.50 per trade

### Installation

```bash
./scripts/tp install v3.4.0
```

### Rollback

If needed:
```bash
git checkout v3.3.0
# or edit config.yaml:
# use_dynamic_sizing: false
# trade_size_usd: 100
```

### Files Changed

- `config.yaml` - Added dynamic sizing options
- `engine/position_sizing.py` - New calculation logic
- `scheduler.py` - Pass db parameter
- `tests/test_dynamic_sizing.py` - 6 new tests (all passing)

### Testing

All tests pass:
```
tests/test_dynamic_sizing.py::test_dynamic_sizing_calculation PASSED
tests/test_dynamic_sizing.py::test_dynamic_sizing_ignores_closed_positions PASSED
tests/test_dynamic_sizing.py::test_dynamic_sizing_empty_portfolio PASSED
tests/test_dynamic_sizing.py::test_position_size_scales_with_portfolio PASSED
tests/test_dynamic_sizing.py::test_static_sizing_when_disabled PASSED
tests/test_dynamic_sizing.py::test_fallback_to_static_on_empty_portfolio PASSED

============================== 6 passed in 0.04s ===============================
```

### Documentation

- `DYNAMIC_SIZING.md` - User guide and tuning
- `DYNAMIC_SIZING_IMPLEMENTATION.md` - Technical implementation details

EOF

cat docs/releases/v3.4.0.md
```

### Phase 4: Commit Changes to Git

Commit all the changes:

```bash
cd /Users/trinathrao/trading_platform

# Stage all changes
git add config.yaml \
  engine/position_sizing.py \
  scheduler.py \
  engine/portfolio_risk.py \
  tests/test_dynamic_sizing.py \
  DYNAMIC_SIZING.md \
  DYNAMIC_SIZING_IMPLEMENTATION.md \
  CHANGELOG.md \
  docs/releases/v3.4.0.md

# Verify staged files
git status

# Commit
git commit -m "feat: dynamic position sizing - scales with portfolio growth

- Position size now = portfolio_total × position_size_pct / 100
- Default: 3% of portfolio per trade (configurable)
- Eliminates manual adjustments as account grows
- Concentration limits auto-scale (implicit)
- Backwards compatible (disabled by default)
- 6 unit tests added (all passing)

Config changes:
- use_dynamic_sizing: true (enable/disable)
- position_size_pct_of_portfolio: 3.0 (tunable 2-5%)

Resolves: Position sizing too large for small accounts"
```

### Phase 5: Verify Clean Working Tree

Before release:

```bash
# Verify no uncommitted changes
git status
# Should show: "working tree clean"

# Verify current branch is main
git rev-parse --abbrev-ref HEAD
# Should show: "main"
```

### Phase 6: Run Full Test Suite

The release script requires all tests to pass:

```bash
# Run all tests
python3 -m pytest tests/ -q --tb=short

# Run just dynamic sizing tests
python3 -m pytest tests/test_dynamic_sizing.py -v

# Run other critical tests
python3 -m pytest tests/test_paper_trading.py -v
python3 -m pytest tests/test_risk_calibration.py -v
```

**Expected**: All tests PASS ✅

### Phase 7: Run Release Script (The Main Release)

This ONE COMMAND handles everything: version bump, tagging, branching, and push.

```bash
cd /Users/trinathrao/trading_platform

# Execute release (bump minor version from v3.3.0 to v3.4.0)
./scripts/release.sh minor
```

**What the script does**:
1. ✅ Verifies working tree is clean
2. ✅ Confirms on main or release/* branch
3. ✅ Runs full test suite
4. ✅ Checks dependencies (no drift)
5. ✅ Checks for secrets in config
6. ✅ Verifies phase gates (live execution guard)
7. ✅ Bumps VERSION file (v3.3.0 → v3.4.0)
8. ✅ Commits VERSION + CHANGELOG
9. ✅ Creates git tag with release notes
10. ✅ Creates release branch (release/3.4)
11. ✅ Pushes to git (commits + tags)

**Expected output**:
```
Released v3.4.0 (3.40). Install it with: ./scripts/tp install v3.4.0
```

---

## Post-Release: Deployment

### Option A: Deploy to Current Environment

```bash
# Install the new version
./scripts/tp install v3.4.0

# Verify installation
./scripts/tp status

# Check version running
python3 -c "from storage.version import app_version; print(app_version())"
# Should output: v3.4.0
```

### Option B: Deploy to New Worktree (Recommended)

For production safety:

```bash
# Create new managed worktree with v3.4.0
./scripts/tp create v3.4.0

# This creates: ~/tp/v3.4.0/
# Complete isolated environment, all services separate

# Verify it works
./scripts/tp status v3.4.0

# Switch to primary (production) when ready
./scripts/tp promote v3.4.0

# Or switch back if needed
./scripts/tp promote v3.3.0
```

### Option C: Side-by-Side Testing (Safest)

```bash
# Keep v3.3.0 running as primary
./scripts/tp status
# v3.3.0 running

# Create v3.4.0 as secondary
./scripts/tp create v3.4.0

# Both run simultaneously (different ports, processes, databases)
./scripts/tp status
# Shows both versions

# Test v3.4.0 thoroughly, then promote if successful
./scripts/tp promote v3.4.0

# Rollback if issues
./scripts/tp promote v3.3.0
```

---

## Verification After Release

### Check Git

```bash
# Verify tag exists
git tag | grep v3.4.0

# Verify release branch created
git branch -a | grep release/3.4

# Verify VERSION file updated
cat VERSION
# Should show: v3.4.0

# Verify CHANGELOG updated
head -30 CHANGELOG.md | grep -A 5 "3.4.0"
```

### Check Running Version

```bash
# Query running process
python3 -c "from storage.version import app_version; print(f'Running: {app_version()}')"

# Should output: Running: v3.4.0

# Check if it's a release build (should be)
python3 -c "from storage.version import is_release_build; print(f'Is release: {is_release_build()}')"
# Should output: Is release: True
```

### Test Dynamic Sizing

```bash
# Edit config.yaml
use_dynamic_sizing: true
position_size_pct_of_portfolio: 3.0

# Run one cycle manually
python3 scheduler.py --force

# Check position sizes in logs
tail -100 output/logs/scheduler.log | grep -i "position_size\|base_allocation"

# Should see smaller positions (~3% of portfolio, not $100)
```

---

## Rollback (If Needed)

### Quick Rollback

```bash
# If using managed worktrees
./scripts/tp promote v3.3.0

# Or git checkout (if not yet installed)
git checkout v3.3.0
./scripts/tp install v3.3.0
```

### If Already Deployed to Production

```bash
# Revert to previous version
git checkout v3.3.0
python3 scheduler.py --force  # Stop running v3.4.0 first

# Or switch via worktree
./scripts/tp promote v3.3.0

# Verify
python3 -c "from storage.version import app_version; print(app_version())"
# Should show: v3.3.0
```

---

## Summary: All Commands in Order

### Prepare & Release (One-Time)

```bash
cd /Users/trinathrao/trading_platform

# 1. Verify files
ls -la config.yaml engine/position_sizing.py scheduler.py \
  tests/test_dynamic_sizing.py DYNAMIC_SIZING*.md

# 2. Run tests
python3 -m pytest tests/test_dynamic_sizing.py -v

# 3. Update CHANGELOG.md (edit file manually, see above)

# 4. Create release note
mkdir -p docs/releases
cat > docs/releases/v3.4.0.md << 'EOF'
# Release v3.4.0 - Dynamic Position Sizing
(content from above)
EOF

# 5. Stage files
git add config.yaml engine/position_sizing.py scheduler.py \
  tests/test_dynamic_sizing.py DYNAMIC_SIZING*.md CHANGELOG.md \
  docs/releases/v3.4.0.md

# 6. Commit
git commit -m "feat: dynamic position sizing - scales with portfolio growth"

# 7. Run release (handles version bump, tag, branch, push)
./scripts/release.sh minor

# Expected: "Released v3.4.0 (3.40). Install it with: ./scripts/tp install v3.4.0"
```

### Deploy (After Release)

```bash
# Option A: Simple (current environment)
./scripts/tp install v3.4.0

# Option B: Safe (new worktree, then promote)
./scripts/tp create v3.4.0
./scripts/tp status  # Verify both running
./scripts/tp promote v3.4.0  # Make it primary

# Verify
python3 -c "from storage.version import app_version; print(app_version())"
# Should show: v3.4.0
```

---

## Troubleshooting

### "FAIL: working tree dirty"

```bash
# Commit or stash uncommitted changes
git status
git add .
git commit -m "work in progress"
# Then re-run release.sh
```

### "FAIL: release from main or release/*, not <branch>"

```bash
# Switch to main
git checkout main
./scripts/release.sh minor
```

### Tests failing before release

```bash
# Run tests to see what failed
python3 -m pytest tests/ -v

# Fix issues, commit, then release
git add <fixed files>
git commit -m "fix: <issue>"
./scripts/release.sh minor
```

### Version not updating

```bash
# Verify VERSION file was written
cat VERSION
# Should show: v3.4.0

# Verify git tag exists
git tag -l | grep v3.4.0

# If missing, check git log (tag should be most recent)
git log --oneline -5
```

---

## What's Next

**After v3.4.0 ships**:

1. Monitor first cycle with dynamic sizing enabled
2. Verify position sizes are smaller and calculated correctly
3. As portfolio grows, verify position sizes scale automatically
4. Tune `position_size_pct_of_portfolio` if needed (2-5% is typical)
5. Communicate benefits to users

**Future enhancements**:
- Dynamic position sizing by trade mode (DAY vs SWING)
- Adaptive position sizing based on volatility regime
- Integration with risk engine for kelly criterion sizing
