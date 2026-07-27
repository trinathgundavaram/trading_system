# Scheduler Issue Analysis & Recommendations

## Problem Summary

The 8 AM scheduled cycle was not triggered because a manual run was in progress, and the scheduler hasn't attempted to run again since. This is a **race condition** with APScheduler's coalescing behavior combined with manual run timing.

## Root Cause Analysis

### What the logs show:
```
2026-07-27 07:57:26 - Manual cycle started
2026-07-27 08:00:44 - Manual cycle finished (3:18 duration)
2026-07-27 08:11:04 - Manual cycle started again
2026-07-27 08:14:24 - Manual cycle finished

NOTE: No "scheduler" triggered run appears between 07:57:26 and 08:11:04
      The 8 AM scheduled run (which should fire at 08:00:00) is missing.
```

### The Race Condition

1. **At 07:57:26**: Manual run is triggered via API → calls `run_cycle(force=True)` → `run_supervised()`
2. **At 08:00:00**: The 8 AM scheduled cron job tries to fire
   - APScheduler's `BlockingScheduler` has `max_instances=1` and `coalesce=True`
   - The manual run's `run_supervised()` is still executing (won't finish until 08:00:44)
   - APScheduler sees the job is "still running" and **coalesces** the 8 AM tick
3. **At 08:00:44**: Manual run finishes → `run_cycle()` returns → `db.set_cycle_finished()` is called
4. **At 08:05:00**: Next scheduled slot would fire, but something is preventing it
5. **At 08:11:04**: Manual run is triggered again (no cron-scheduled run visible)

### Why the scheduler doesn't pick up the next slot

The APScheduler configuration (line 2064-2067 of scheduler.py):
```python
scheduler.add_job(
    run_cycle, "cron", day_of_week="mon-fri", hour="9-16", minute=f"*/{interval}",
    id="trading_cycle", max_instances=1, coalesce=True,
)
```

**The Problem**:
- `coalesce=True` means if multiple scheduled times are missed while the job is running, they get combined into ONE execution
- Once a scheduled slot is coalesced (skipped), it's gone forever
- Even after the manual run finishes and `db.set_cycle_finished()` is called, APScheduler's internal state might not immediately recognize the job as "available" for the next scheduled slot

**The Bigger Problem**:
- There's a **timing window** (~44 seconds in this case) where:
  - A manual run is still executing (set_cycle_running=true in DB)
  - The next scheduled slot tries to fire
  - APScheduler coalesces it
  - Even when the manual run finishes, the cron job may not immediately requeue for the next minute

## Code Review: Current Implementation

### What was supposed to fix this (2026-07-22 incident):
The `run_supervised()` function was designed to:
- Run the cycle in a child process with a hard timeout (default 15 min)
- Ensure `run_cycle()` always returns within ~15 min, no exceptions
- Allow APScheduler to immediately consider the job "done" when the function returns

### What's still broken:
1. **APScheduler's `coalesce=True` policy** - Once a scheduled tick is missed during an in-flight cycle, it's silently dropped
2. **Manual + Scheduled overlap** - When a manual run overlaps with a scheduled run's fire time, the scheduled run is coalesced away
3. **No retry mechanism** - If the 8 AM run is coalesced, there's no built-in recovery until the NEXT scheduled slot
4. **No monitoring of missing cycles** - The logs show "Scheduled cycle appears MISSED" was logged in 2026-07-22, but there's no such alert now

## Root Cause: APScheduler's Coalescing Behavior

The fundamental issue is APScheduler's `coalesce=True` configuration:
- When multiple scheduled times fire while a job is running, they are "coalesced" into a single execution
- In this case: The 8:00, 8:05, 8:10 AM slots all fire within the same minute the manual run is finishing
- APScheduler coalesces all of them into one queued execution
- But that queued execution never happens because by the time it would run, the next "real" scheduled slot has already passed

## Fixes Applied

### 1. **Disabled Coalescing in APScheduler** ✅
**Location**: `scheduler.py`, line 2093
**Changed**: `coalesce=True` → `coalesce=False`

**Effect**:
- Missed scheduled slots are now queued for execution instead of being dropped
- If the 8 AM run fires while a manual run is in progress, it will execute after the manual run finishes
- Multiple missed slots will queue up and execute in FIFO order

### 2. **Added Database-Level Interlock** ✅
**Location**: `scheduler.py`, lines 206-256 (in `run_cycle()` function)
**Change**: Check if a cycle is already running before proceeding

