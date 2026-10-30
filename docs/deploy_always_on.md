# Running the engine on an always-on machine

The strategies are rule-based and run on a schedule (month-end rebalances 15:15-15:25 CT, a daily maintenance job at
06:30 ET, swing cycles every 15 min in market hours). A laptop is the weak link: a reboot, a macOS update, a power cut
or an unplugged battery stops everything. Missed month-ends are caught up on the next run, but the daily marks and
swing cycles in between are lost.

## What is already hardened
- launchd `KeepAlive` restarts the scheduler and UI if they die; they start at login (`scripts/services.py install`).
- `caffeinate` + `sudo pmset -a sleep 0 disablesleep 1` keep the Mac awake.
- Missed month-end rebalances are caught up (`needs_rebalance`); missed daily marks are backfilled (`engine/mark_backfill.py`).
- A nightly Postgres dump is kept in `output/backups` (`scripts/backup_db.py`).

## Staying always-on at zero cost
Everything here is free: the Mac you already own, built-in macOS settings, free-tier data APIs and the free ntfy.sh topic.
1. Keep the Mac plugged in (sleep is already disabled with `sudo pmset -a sleep 0 disablesleep 1`).
2. After a power cut, start automatically: `sudo pmset -a autorestart 1`, and turn on automatic login
   (System Settings > Users & Groups) so the launchd agents start without you.
3. Turn off automatic macOS restarts for updates (System Settings > General > Software Update > Automatic updates ->
   only "Security responses"), and install updates yourself at a weekend.
4. Copy the nightly dumps off the disk for free: set `TP_BACKUP_DIR` to a folder inside iCloud Drive or an external
   drive (see `scripts/backup_db.py`).
5. A spare old laptop or Raspberry Pi you already own can run the same `scripts/services.py install` setup; nothing
   needs to be bought for this.

Paid options (a cloud VM, a Mac mini) are deliberately not part of this plan. Paper trading only needs the above for
continuity; revisit hosting only if you ever decide to trade real money.
