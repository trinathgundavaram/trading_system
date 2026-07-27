# Deployment Commands - Copy & Paste Ready

## Pre-Deployment Cleanup (Run from your actual terminal)

```bash
cd ~/trading_platform

# Clear git lock that may exist
rm -f .git/index.lock

# Check current state
git status
git log --oneline -3
git describe --tags
```

---

## Commit & Tag (Copy & paste each command)

```bash
cd ~/trading_platform

# STEP 1: Clean config.yaml (only if it has the comment-only changes)
git restore config.yaml

# STEP 2: Stage files
git add scheduler.py SCHEDULER_ISSUE_ANALYSIS.md

# STEP 3: Commit
git commit -m "fix: scheduler race condition - disable coalesce and add DB-level interlock

§49 Phase 4 - Fix for 2026-07-27 incident where 8 AM scheduled cycle was
silently dropped while manual run was in progress.

Root cause: APScheduler coalesce=True silently drops scheduled slots that
fire while job is running. Manual run (07:57-08:00:44) caused 8 AM slot to
be coalesced away forever.

Changes:
1. Line 2093: coalesce=True → coalesce=False (missed slots queue for retry)
2. Lines 206-256: DB-level interlock (scheduled cycles skip if one running)
3. Lines 2096-2115: Heartbeat monitoring (alerts if cycles degrading)

Testing: Scheduled cycles run regularly, manual runs proceed, no overlaps.

Fixes: Silent scheduler death on manual/scheduled overlap"

# STEP 4: Verify commit
git log --oneline -1
git show --stat HEAD

# STEP 5: Create release tag
git tag -a v3.3.1 -m "v3.3.1: scheduler race condition fix

Fixes critical race condition where scheduled cycles silently dropped
when manual runs overlapped scheduled fire times.

2026-07-27 incident: Manual run 07:57-08:00:44 caused 8 AM scheduled
slot to be coalesced away. This pairs with 2026-07-22 child-process-timeout
fix for complete scheduler protection.

No database changes. Backward compatible. Rollback: tp promote v3.3.0"

# STEP 6: Verify tag
git describe --tags
git show v3.3.1 | head -20
```

---

## Push to Remote

```bash
cd ~/trading_platform

# Push main branch
git push origin main

# Push tag
git push origin v3.3.1

# Verify
git log --oneline -1
git branch -vv
```

---

## Deploy & Promote

```bash
cd ~/trading_platform

# STEP 1: Backup current version
./scripts/tp backup --label pre_v3_3_1

# STEP 2: Install new version
./scripts/tp install v3.3.1

# Wait for install to complete, watch for pandas_ta availability

# STEP 3: Promote to primary
./scripts/tp promote v3.3.1

# STEP 4: Verify deployment
./scripts/tp list
./service.sh status

# STEP 5: Health check
sleep 2
curl -s localhost:$(./scripts/tp list | awk '/v3.3.1/{print $3}')/api/health
```

---

## Post-Deployment Verification

```bash
cd ~/trading_platform

# STEP 1: Check logs for successful startup
./service.sh logs scheduler | head -50

# STEP 2: Verify scheduler is running (wait ~2 min for next cycle)
./service.sh logs scheduler | tail -30

# Should see:
# ✅ "Cycle #X started (scheduler)" entries
# ✅ "Cycle #X done in Ys" completion messages
# ❌ NO errors or "Scheduled cycle skipped" messages during normal operation
```

---

## Test: Manual Run During Scheduled Time

```bash
# Get current time - wait until just before a scheduled slot
# (e.g., if interval is 5 min, wait until :58 or :59 of a minute)

# Trigger manual run
curl -X POST http://localhost:8001/api/cycle/run_now

# Immediately check logs
./service.sh logs scheduler | tail -50

# Expected:
# - Manual run starts (triggered_by=manual)
# - If scheduled slot fires: "Scheduled cycle skipped - another cycle is already in progress"
# - Manual run completes
# - Next scheduled slot runs normally (triggered_by=scheduler)
```

---

## Rollback (if needed)

```bash
cd ~/trading_platform

# Revert to previous version
./scripts/tp promote v3.3.0

# Verify
./service.sh status
./scripts/tp list
```

---

## Quick Status Commands (Bookmark these)

```bash
# Check which version is primary
./scripts/tp list

# View active services
./service.sh status

# Watch scheduler logs in real-time
./service.sh logs scheduler | tail -f

# Health check
curl -s http://localhost:8001/api/health

# API cycle info
curl -s http://localhost:8001/api/cycles/latest | jq .
```

---

## Troubleshooting

**Problem: `git push origin v3.3.1` fails**
```bash
# Check if tag already exists
git tag -l v3.3.1
# If it exists, delete and recreate
git tag -d v3.3.1
git tag -a v3.3.1 -m "your message"
git push origin v3.3.1
```

**Problem: `./scripts/tp install` fails with pandas_ta warning**
```bash
# This is critical - cycle thresholds depend on pandas_ta
# Stop deployment and fix venv
# Then restart: ./scripts/tp install v3.3.1
```

**Problem: Services won't restart**
```bash
# Check if ports are in use
lsof -i :8001  # UI port
lsof -i :8002  # Scheduler port

# Kill stuck process if needed
kill -9 <PID>

# Restart
./service.sh stop
./service.sh start
```

**Problem: Want to check what changed**
```bash
# View diff before committing
git diff scheduler.py

# View commit history
git log --oneline -10

# View specific version
git show v3.3.1:scheduler.py | head -100
```

---

## Success Criteria

✅ `git push origin v3.3.1` succeeds
✅ `./scripts/tp list` shows v3.3.1 as primary
✅ `./service.sh status` shows scheduler/ui/maverick running
✅ `curl localhost:8001/api/health` returns 200 OK
✅ Logs show scheduled cycles running at regular intervals
✅ No errors or exceptions in scheduler.log

---

## Summary of What Changed

| File | Change | Reason |
|------|--------|--------|
| scheduler.py:206-256 | Add DB-level interlock | Prevent overlapping cycles |
| scheduler.py:2093 | coalesce=False | Queue missed slots instead of dropping |
| scheduler.py:2096-2115 | Add heartbeat monitoring | Detect scheduler degradation early |
| SCHEDULER_ISSUE_ANALYSIS.md | Full technical analysis | Reference documentation |
| DEPLOYMENT_COMMANDS.sh | Automation script | One-shot deployment |
| QUICK_DEPLOYMENT.md | Step-by-step guide | Manual deployment |

---

## Version History

```
v3.3.1 (Current) - Scheduler race condition fix
  ↓
v3.3.0 - Previous stable
  ↓
v3.2.0 - External audit follow-through
  ↓
v3.1.1 - Learning loop improvements
```

Rollback any time with: `./scripts/tp promote v3.3.0`