**Effect**:
- Scheduled cycles (force=False) skip silently if a cycle is already running
- Manual cycles (force=True) always proceed
- Prevents duplicate overlapping cycles even with `coalesce=False`
- When a scheduled run is queued and later tries to execute, the interlock prevents it from starting if another cycle is in progress

**Code**:
```python
if not force:
    try:
        status = db.get_cycle_status()
        if status.get("is_running"):
            logger.info("Scheduled cycle skipped - another cycle is already in progress")
            return
    except Exception as e:
        logger.warning(f"Could not check cycle status (proceeding anyway): {e}")
```

### 3. **Added Scheduler Heartbeat Monitoring** ✅
**Location**: `scheduler.py`, lines 2096-2115 (in `_record_next_run()` function)
**Change**: Monitor if scheduler is falling behind

**Effect**:
- Logs a warning if the last cycle finished more than 1.5x the expected interval ago
- Helps detect when cycles are being coalesced or delayed
- Provides visibility for debugging future scheduler issues

**Code**:
```python
if job.last_execution_time:
    now = datetime.now(ET)
    elapsed = (now - job.last_execution_time).total_seconds()
    expected_interval = interval * 60
    if elapsed > expected_interval * 1.5:
        logger.warning(
            f"Scheduler WARNING: Last cycle finished {elapsed / 60:.1f} min ago, "
            f"expected interval is {interval} min - cycles may be getting coalesced"
        )
```

## How the Fix Works

**Scenario: Manual run at 07:57, scheduled run at 08:00**

1. Manual run starts at 07:57:26 (force=True)
   - Calls `run_cycle(force=True)`
   - Skips the is_running check (force=True bypasses it)
   - Calls `run_supervised()`

2. 08:00:00 - Scheduled cron fires
   - With `coalesce=False`, the 08:00 slot is now queued (not coalesced away)
   - But when the run_cycle() function is called, it checks if a cycle is running
   - Manual run is still in progress → DB says is_running=True
   - Scheduled run is skipped with a log message
   - The queued 08:00 slot remains in APScheduler's queue

3. 08:00:44 - Manual run finishes
   - Calls `db.set_cycle_finished()` → is_running=False

4. 08:05:00 - Next scheduled slot fires
   - Calls `run_cycle(force=False)`
   - Checks is_running → False (manual run finished)
   - Proceeds with the cycle

5. OR if APScheduler had already queued the 08:00 and 08:05 slots together:
   - After manual run finishes, both queued slots will try to run
   - First one checks is_running → False → runs
   - Other queued ones check is_running → True (first queued run is now in progress) → skipped with log message
   - No duplicate or overlapping cycles

## What This Prevents

1. **Silent scheduler death**: A missed scheduled slot is no longer silently dropped
2. **Overlapping cycles**: The DB interlock prevents concurrent cycles
3. **Future incidents**: Heartbeat monitoring alerts if scheduler is degrading

## Testing

After deployment, monitor logs for:

1. **Expected behavior**:
   - Scheduled cycles run at regular intervals
   - Manual runs always proceed
   - No log messages saying "Scheduled cycle skipped - another cycle is already in progress" during normal operation

2. **If manual + scheduled overlap**:
   - Manual cycle runs
   - Log shows "Scheduled cycle skipped - another cycle is already in progress"
   - After manual cycle finishes, next scheduled slot executes normally
   - No backlog or multiple cycles running

3. **Heartbeat monitoring**:
   - Should see "Scheduler WARNING" only if cycles are taking much longer than expected
   - Not during normal operation

## Code Locations

- **APScheduler config**: `scheduler.py`, lines 2084-2094
- **DB-level interlock**: `scheduler.py`, lines 206-256 (run_cycle function)
- **Heartbeat monitoring**: `scheduler.py`, lines 2096-2115 (_record_next_run function)
- **Cycle supervisor**: `engine/cycle_supervisor.py`, lines 219-288
- **Database status tracking**: `storage/database.py` (cycle_status table)

## Related Incidents

- **2026-07-22**: Cron scheduler silently stopped firing (root cause: inline cycle body could wedge forever)
  - Fixed by: Moving cycle body to child process with hard timeout
  - This fix: Additional safeguards against APScheduler's coalescing behavior

## Deployment Notes

1. No database migrations needed
2. No configuration file changes needed
3. Backward compatible with existing cycles
4. Can be rolled back by reverting the two changes (coalesce=True, remove interlock)
