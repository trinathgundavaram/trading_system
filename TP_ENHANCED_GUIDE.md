# Enhanced Version Management Guide

## Problem

The current `tp` script can leave multiple versions installed, causing:
1. Service registration conflicts (duplicate launchd entries fighting for the same port)
2. Port collisions (both v3.2.0:8081 and v3.3.1:8082, only one marked as primary)
3. launchctl bootstrap failures (`Input/output error`)
4. Wasted disk space (old versions never cleaned up)

## Solution

Use the new `tp_enhanced.py` script to:
1. **Auto-remove old versions** when promoting a new one (only 1 version active)
2. **Fix launchctl bootstrap issues** with modern macOS APIs
3. **Clean up disk space** by removing old worktrees, venvs, and databases

---

## Quick Start

### 1. Promote v3.3.1 and Auto-Clean Old Versions

```bash
cd ~/trading_platform

# Promotes v3.3.1 as primary AND removes all other versions
python3 scripts/tp_enhanced.py promote v3.3.1
```

**What this does:**
- ✅ Sets v3.3.1 as the new PRIMARY version
- ✅ Uninstalls services for old versions
- ✅ Removes v2.1.0 worktree, venv, data directory, and database
- ✅ Removes v3.2.0 worktree, venv, data directory, and database
- ✅ Backs up all databases before removing
- ✅ Updates registry.json
- ✅ Leaves only v3.3.1 installed

### 2. Fix Service Startup Issues

If launchctl bootstrap fails:

```bash
# Fix stuck launchctl entries and reinstall services
python3 scripts/tp_enhanced.py fix-services

# Then verify
./service.sh status
```

**What this does:**
- ✅ Cleans up stuck launchctl entries
- ✅ Removes conflicting duplicate plist files
- ✅ Reinstalls service plists correctly
- ✅ Bootstraps services using modern macOS API
- ✅ Retries with `sudo` if needed

### 3. Manual Cleanup (Keep Current Primary)

If you already promoted but want to manually remove old versions:

```bash
python3 scripts/tp_enhanced.py cleanup
```

**What this does:**
- ✅ Keeps PRIMARY version
- ✅ Backs up all other versions' databases
- ✅ Removes all other versions' worktrees, venvs, data
- ✅ Updates registry.json

---

## Step-by-Step: Deploy v3.3.1

```bash
cd ~/trading_platform

# 1. Backup (optional - already done by promote)
./scripts/tp backup --label pre_v3_3_1

# 2. Install new version
./scripts/tp install v3.3.1
# Watch for: pandas_ta available

# 3. Promote and auto-cleanup ALL old versions
python3 scripts/tp_enhanced.py promote v3.3.1

# 4. Verify only v3.3.1 remains
./scripts/tp list
# Should show ONLY v3.3.1 with PRIMARY flag

# 5. Fix any service issues
python3 scripts/tp_enhanced.py fix-services

# 6. Check services
./service.sh status
# All should show "state = running"

# 7. Health check
sleep 2
curl -s http://localhost:8080/api/health | jq .

# 8. Monitor scheduler logs
./service.sh logs scheduler | tail -50
# Should see: "Cycle #X started (scheduler)"
```

---

## Architecture: Before vs After

### BEFORE (Current)
```
~/tp/versions/
├── v2.1.0/          ← OLD, takes 2GB
├── v3.2.0/          ← OLD, takes 2GB
└── v3.3.1/          ← PRIMARY (8082), takes 2GB

PRIMARY file = v3.3.1
Services running on: 8080, 8081, 8082
Registry: 3 versions tracked

Result: 6GB total, port conflicts, duplicate service registrations
```

### AFTER (Enhanced)
```
~/tp/versions/
└── v3.3.1/          ← ONLY version, takes 2GB

PRIMARY file = v3.3.1
Services running on: 8082 (proxied via 8080)
Registry: 1 version tracked

Result: 2GB total, no conflicts, clean operation
```

---

## Troubleshooting

### Still Getting "Address in use" Errors

```bash
# Find what's running on 8080/8081/8082
lsof -i :8080
lsof -i :8081
lsof -i :8082

# Kill stuck processes
kill -9 <PID>

# Clean launchctl completely
launchctl unload ~/Library/LaunchAgents/com.tradingplatform.*.plist 2>/dev/null || true
rm ~/Library/LaunchAgents/com.tradingplatform.*.plist 2>/dev/null || true

# Reinstall fresh
python3 scripts/tp_enhanced.py fix-services
```

### Services Won't Start After fix-services

```bash
# Check for plist syntax errors
plutil -lint ~/Library/LaunchAgents/com.tradingplatform.v3.3.1.scheduler.plist

# Manually bootstrap with debug output
sudo launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.tradingplatform.v3.3.1.scheduler.plist 2>&1 | head -20

# Check system logs
log stream --level debug --predicate 'process == "launchd"' 2>&1 | head -20
```

### Database Won't Drop

```bash
# If `dropdb` hangs, kill postgres connections
psql -c "SELECT pid, usename, query FROM pg_stat_activity WHERE datname = 'tp_v3_2_0';"

# Kill the connections
psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'tp_v3_2_0' AND pid <> pg_backend_pid();"

# Then dropdb should work
dropdb tp_v3_2_0
```

---

## Automation (Optional)

Make `tp_enhanced.py` an alias for ease of use:

```bash
# Add to ~/.zshrc or ~/.bash_profile
alias tp='python3 ~/trading_platform/scripts/tp_enhanced.py'

# Then use:
tp promote v3.3.1
tp cleanup
tp fix-services
```

---

## Safety Notes

1. **All removals include backups** - every version's database is backed up to `~/tp/archive/` before removal
2. **Only non-primary versions are removed** - the PRIMARY version is never deleted
3. **Worktrees are removed, not git branches** - the branches stay, just the checked-out copies are removed
4. **Reversible** - you can always `git worktree add` the branch back if needed

---

## Performance Impact

| Action | Before | After |
|--------|--------|-------|
| Disk space | 6GB (3 versions × 2GB) | 2GB (1 version) |
| Service startup | Slow (3 versions' services try to start) | Fast (1 version only) |
| Database connections | 3 postgres connections running | 1 connection running |
| Promotion time | 30s (uninstall old services) | 2m (uninstall + backup + remove) |

---

## When to Use Each Command

| Scenario | Command |
|----------|---------|
| Deploy new version + clean house | `tp promote v3.3.1` |
| Just remove old versions (already promoted) | `tp cleanup` |
| Fix stuck services without changing version | `tp fix-services` |
| Keep multiple versions for testing | Use original `./scripts/tp` instead |

---

## Version History

**v3.3.1** (Current)
- Scheduler race condition fix (coalesce=False + DB interlock)
- Deploy with: `python3 scripts/tp_enhanced.py promote v3.3.1`

**v3.3.0** (Old)
- Will be removed by: `python3 scripts/tp_enhanced.py promote v3.3.1`

**v3.2.0** (Old)
- Will be removed by: `python3 scripts/tp_enhanced.py promote v3.3.1`

**v2.1.0** (Old)
- Will be removed by: `python3 scripts/tp_enhanced.py promote v3.3.1`
