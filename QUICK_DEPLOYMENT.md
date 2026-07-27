# Quick Deployment Commands - v3.3.1

## One-Liner Summary
Scheduler race condition fix: disable coalesce + add DB-level interlock

---

## Commands to Run (in order)

### Step 1: Verify & Clean
```bash
cd ~/trading_platform
git status
git restore config.yaml      # Revert unrelated changes
git status                   # Should show only scheduler.py modified
```

### Step 2: Commit
```bash
git add scheduler.py SCHEDULER_ISSUE_ANALYSIS.md
git commit -m "fix: scheduler race condition - disable coalesce and add DB-level interlock

§49 Phase 4 - Fix for 2026-07-27 incident where 8 AM scheduled cycle was
silently dropped (coalesced) while a manual run was in progress.

Changes:
1. Disable APScheduler coalesce=True - missed scheduled slots now queue
2. Add DB-level interlock in run_cycle() - prevents overlapping cycles
3. Add scheduler heartbeat monitoring - detects degradation early

Fixes: Silent scheduler death when manual runs overlap scheduled slots"
```

### Step 3: Create Tag
```bash
git tag -a v3.3.1 -m "v3.3.1: scheduler race condition fix

Fixes critical race condition where scheduled cycles were silently dropped
when manual runs overlapped with scheduled fire times. Pairs with 2026-07-22
child process timeout fix for complete scheduler protection.

No database schema changes. Backward compatible. Rollback: tp promote v3.3.0"
```

### Step 4: Push to Remote
```bash
git push origin main
git push origin v3.3.1
```

### Step 5: Backup Current
```bash
./scripts/tp backup --label pre_v3_3_1
```

### Step 6: Install New Version
```bash
./scripts/tp install v3.3.1
```
⚠️ Watch output for `pandas_ta` - if it shows WARNING, stop and fix venv

### Step 7: Promote to Primary
```bash
./scripts/tp promote v3.3.1
```

### Step 8: Verify
```bash
./scripts/tp list           # Verify v3.3.1 is primary
./service.sh status         # Should show scheduler/ui/maverick running
sleep 2
curl -s localhost:$(./scripts/tp list | awk '/v3.3.1/{print $3}')/api/health
```

### Step 9: Monitor Logs
```bash
./service.sh logs scheduler | tail -50
```

Expected in logs:
- ✅ "Cycle #X started (scheduler)" at regular intervals
- ✅ "Cycle #X started (manual)" when triggered manually
- ✅ No errors or warnings

NOT expected:
- ❌ "Scheduled cycle skipped - another cycle is already in progress" (not during normal operation)

---

## Testing the Fix

### Test 1: Normal Scheduled Cycles
Wait for a scheduled cycle to fire and complete. Check logs:
```bash
./service.sh logs scheduler | grep "Cycle #.*started (scheduler)"
```

### Test 2: Manual + Scheduled Overlap
Trigger a manual run at a time close to a scheduled slot:
```bash
curl -X POST http://localhost:8001/api/cycle/run_now
# Immediately check logs
./service.sh logs scheduler | tail -30
```

Expected behavior:
- Manual run starts
- If scheduled slot tries to fire → "Scheduled cycle skipped" message
- Manual run finishes
- Next scheduled slot executes normally
- No overlapping cycles

### Test 3: Check Heartbeat Monitoring
Wait 30+ minutes and check for scheduler health warnings:
```bash
./service.sh logs scheduler | grep "Scheduler WARNING"
```

Should NOT see warnings during normal operation (cycles finishing within 1.5x expected interval)

---

## Rollback (if needed)

```bash
./scripts/tp promote v3.3.0
./service.sh status
```

This is immediate and lossless. v3.3.0's database/venv unchanged.

---

## What Changed

### File: `scheduler.py`

**Line 206-256 (run_cycle function)**
```python
# NEW: Database-level interlock for scheduled cycles
if not force:  # Only for scheduled runs (force=False)
    status = db.get_cycle_status()
    if status.get("is_running"):
        logger.info("Scheduled cycle skipped - another cycle is already in progress")
        return
```

**Line 2093 (APScheduler config)**
```python
# CHANGED: coalesce=True → coalesce=False
id="trading_cycle", max_instances=1, coalesce=False,  # was: coalesce=True
```

**Lines 2096-2115 (_record_next_run function)**
```python
# NEW: Scheduler heartbeat monitoring
if job.last_execution_time:
    elapsed = (now - job.last_execution_time).total_seconds()
    expected_interval = interval * 60
    if elapsed > expected_interval * 1.5:
        logger.warning(f"Scheduler WARNING: Last cycle finished {elapsed / 60:.1f} min ago...")
```

### Files Added

**SCHEDULER_ISSUE_ANALYSIS.md** - Full technical analysis
**DEPLOYMENT_COMMANDS.sh** - Complete deployment automation script

---

## FAQ

**Q: Will scheduled cycles run at different times?**
A: No. They still run at the same cron schedule (every 5 min by default, Mon-Fri 9-16 ET). This fix just prevents them from being silently dropped.

**Q: Can manual runs overlap with scheduled runs now?**
A: No. Manual runs proceed, but scheduled cycles are skipped if one is running. They queue up and execute after.

**Q: Do I need to change the database?**
A: No. This uses existing cycle_status table. No schema changes.

**Q: How long does deployment take?**
A: ~5-10 min for install + promote. Services restart automatically.

**Q: What if something breaks?**
A: Run `./scripts/tp promote v3.3.0` to rollback immediately.

---

## Version Info

| Component | Before | After |
|-----------|--------|-------|
| Primary Version | v3.3.0 | v3.3.1 |
| APScheduler coalesce | True ❌ | False ✅ |
| DB-level interlock | None ❌ | Yes ✅ |
| Heartbeat monitoring | None ❌ | Yes ✅ |

---

## Incident Reference

**Date:** 2026-07-27 08:00 ET
**Issue:** 8 AM scheduled cycle coalesced away while manual run in progress
**Root Cause:** APScheduler's `coalesce=True` + scheduled run overlapping with in-flight manual run
**Impact:** Scheduler went silent until next manual trigger
**Status:** FIXED in v3.3.1
